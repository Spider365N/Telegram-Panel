"""
End-to-end ("live") tests for the whole panel.

They run the real classes — router, panels, services, engagement, executor —
against fake Telegram transports, and cover the operator-visible promises:

* a normal user gets the service panel, the owner gets the admin panel
* the private-channel reaction bug from the production log is gone
* signed-in accounts survive a crash (session strings in the database)
* the bot being added to a channel starts free automation immediately
* the command menu is pushed to Telegram automatically
"""
from __future__ import annotations

import asyncio

import pytest
from telethon.sessions import StringSession

from src import commands as commands_module
from src.config import ADMIN_ID
from src.services.accounts import export_session_string, get_account_registry
from tests.fakes import (
    FakeEvent,
    FakeTelegramClient,
    build_stack,
    drain_executor,
    make_channel,
)

OWNER = int(ADMIN_ID or 123456789)
USER = 987654321


async def boot(accounts: int = 4, channels=None, member_of=None):
    channels = channels or {}
    clients = {
        f"phone{i + 1}": FakeTelegramClient(f"phone{i + 1}", user_id=7000 + i,
                                            channels=channels, member_of=member_of)
        for i in range(accounts)
    }
    stack = await build_stack(clients)
    stack.router._cache.ttl = 0.0
    await stack.registry.sync_from_live_clients()
    return stack


