"""
Round 6 — everything the production log (`logs.800000003.json`) reported.

Each test here pins one of the operator's requirements:

1. OTP / 2-step password mistakes get 3 attempts instead of restarting the flow.
2. Accounts must never be logged out or marked inactive because of an error —
   only a ban/block/revoked session may drop one (pool self-healing).
3. Admin/owner god mode: every feature, no payment.
4. Auto-join bookkeeping stays in the logs, not in the chat.
5. A plan that is granted covers its services: no per-feature payment requests.
6. Plan activation notification with details + duration.
8. Random positive auto comments on new posts.
9. Auto views actually run (the flag is not switched off any more, and a plan
   granted later turns them on).
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeInvalidError,
    SessionRevokedError,
    UserDeactivatedBanError,
)
from src.services.engagement import looks_like_disconnect_error
from src.utils import is_fatal_session_error
from tests.fakes import (
    FakeEvent,
    FakeMessage,
    FakeTelegramClient,
    build_stack,
    drain_executor,
    make_channel,
    session_stack,
)

USER = 4242
CH = 700700


# ---------------------------------------------------------------------------
class TestSignInRetries:
    """Wrong OTP / 2FA must be retryable (3 tries), not a full restart."""

    @pytest.fixture
    def handler(self, mock_tbot, mock_telegram_client):
        from src.client import AccountHandler

        instance = AccountHandler(mock_tbot)
        instance.finalize_client_setup = AsyncMock()
        return instance

    @pytest.mark.asyncio
    async def test_wrong_code_keeps_the_client(self, handler, mock_tbot, mock_event,
                                               mock_telegram_client):
        mock_event.message.text = "00000"
        mock_tbot.handlers.update({"temp_client": mock_telegram_client,
                                   "temp_phone": "+15550000001"})
        mock_telegram_client.sign_in = AsyncMock(
            side_effect=PhoneCodeInvalidError(request=Mock())
        )
        await handler.code_handler(mock_event)

        assert mock_tbot.handlers.get("temp_client") is mock_telegram_client
        assert mock_tbot.handlers.get("temp_phone") == "+15550000001"
        assert mock_tbot.handlers.get("temp_attempts") == 1
        sent = [str(call) for call in mock_tbot.tbot.send_message.call_args_list]
        assert any("2" in text and "attempt" in text for text in sent), sent

    @pytest.mark.asyncio
    async def test_third_wrong_code_cancels_the_flow(self, handler, mock_tbot, mock_event,
                                                    mock_telegram_client):
        mock_event.message.text = "00000"
        mock_tbot.handlers.update({"temp_client": mock_telegram_client,
                                   "temp_phone": "+15550000013",
                                   "temp_attempts": 2})
        mock_telegram_client.sign_in = AsyncMock(
            side_effect=PhoneCodeInvalidError(request=Mock())
        )
        await handler.code_handler(mock_event)

        assert "temp_client" not in mock_tbot.handlers
        sent = " ".join(str(call) for call in mock_tbot.tbot.send_message.call_args_list)
        assert "attempts used" in sent.lower() or "cancelled" in sent.lower()

    @pytest.mark.asyncio
    async def test_correct_code_after_a_wrong_one_still_works(self, handler, mock_tbot,
                                                             mock_event, mock_telegram_client):
        mock_event.message.text = "12345"
        mock_tbot.handlers.update({"temp_client": mock_telegram_client,
                                   "temp_phone": "+15550000014",
                                   "temp_attempts": 1})
        mock_telegram_client.sign_in = AsyncMock()
        mock_telegram_client.is_user_authorized = AsyncMock(return_value=True)

        await handler.code_handler(mock_event)

        handler.finalize_client_setup.assert_awaited()
        assert mock_tbot.handlers.get("temp_attempts") == 0

    @pytest.mark.asyncio
    async def test_wrong_2fa_password_gets_three_tries(self, handler, mock_tbot, mock_event,
                                                      mock_telegram_client):
        mock_event.message.text = "wrong-password"
        mock_tbot.handlers.update({"temp_client": mock_telegram_client,
                                   "temp_phone": "+15550000015"})
        mock_telegram_client.sign_in = AsyncMock(
            side_effect=PasswordHashInvalidError(request=Mock())
        )
        await handler.password_handler(mock_event)
        assert mock_tbot.handlers.get("temp_client") is mock_telegram_client
        assert mock_tbot.handlers.get("temp_pw_attempts") == 1

        mock_tbot.handlers["temp_pw_attempts"] = 2
        await handler.password_handler(mock_event)
        assert "temp_client" not in mock_tbot.handlers


class TestFatalSessionClassifier:
    """Only bans/blocks/revoked sessions may drop an account."""

    def test_connection_problems_are_not_fatal(self):
        for message in (
            "Cannot send requests while disconnected",
            "sqlite3.OperationalError: database is locked",
            "An invalid Peer was used. Make sure to pass the right peer type",
            "The invite link is invalid or has expired",
            "ConnectionError: Server closed the connection",
        ):
            assert is_fatal_session_error(RuntimeError(message)) is False, message

    def test_ban_and_revoke_are_fatal(self):
        assert is_fatal_session_error(UserDeactivatedBanError(request=Mock())) is True
        assert is_fatal_session_error(SessionRevokedError(request=Mock())) is True
        assert is_fatal_session_error(RuntimeError("The phone number is banned")) is True
        assert is_fatal_session_error(RuntimeError("this account was deactivated")) is True

    def test_disconnect_errors_are_retried_by_the_batch_runner(self):
        assert looks_like_disconnect_error(RuntimeError("Cannot send requests while disconnected"))
        assert looks_like_disconnect_error(RuntimeError("database is locked"))
        assert not looks_like_disconnect_error(ValueError("Target message is not a post"))


class TestPoolSelfHealing:
    @pytest.mark.asyncio
    async def test_a_dropped_account_is_reconnected(self):
        stack = await session_stack(2)
        dropped = next(name for name in stack.bot.active_clients if not name.startswith("phone"))
        client = stack.bot.active_clients[dropped]
        await client.disconnect()                     # the sqlite-lock crash
        assert client.is_connected() is False

        stats = await stack.registry.revive_disconnected()

        assert client.is_connected() is True
        assert stats["reconnected"] >= 1
        assert dropped in stack.bot.active_clients

    @pytest.mark.asyncio
    async def test_a_banned_account_is_dropped_and_the_admin_told(self):
        stack = await session_stack(1)
        name = next(iter(stack.bot.active_clients))
        client = stack.bot.active_clients[name]
        await client.disconnect()

        async def _refuse():
            raise UserDeactivatedBanError(request=Mock())

        client.connect = _refuse
        stats = await stack.registry.revive_disconnected()

        assert stats["revoked"] == 1
        assert name not in stack.bot.active_clients
        notices = [m["text"] for m in stack.bot.tbot.sent_messages if "logged out" in m["text"].lower()]
        assert notices, [m["text"] for m in stack.bot.tbot.sent_messages]


class TestPlanUnlocksEverything:
    @pytest.mark.asyncio
    async def test_a_granted_plan_announces_details_and_duration(self):
        stack = await build_stack({})
        plan = await stack.users.grant_plan(USER, kinds=["all"], days=30)
        assert plan["type"] == "premium"

        notes = [m["text"] for m in stack.bot.tbot.sent_messages
                 if "plan is now active" in m["text"]]
        assert notes, [m["text"] for m in stack.bot.tbot.sent_messages]
        assert "30 day" in notes[0]
        assert "comments" in notes[0].lower()

    @pytest.mark.asyncio
    async def test_plan_covered_orders_are_not_charged_again(self):
        stack = await build_stack({})
        await stack.users.credit(USER, 500, "topup")
        await stack.users.grant_plan(USER, kinds=["views", "comments"], days=30)

        order = await stack.orders.create_order(
            USER, "comment", {"link": "https://t.me/c/1/1", "text": "nice", "accounts": 5},
            plan={"plan_id": "comment_100", "title": "Comments", "quantity": 5, "price": 149.0},
        )
        assert float(order["price"]) == 0.0
        assert order["paid_with"] == "plan"
        assert await stack.users.balance(USER) == 500

    @pytest.mark.asyncio
    async def test_an_uncovered_service_is_still_charged(self):
        stack = await build_stack({})
        await stack.users.credit(USER, 500, "topup")
        await stack.users.grant_plan(USER, kinds=["reactions"], days=30)

        order = await stack.orders.create_order(
            USER, "comment", {"link": "https://t.me/c/1/1", "text": "nice", "accounts": 5},
            plan={"plan_id": "comment_100", "title": "Comments", "quantity": 5, "price": 149.0},
        )
        assert float(order["price"]) == 149.0
        assert await stack.users.balance(USER) == 351.0

    @pytest.mark.asyncio
    async def test_admin_and_owner_are_never_charged(self):
        from src import config as app_config

        stack = await build_stack({})
        admin_id = 800000005
        original = list(getattr(app_config, "ADMIN_IDS", []) or [])
        app_config.ADMIN_IDS = [admin_id]
        try:
            order = await stack.orders.create_order(
                admin_id, "views", {"link": "https://t.me/c/1/1", "views": 1000},
                plan={"plan_id": "views_1k", "title": "1k views", "quantity": 1000,
                      "price": 79.0},
            )
            assert float(order["price"]) == 0.0
            assert order["paid_with"] == "owner"
        finally:
            app_config.ADMIN_IDS = original

    @pytest.mark.asyncio
    async def test_granting_a_plan_switches_channel_automation_back_on(self):
        stack = await build_stack({})
        await stack.channels.register(-100800000001, USER, title="Promo")
        await stack.settings.set_path("views.auto.enabled", True)

        await stack.users.grant_plan(USER, kinds=["views", "comments"], days=30)

        channel = await stack.channels.get(-100800000001)
        assert (channel.get("auto_views") or {}).get("enabled") is True
        assert (channel.get("auto_comments") or {}).get("enabled") is True


class TestAutoComments:
    def _stack(self, accounts=3, *, texts=None, entitled=True):
        async def build():
            channel = make_channel(CH)
            clients = {
                f"phone{index}": FakeTelegramClient(
                    f"phone{index}", user_id=8000 + index,
                    channels={CH: channel}, member_of={CH},
                )
                for index in range(accounts)
            }
            stack = await build_stack(clients)
            await stack.registry.sync_from_live_clients()
            await stack.channels.register(-100800000002, USER,
                                          title="Comments Test", invite_link="https://t.me/+inv")
            if texts:
                await stack.settings.set_path("comments.auto.texts", texts)
            await stack.settings.set_path("comments.auto.enabled", True)
            if entitled:
                await stack.users.grant_plan(USER, kinds=["comments"], days=30)
            await stack.channels.set_auto_comments(-100800000002, True, accounts=3)
            return stack
        return build

    @pytest.mark.asyncio
    async def test_random_positive_comments_are_posted(self):
        stack = await self._stack()()
        result = await stack.auto.handle_new_post(-100800000002, 5)
        assert result["comments"]["ok"] >= 1, result

        posted = [
            message["text"]
            for client in stack.bot.active_clients.values()
            for message in client.sent_messages
            if message.get("comment_to") == 5
        ]
        assert posted, [c.sent_messages for c in stack.bot.active_clients.values()]
        assert all(isinstance(text, str) and text.strip() for text in posted)

    @pytest.mark.asyncio
    async def test_without_a_plan_nothing_is_posted(self):
        stack = await self._stack(entitled=False)()
        result = await stack.auto.handle_new_post(-100800000002, 6)
        assert "comments" not in result
        assert not any(
            message.get("comment_to") == 6
            for client in stack.bot.active_clients.values()
            for message in client.sent_messages
        )


class TestAutoViews:
    @pytest.mark.asyncio
    async def test_the_views_flag_is_not_switched_off_when_no_plan_exists(self):
        channel = make_channel(CH)
        clients = {
            "phone1": FakeTelegramClient("phone1", user_id=8001, channels={CH: channel},
                                         member_of={CH}),
        }
        stack = await build_stack(clients)
        await stack.channels.register(-100800000002, USER, title="Promo", invite_link="https://t.me/+inv")
        await stack.registry.sync_from_live_clients()
        await stack.settings.set_path("views.auto.enabled", True)
        await stack.channels.set_auto_views(-100800000002, True)

        outcome = await stack.auto.handle_new_post(-100800000002, 7)

        assert "views" not in outcome
        channel_doc = await stack.channels.get(-100800000002)
        assert (channel_doc.get("auto_views") or {}).get("enabled") is True, channel_doc

    @pytest.mark.asyncio
    async def test_views_run_once_somebody_has_a_plan(self):
        channel = make_channel(CH)
        clients = {
            "phone1": FakeTelegramClient("phone1", user_id=8001, channels={CH: channel},
                                         member_of={CH}),
        }
        stack = await build_stack(clients)
        await stack.channels.register(-100800000002, USER, title="Promo", invite_link="https://t.me/+inv")
        await stack.registry.sync_from_live_clients()
        await stack.settings.set_path("views.auto.enabled", True)
        await stack.channels.set_auto_views(-100800000002, True)
        await stack.users.grant_plan(USER, kinds=["views"], days=30)

        outcome = await stack.auto.handle_new_post(-100800000002, 8)
        assert outcome.get("views", {}).get("ok", 0) >= 1, outcome


class TestAutoJoinStaysInTheLogs:
    @pytest.mark.asyncio
    async def test_warm_up_does_not_post_into_the_chat(self, caplog):
        channel = make_channel(CH)
        clients = {
            f"phone{index}": FakeTelegramClient(
                f"phone{index}", user_id=8000 + index, channels={CH: channel},
                member_of=set(),
            )
            for index in range(2)
        }
        stack = await build_stack(clients)
        await stack.channels.register(-100800000002, USER, title="Promo", invite_link="https://t.me/+inv")
        await stack.registry.sync_from_live_clients()
        await stack.channels.set_auto_reactions(-100800000002, True)

        with caplog.at_level("INFO"):
            outcome = await stack.auto.warm_all_channels(reason="test", notify=True)

        assert outcome["joined"] >= 1, outcome
        chat_texts = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert not any("joined your channels" in text or "placed in your channels" in text
                       for text in chat_texts), chat_texts
        assert any("Auto-join finished" in record.message for record in caplog.records)


class TestPanelGodMode:
    @pytest.mark.asyncio
    async def test_admin_sees_every_feature_unlocked(self):
        from src import config as app_config

        stack = await build_stack({})
        admin_id = 800000006
        original_owner = getattr(app_config, "OWNER_ID", 0)
        original_admins = list(getattr(app_config, "ADMIN_IDS", []) or [])
        app_config.OWNER_ID = admin_id
        app_config.ADMIN_IDS = [admin_id]
        try:
            for feature in ("reactions", "views", "comments", "members", "channels"):
                assert await stack.users.has_feature(admin_id, feature) is True
            limits = await stack.users.effective_limits(admin_id)
            assert limits["unlimited"] is True
            assert limits["accounts"] > 1000
        finally:
            app_config.OWNER_ID = original_owner
            app_config.ADMIN_IDS = original_admins


# ---------------------------------------------------------------------------
# plan-covered orders & channel (re)attribution
# ---------------------------------------------------------------------------
class TestPlanCoveredOrderAttribution:
    @pytest.mark.asyncio
    async def test_panel_plan_quote_is_paid_with_plan(self):
        """A "0 — your plan pays for it" order must say *plan*, not *free*."""
        stack = await build_stack({})
        await stack.users.credit(USER, 500, "test")
        quote = {
            "plan_id": "plan_included", "title": "Comments • included in your plan",
            "quantity": 5, "accounts": 5, "price": 0.0,
            "covered_by_plan": True,
        }
        order = await stack.orders.create_order(USER, "comment", {"link": "x", "text": "nice"},
                                                plan=quote, quantity=5)
        assert float(order["price"]) == 0
        assert order["paid_with"] == "plan"
        assert await stack.users.balance(USER) == 500

    @pytest.mark.asyncio
    async def test_plan_covered_order_leaves_the_wallet_alone(self):
        stack = await build_stack({})
        await stack.users.credit(USER, 100, "test")
        await stack.users.grant_plan(USER, kinds=["all"], days=30, notify=False)
        order = await stack.orders.create_order(
            USER, "views", {"link": "https://t.me/c/800000007/5", "quantity": 1000},
            plan={"plan_id": "views_1k", "title": "1000 Views", "price": 79.0,
                  "quantity": 1000},
        )
        assert order["paid_with"] == "plan"
        assert float(order["price"]) == 0
        assert await stack.users.balance(USER) == 100


class TestChannelReattribution:
    @pytest.mark.asyncio
    async def test_a_real_adder_replaces_a_guessed_owner(self):
        stack = await build_stack({})
        guessed, real = 800000008, 800000009
        await stack.channels.register(-100800000002, guessed, title="Promo",
                                      owner_source="creator_guess")
        assert (await stack.channels.get(-100800000002))["owner_id"] == guessed

        await stack.channels.register(-100800000002, real, title="Promo",
                                      added_by=real, owner_source="added_by")
        doc = await stack.channels.get(-100800000002)
        assert doc["owner_id"] == real, doc

    @pytest.mark.asyncio
    async def test_an_explicit_owner_is_never_stolen(self):
        stack = await build_stack({})
        owner, stranger = 800000008, 800000009
        await stack.channels.register(-100800000002, owner, title="Promo",
                                      owner_source="added_by")
        await stack.channels.register(-100800000002, stranger, title="Promo",
                                      added_by=stranger, owner_source="added_by")
        assert (await stack.channels.get(-100800000002))["owner_id"] == owner

        # ...unless an operator explicitly reassigns it.
        await stack.channels.set_owner(-100800000002, stranger)
        doc = await stack.channels.get(-100800000002)
        assert doc["owner_id"] == stranger
        assert stranger in (doc.get("sponsors") or [])

    @pytest.mark.asyncio
    async def test_set_owner_makes_the_new_owner_entitled(self):
        stack = await build_stack({})
        await stack.channels.register(-100800000002, 800000008, title="Promo")
        await stack.channels.set_owner(-100800000002, USER)
        doc = await stack.channels.get(-100800000002)
        assert USER in (doc.get("sponsors") or [])
        assert await stack.auto._entitled(doc, "views") is False
        await stack.users.grant_plan(USER, kinds=["views"], days=30, notify=False)
        assert await stack.auto._entitled(doc, "views") is True


class TestSignInClientReuse:
    """The sqlite lock in the production log had one cause: two clients on one
    session file. The sign-in flow must reuse the client it already has."""

    @pytest.mark.asyncio
    async def test_same_phone_reuses_the_existing_sign_in_client(self, monkeypatch):
        import src.client as client_module

        created: list = []

        class _FakeSignInClient:
            def __init__(self, name):
                self.name = name
                self.connected = False

            def is_connected(self):
                return self.connected

            async def connect(self):
                self.connected = True

            async def disconnect(self):
                self.connected = False

        def _factory(name, api_id, api_hash):
            instance = _FakeSignInClient(name)
            created.append(instance)
            return instance

        monkeypatch.setattr(client_module, "TelegramClient", _factory)
        tbot = MagicMock()
        tbot.handlers = {}
        tbot.client_manager = None
        handler = client_module.AccountHandler(tbot)

        # ``_signin_client`` receives the sanitized session name (as the
        # handler does), and the same phone must reuse the same client.
        first = await handler._signin_client("800000010")
        second = await handler._signin_client("800000010")

        assert first is second, "the second attempt opened a second client on the same session file"
        assert len(created) == 1, created

    @pytest.mark.asyncio
    async def test_a_different_phone_closes_the_old_client_first(self, monkeypatch):
        import src.client as client_module
        import asyncio

        created: list = []

        class _FakeSignInClient:
            def __init__(self, name):
                self.name = name
                self.connected = False
                self.disconnected = False

            def is_connected(self):
                return self.connected

            async def connect(self):
                self.connected = True

            async def disconnect(self):
                self.connected = False
                self.disconnected = True

        def _factory(name, api_id, api_hash):
            instance = _FakeSignInClient(name)
            created.append(instance)
            return instance

        monkeypatch.setattr(client_module, "TelegramClient", _factory)
        tbot = MagicMock()
        tbot.handlers = {}
        tbot.active_clients = {}
        tbot.client_manager = None
        handler = client_module.AccountHandler(tbot)

        first = await handler._signin_client("800000010")
        second = await handler._signin_client("800000011")
        await asyncio.sleep(0)          # let the cleanup task run

        assert second is not first
        assert first.disconnected is True, "the abandoned sign-in client was left connected"


class TestProductionAttributionCase:
    """The exact production attribution bug: the channel belonged to one id
    (800000012) while another user (800000013) held the paid plan — so views
    were switched off for a channel somebody *had* paid for."""

    @pytest.mark.asyncio
    async def test_the_payer_becomes_a_sponsor_and_views_run(self):
        channel = make_channel(CH)
        clients = {
            "phone1": FakeTelegramClient("phone1", user_id=8001, channels={CH: channel},
                                         member_of={CH}),
        }
        stack = await build_stack(clients)
        stranger, payer = 800000013, 800000014
        await stack.channels.register(-100800000002, stranger, title="Promo",
                                      invite_link="https://t.me/+inv")
        await stack.registry.sync_from_live_clients()
        await stack.settings.set_path("views.auto.enabled", True)
        await stack.channels.set_auto_views(-100800000002, True)

        # Nobody entitled yet: the channel owner has no plan.
        assert "views" not in await stack.auto.handle_new_post(-100800000002, 20)

        # The paying user gets a plan and orders views for that channel.
        await stack.users.credit(payer, 500, "test")
        await stack.users.grant_plan(payer, kinds=["views"], days=30, notify=False)
        order = await stack.orders.create_order(
            payer, "views",
            {"channel_id": -100800000002, "link": "https://t.me/c/700700/5", "views": 5},
            plan={"plan_id": "views_1k", "title": "100 Views", "price": 0.0,
                  "quantity": 5},
        )
        await stack.executor.run(order["_id"])

        doc = await stack.channels.get(-100800000002)
        assert payer in (doc.get("sponsors") or []), doc
        assert doc["owner_id"] == stranger, "the channel owner must not change"

        # Now the very next post gets views — without any manual toggle.
        outcome = await stack.auto.handle_new_post(-100800000002, 21)
        assert outcome.get("views", {}).get("ok", 0) >= 1, outcome


class TestAccountStatusHasOneSourceOfTruth:
    """An account is only ever "inactive" after a ban/block, and that fact lives
    in the registry (MongoDB) — the legacy ``config['inactive_accounts']`` dict
    is read-only history, never written."""

    @pytest.mark.asyncio
    async def test_the_revoked_list_is_read_from_the_registry(self):
        from src.client import SessionManager
        from src.services.accounts import get_account_registry

        stack = await build_stack({})
        registry = get_account_registry(stack.registry.store)
        saved = await registry.store.save_signed_in(
            None, "+15550000016", owner_id=USER,
            session_string="1BVtsOK4notreal", me=types.SimpleNamespace(
                id=7001, username="banned_user", phone="+15550000017",
                first_name="Banned", last_name="User"),
        )
        await registry.store.set_status(saved["_id"], "revoked", "banned by Telegram")

        config = {"clients": {}}                # no legacy key at all
        manager = SessionManager(config, {}, stack.bot)

        event = FakeEvent(sender_id=USER, chat_id=USER)
        await manager.show_inactive_accounts(event)

        text = event.last_text or ""
        assert "+15550000016" in text, text
        assert "banned" in text.lower(), text
        assert "inactive_accounts" not in config, "the legacy key must not be created"

    @pytest.mark.asyncio
    async def test_an_authorized_account_clears_its_revoked_status(self):
        from src.client import SessionManager
        from src.services.accounts import get_account_registry

        stack = await build_stack({})
        registry = get_account_registry(stack.registry.store)
        saved = await registry.store.save_signed_in(
            None, "+15550000019", session_string="1BVtsOK4notreal",
            me=types.SimpleNamespace(id=7002, username="back", phone="+15550000020",
                                     first_name="Back", last_name="Again"),
        )
        await registry.store.set_status(saved["_id"], "revoked", "was banned")

        session_name = saved["phone"].lstrip("+")
        config = {"clients": {f"{session_name}.session": {}},
                  "inactive_accounts": {session_name: {"reason": "auth_error"}}}
        manager = SessionManager(config, {}, stack.bot)

        await manager._mark_active_in_registry(session_name)
        doc = await registry.store.get(saved["_id"])
        assert doc["status"] == "active", doc