def user_event(data=None, text="", sender_id=USER):
    return FakeEvent(sender_id=sender_id, chat_id=sender_id, data=data, text=text)


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestFullJourney:
    async def test_user_journey_start_to_finished_order(self):
        channel = make_channel(777001)
        stack = await boot(4, channels={777001: channel})

        # 1) /start → service panel
        start = user_event()
        await stack.router.start_for(start)
        assert "TELEGRAM PANEL" in start.last_text

        # 2) reactions menu → free plan
        await stack.router.handle_callback(user_event(data="u:or"))
        free = user_event(data="u:free_react")
        await stack.router.handle_callback(free)
        assert "Free reactions" in free.last_text

        # 3) send the post link → order runs
        link = user_event(text="https://t.me/c/777001/42")
        await stack.router.handle_message(link)
        await drain_executor(stack)

        orders = await stack.orders.list_for_user(USER)
        assert len(orders) == 1 and orders[0]["status"] == "completed"
        for client in stack.bot.active_clients.values():
            assert "SendReactionRequest" in [type(r).__name__ for r in client.requests]

        # 4) order management shows the finished order
        listing = user_event(data="u:om")
        await stack.router.handle_callback(listing)
        assert "Order Management" in listing.last_text

    async def test_owner_journey_admin_panel_and_price_change(self):
        stack = await boot(2)
        start = user_event(sender_id=OWNER)
        await stack.router.start_for(start)
        assert "ADMIN PANEL" in start.last_text

        await stack.router.handle_callback(user_event(data="a:pricing", sender_id=OWNER))
        await stack.router.handle_callback(
            user_event(data="a:plan_edit:reactions:react_50:price", sender_id=OWNER))
        await stack.router.handle_message(user_event(text="59", sender_id=OWNER))

        # the new price is what a user is charged
        assert (await stack.settings.get_plan("reactions", "react_50"))["price"] == 59
        await stack.users.credit(USER, 100, "topup")
        order = user_event(data="u:buy_react:react_50")
        await stack.router.handle_callback(order)
        await stack.router.handle_message(user_event(text="https://t.me/c/1/1"))
        assert await stack.users.balance(USER) == 41

    async def test_private_channel_reaction_crash_is_fixed(self):
        """The production error: PeerChannel(channel_id=…) not found."""
        channel = make_channel(3852827894, title="Private")
        stack = await boot(3, channels={3852827894: channel})
        await stack.users.credit(USER, 500, "topup")

        await stack.router.handle_callback(user_event(data="u:buy_react:react_200"))
        await stack.router.handle_message(user_event(text="https://t.me/c/3852827894/22"))
        await drain_executor(stack)

        finished = (await stack.orders.list_for_user(USER))[0]
        assert finished["status"] in ("completed", "partial")
        assert finished["result"]["ok"] > 0
        assert not any("Could not find the input entity" in err
                       for err in finished["result"].get("errors", []))


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCrashRecovery:
    async def test_session_strings_are_stored_in_the_database(self):
        stack = await boot(2)
        docs = await stack.accounts.list_accounts()
        assert len(docs) == 2
        for doc in docs:
            assert doc["session_string"], "session string must be persisted"
            assert doc["status"] == "active"
            assert doc["phone"] and doc["tg_user_id"]

    async def test_accounts_are_restored_after_a_restart(self, monkeypatch):
        stack = await boot(3)
        stored = await stack.accounts.list_accounts()
        assert len(stored) == 3

        # --- simulate a crash/restart: memory is empty, database is not ----
        stack.bot.active_clients.clear()
        stack.registry._doc_by_session.clear()
        stack.registry._restore_attempted = False

        created = {}

        def fake_builder(session_string, **kwargs):
            client = FakeTelegramClient(f"restored_{len(created)}", user_id=8000 + len(created))
            created[session_string[:16]] = client
            return client

        monkeypatch.setattr("src.services.accounts.build_client_from_session", fake_builder)
        restored = await stack.registry.restore_missing_clients()
        assert restored == 3
        assert len(stack.bot.active_clients) == 3

    async def test_revoked_session_is_marked_not_restored_forever(self, monkeypatch):
        stack = await boot(1)
        doc = (await stack.accounts.list_accounts())[0]
        stack.bot.active_clients.clear()
        stack.registry._doc_by_session.clear()
        stack.registry._restore_attempted = False

        def unauthorized_builder(session_string, **kwargs):
            client = FakeTelegramClient("dead", authorized=False)
            return client

        monkeypatch.setattr("src.services.accounts.build_client_from_session", unauthorized_builder)
        restored = await stack.registry.restore_missing_clients()
        assert restored == 0
        refreshed = await stack.store.find_one("accounts", {"_id": doc["_id"]})
        assert refreshed["status"] == "revoked"

    async def test_sessions_survive_sign_out_of_session_files(self):
        """Session strings are self-sufficient — no .session file needed."""
        stack = await boot(1)
        doc = (await stack.accounts.list_accounts())[0]
        session = StringSession(doc["session_string"])
        assert session.auth_key is not None
        assert session.dc_id == 2

    async def test_registry_picks_only_healthy_accounts(self):
        stack = await boot(3)
        docs = await stack.accounts.list_accounts()
        await stack.accounts.set_status(docs[0]["_id"], "revoked", "dead")
        healthy = await stack.registry.healthy_clients()
        assert len(healthy) == 2


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestChannelInstallFlow:
    async def test_bot_added_to_channel_starts_free_automation(self):
        from src.telbot import TelegramBot

        stack = await boot(5)
        channel = make_channel(555001, title="Customer Channel")

        bot = TelegramBot.__new__(TelegramBot)
        bot.tbot = stack.bot.tbot
        bot.active_clients = stack.bot.active_clients
        bot.active_clients_lock = asyncio.Lock()
        bot.store = stack.store
        bot.account_registry = stack.registry
        bot.panel_router = stack.router
        bot.services_ready = True
        bot.account_handler = None
        bot.client_manager = None
        bot.monitor = None
        bot.config = {"clients": {}, "inactive_accounts": {}}

        class AddedEvent:
            user_added = True
            user_kicked = False
            sender_id = USER
            chat_id = USER
            user_id = None  # filled below

            async def get_chat(self_inner):
                return channel

            async def respond(self_inner, *args, **kwargs):
                return None

        event = AddedEvent()
        event.user_id = (await stack.bot.tbot.get_me()).id  # the bot itself

        await bot._chat_action_handler(event)

        registered = await stack.channels.get(-100555001)
        assert registered is not None
        assert registered["owner_id"] == USER
        # Auto reactions are the free tier — they always start.
        assert registered["auto_reactions"]["enabled"] is True
        # Auto views are a premium service, so they stay off for a free user.
        assert registered["auto_views"]["enabled"] is False
        # the owner got a confirmation message
        texts = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("Channel linked" in t for t in texts)

    async def test_premium_user_gets_auto_views_on_channel_add(self):
        from src.services.users import get_user_service
        from src.telbot import TelegramBot

        channel = make_channel(555003)
        stack = await boot(3, channels={555003: channel})
        # the owner activates the views plan for this user
        await get_user_service().grant_plan(USER, kinds=["views"], days=30)

        bot = TelegramBot.__new__(TelegramBot)
        bot.tbot = stack.bot.tbot
        bot.active_clients = stack.bot.active_clients
        bot.account_registry = stack.registry
        bot.store = stack.store
        bot.panel_router = stack.router
        bot.services_ready = True

        class AddedEvent:
            user_added = True
            user_kicked = False
            sender_id = USER
            chat_id = USER
            user_id = None

            async def get_chat(self_inner):
                return channel

            async def respond(self_inner, *args, **kwargs):
                return None

        event = AddedEvent()
        event.user_id = (await stack.bot.tbot.get_me()).id
        await bot._chat_action_handler(event)

        registered = await stack.channels.get(-100555003)
        assert registered["auto_reactions"]["enabled"] is True
        assert registered["auto_views"]["enabled"] is True

    async def test_new_post_triggers_auto_engagement(self):
        from src.telbot import TelegramBot

        channel = make_channel(555002)
        stack = await boot(6, channels={555002: channel})
        await stack.channels.register(-100555002, USER, title="Auto")
        await stack.auto.enable_free_plan(-100555002)
        await stack.settings.set_path("reactions.auto.accounts_per_post", 4)

        bot = TelegramBot.__new__(TelegramBot)
        bot.tbot = stack.bot.tbot
        bot.active_clients = stack.bot.active_clients
        bot.account_registry = stack.registry

        class PostEvent:
            id = 12
            chat_id = USER
            sender_id = USER

            async def get_chat(self_inner):
                return channel

        await bot._channel_post_handler(PostEvent())
        doc = await stack.channels.get(-100555002)
        assert doc["stats"]["posts_seen"] == 1
        assert doc["stats"]["reactions_sent"] == 4


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestCommandRegistration:
    async def test_commands_are_pushed_to_telegram(self):
        client = FakeTelegramClient("bot", user_id=1)
        result = await commands_module.register_commands(client, owner_id=OWNER, admin_ids=[OWNER])
        assert result["user"] is True and result["admin"] is True
        assert [type(r).__name__ for r in client.requests].count("SetBotCommandsRequest") >= 2
        sent = [r for r in client.requests if type(r).__name__ == "SetBotCommandsRequest"]
        default_scope = [r for r in sent if type(r.scope).__name__ == "BotCommandScopeDefault"]
        assert default_scope, "default scope must be registered"
        names = {c.command for c in default_scope[0].commands}
        assert {"start", "views", "reactions", "orders", "wallet", "premium"} <= names
        admin_scope = [r for r in sent if type(r.scope).__name__ == "BotCommandScopeChatAdmins"]
        assert admin_scope and any(c.command == "pricing" for c in admin_scope[0].commands)

    async def test_registration_failure_does_not_raise(self):
        class Exploding:
            async def __call__(self, request, *args, **kwargs):
                raise RuntimeError("network down")

        result = await commands_module.register_commands(Exploding())
        assert result["user"] is False and result["admin"] is False

    async def test_every_registered_command_is_handled_by_the_router(self):
        stack = await boot(1)
        handled = set()
        cmds = {
            "views": "ov", "reactions": "or", "autoviews": "av", "autoreactions": "ar",
            "members": "mb", "orders": "om", "wallet": "wallet", "premium": "prem",
            "account": "ai", "support": "sp", "addchannel": "addch", "addgroup": "addgr",
        }
        for command, action in cmds.items():
            event = user_event(data=f"u:{action}")
            await stack.router.handle_callback(event)
            handled.add(command)
            assert event.last_text, f"{command} produced no screen"
        assert handled == set(cmds)


# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestSessionExport:
    async def test_export_round_trip(self):
        client = FakeTelegramClient("phone9", user_id=4242)
        session_string = export_session_string(client)
        assert session_string and len(session_string) > 20
        restored = StringSession(session_string)
        assert restored.dc_id == client.session.dc_id

    async def test_registry_register_stores_metadata(self):
        stack = await boot(0)
        client = FakeTelegramClient("phoneX", user_id=5150)
        client._me = type("Me", (), {
            "id": 5150, "username": "kaka", "first_name": "Kaka", "last_name": "S",
            "phone": "+15550000007",
        })()
        doc_id = await stack.registry.register("phoneX", client, owner_id=42)
        doc = await stack.store.find_one("accounts", {"_id": doc_id})
        assert doc["tg_user_id"] == 5150
        assert doc["name"] == "Kaka S"
        assert doc["owner_id"] == 42
        assert doc["session_string"]


@pytest.mark.asyncio
class TestMultiUserIsolation:
    async def test_two_users_get_separate_orders_and_wallets(self):
        channel = make_channel(606060)
        stack = await boot(4, channels={606060: channel})
        other = USER + 1
        await stack.users.credit(USER, 500, "topup")
        await stack.users.credit(other, 500, "topup")
        # Views are a premium service: activate the plan for both test users.
        await stack.users.grant_plan(USER, kinds=["views"], days=30)
        await stack.users.grant_plan(other, kinds=["views"], days=30)

        for buyer in (USER, other):
            event = FakeEvent(sender_id=buyer, chat_id=buyer, data="u:buy_views:views_1k")
            await stack.router.handle_callback(event)
            await stack.router.handle_message(
                FakeEvent(sender_id=buyer, chat_id=buyer, text="https://t.me/c/606060/1"))
            await drain_executor(stack)

        assert len(await stack.orders.list_for_user(USER)) == 1
        assert len(await stack.orders.list_for_user(other)) == 1
        # Both users have a views plan: the orders are covered by it, so nothing
        # is taken from their wallets — but each user still gets their own order.
        assert await stack.users.balance(USER) == 500
        assert await stack.users.balance(other) == 500
        assert (await stack.orders.list_for_user(USER))[0]["paid_with"] == "plan"

    async def test_user_cannot_see_another_users_order(self):
        stack = await boot(1)
        await stack.users.credit(USER, 500, "topup")
        plan = await stack.orders.quote_plan("views", "views_1k")
        order = await stack.orders.create_order(USER, "views", {"link": "x"}, plan=plan)
        event = FakeEvent(sender_id=USER + 5, chat_id=USER + 5, data=f"u:order:{order['_id']}")
        await stack.router.handle_callback(event)
        assert any("not found" in str(a["text"]).lower() for a in event.answers)
