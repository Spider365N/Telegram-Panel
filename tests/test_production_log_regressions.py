"""Regressions for the three bugs visible in a real production log.

The log (Railway, 2026-10-15) showed:

    [ERROR] Could not read settings from store: name 'get_store_sync' is not defined
    [INFO]  Channel registered: -100800000001 (Logs Test) owner=None
    [INFO]  Bot added to chat -100800000001 by None
    [INFO]  Auto reactions on -100800000001/28: 0 ok, 0 failed (credits left: -1)

…i.e. settings never came from the database, a channel added by a user (whose id
Telegram did not report) stayed invisible in the panel, and automation reported a
silent "success" while the account pool was empty.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from tests.fakes import (
    FakeEvent,
    FakeTelegramClient,
    FakeUser,
    build_stack,
    drain_executor,
    make_channel,
)

CHANNEL = 800000001          # the channel id from the production log
INTERNAL = int(f"-100{CHANNEL}")


class ChannelActionEvent:
    """A ChatAction-style event: the bot was added to a channel."""

    def __init__(self, *, actor_id=None, added_by=None, action_from=None,
                 channel_id=CHANNEL, title="Logs Test"):
        self.user_added = True
        self.user_kicked = False
        self.chat_id = channel_id
        self.user_id = BOT_ID                 # the user that joined == the bot
        self.sender_id = actor_id             # None in the failing deployment
        self.added_by = added_by if added_by is not None else actor_id
        self.action_message = (
            type("Msg", (), {"from_id": FakeUser(action_from), "sender_id": action_from})()
            if action_from else None
        )
        self._chat = make_channel(channel_id, title=title)

    async def get_chat(self):
        return self._chat


BOT_ID = 424242


@pytest_asyncio.fixture
async def stack():
    transport = FakeTelegramClient("bot", user_id=BOT_ID)
    built = await build_stack({}, bot_client=transport)
    yield built


# ---------------------------------------------------------------------------
class TestSettingsAreReadFromTheStore:
    @pytest.mark.asyncio
    async def test_service_without_an_explicit_store_still_finds_the_database(self, stack):
        """The production bug: `get_store_sync` was not imported, so every read
        silently fell back to the built-in defaults."""
        from src.services.settings import get_settings_service, reset_settings_service

        # a value only present in the store — not in DEFAULT_SETTINGS
        await stack.settings.set_path("payments.upi_id", "store-only@upi")
        await stack.settings.set_path("reactions.free_account_limit", 7)

        reset_settings_service()
        service = get_settings_service()          # no store argument (production path)
        assert service._store is None
        settings = await service.get(refresh=True)
        assert settings["payments"]["upi_id"] == "store-only@upi"
        assert settings["reactions"]["free_account_limit"] == 7

    @pytest.mark.asyncio
    async def test_settings_edits_survive_a_fresh_service_instance(self, stack):
        """An admin price edit must be visible to a newly created service."""
        from src.services.settings import get_settings_service, reset_settings_service

        plans = await stack.settings.plans("reactions")
        for plan in plans:
            if plan.get("id") == "react_50":
                plan["price"] = 59
        await stack.settings.update_section("reactions", {"plans": plans})
        reset_settings_service()
        plan = await get_settings_service().get_plan("reactions", "react_50")
        assert plan["price"] == 59

    @pytest.mark.asyncio
    async def test_no_store_error_is_logged(self, stack, caplog):
        from src.services.settings import get_settings_service, reset_settings_service
        reset_settings_service()
        service = get_settings_service()
        with caplog.at_level("ERROR"):
            await service.get(refresh=True)
        assert "get_store_sync" not in caplog.text
        assert "Could not read settings from store" not in caplog.text


# ---------------------------------------------------------------------------
class TestChannelRegistration:
    @pytest.mark.asyncio
    async def test_sender_id_missing_falls_back_to_added_by(self, stack, monkeypatch):
        """`Bot added to chat … by None` must not leave the channel unowned."""
        import src.telbot as telbot_module

        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        event = ChannelActionEvent(actor_id=None, added_by=777001)

        await telbot_module.TelegramBot._chat_action_handler(bot, event)
        doc = await stack.channels.get(INTERNAL)
        assert doc is not None
        assert doc["owner_id"] == 777001, doc

    @pytest.mark.asyncio
    async def test_actor_only_on_the_action_message(self, stack):
        import src.telbot as telbot_module

        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        event = ChannelActionEvent(actor_id=None, added_by=None, action_from=777002)

        await telbot_module.TelegramBot._chat_action_handler(bot, event)
        doc = await stack.channels.get(INTERNAL)
        assert doc["owner_id"] == 777002

    @pytest.mark.asyncio
    async def test_bot_never_becomes_the_owner(self, stack):
        import src.telbot as telbot_module

        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        # only the "user_id" of the added user is available — that is the bot
        event = ChannelActionEvent(actor_id=None, added_by=None)
        # with the real bot id known, the "added user" (= the bot) is ignored
        assert telbot_module.TelegramBot._event_actor_id(event, BOT_ID) == 0
        # and when a different user is the actor, that user wins
        user_event = ChannelActionEvent(actor_id=555001, added_by=None)
        assert telbot_module.TelegramBot._event_actor_id(user_event, BOT_ID) == 555001

    @pytest.mark.asyncio
    async def test_unknown_actor_notifies_the_admins(self, stack, monkeypatch):
        import src.telbot as telbot_module

        sent = []

        async def fake_to_admins(text, buttons=None):
            sent.append(text)
            return 1

        monkeypatch.setattr(stack.notifier, "to_admins", fake_to_admins)
        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        await telbot_module.TelegramBot._chat_action_handler(bot, ChannelActionEvent())

        assert sent, "admins were never told about the new channel"
        assert "Channel linked" in sent[0]
        assert "assign" in sent[0].lower() or "could not tell" in sent[0].lower()

    @pytest.mark.asyncio
    async def test_owner_notice_mentions_missing_accounts(self, stack, monkeypatch):
        import src.telbot as telbot_module

        sent = []
        monkeypatch.setattr(stack.notifier, "to_user",
                            lambda uid, text, buttons=None: sent.append(text) or True)
        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        await telbot_module.TelegramBot._chat_action_handler(
            bot, ChannelActionEvent(actor_id=777003))

        assert sent and "No accounts are signed in" in sent[0]

    @pytest.mark.asyncio
    async def test_owner_notice_mentions_the_pool_when_accounts_exist(self, stack, monkeypatch):
        import src.telbot as telbot_module
        from tests.fakes import FakeTelegramClient as FTC

        stack.bot.active_clients.update({
            f"phone{i}": FTC(f"phone{i}", user_id=900 + i) for i in range(3)
        })
        await stack.registry.sync_from_live_clients()

        sent = []
        monkeypatch.setattr(stack.notifier, "to_user",
                            lambda uid, text, buttons=None: sent.append(text) or True)
        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        await telbot_module.TelegramBot._chat_action_handler(
            bot, ChannelActionEvent(actor_id=777004))
        assert sent and "3 account" in sent[0]


class TestUnownedChannelIsVisible:
    @pytest.mark.asyncio
    async def test_admin_panel_lists_a_channel_without_an_owner(self, stack, monkeypatch):
        import src.config as config_module

        admin = 123456
        monkeypatch.setattr(config_module, "OWNER_ID", admin, raising=False)
        monkeypatch.setattr(config_module, "ADMIN_ID", admin, raising=False)
        await stack.channels.register(INTERNAL, None, title="Logs Test")

        event = FakeEvent(sender_id=admin, chat_id=admin, data="u:ch")
        stack.router._cache.ttl = 0.0
        await stack.router.handle_callback(event)
        assert "Logs Test" in event.last_text, event.last_text

    @pytest.mark.asyncio
    async def test_regular_user_still_only_sees_their_own(self, stack):
        await stack.channels.register(INTERNAL, None, title="Logs Test")
        mine = -100800000002
        await stack.channels.register(mine, 616161, title="My own channel")

        event = FakeEvent(sender_id=616161, chat_id=616161, data="u:ch")
        stack.router._cache.ttl = 0.0
        await stack.router.handle_callback(event)
        assert "My own channel" in event.last_text
        assert "Logs Test" not in event.last_text


# ---------------------------------------------------------------------------
class TestEmptyPoolIsReported:
    @pytest.mark.asyncio
    async def test_auto_reactions_warn_the_owner_instead_of_failing_silently(
            self, stack, monkeypatch):
        notices = []

        async def capture(chat_id, text, buttons=None, parse_mode=None):
            notices.append((chat_id, text))
            return True

        monkeypatch.setattr(stack.notifier, "send", capture)
        await stack.channels.register(INTERNAL, 777005, title="Logs Test",
                                      invite_link="https://t.me/+logs")
        await stack.auto.enable_free_plan(INTERNAL)

        outcome = await stack.auto.handle_new_post(INTERNAL, 28)
        assert outcome["reactions"]["ok"] == 0
        assert notices, "owner was not warned about the empty account pool"
        assert "No accounts are signed in" in notices[0][1]

    @pytest.mark.asyncio
    async def test_the_warning_is_not_repeated_for_every_post(self, stack, monkeypatch):
        notices = []

        async def capture(chat_id, text, buttons=None, parse_mode=None):
            notices.append(text)
            return True

        monkeypatch.setattr(stack.notifier, "send", capture)
        await stack.channels.register(INTERNAL, 777006, title="Logs Test")
        await stack.auto.enable_free_plan(INTERNAL)
        for message_id in range(1, 4):
            await stack.auto.handle_new_post(INTERNAL, message_id)
        assert len(notices) == 1

    @pytest.mark.asyncio
    async def test_warning_goes_to_admins_for_an_unowned_channel(self, stack, monkeypatch):
        notices = []
        monkeypatch.setattr(stack.notifier, "to_admins",
                            lambda text, buttons=None: notices.append(text) or 1)
        await stack.channels.register(INTERNAL, None, title="Logs Test")
        await stack.auto.enable_free_plan(INTERNAL)
        await stack.auto.handle_new_post(INTERNAL, 1)
        assert notices

    @pytest.mark.asyncio
    async def test_with_accounts_reactions_actually_happen(self, stack, monkeypatch):
        from tests.fakes import FakeTelegramClient as FTC

        clients = {
            f"phone{i}": FTC(f"phone{i}", user_id=900 + i,
                             channels={CHANNEL: make_channel(CHANNEL)})
            for i in range(4)
        }
        stack.bot.active_clients.update(clients)
        await stack.registry.sync_from_live_clients()

        await stack.channels.register(INTERNAL, 777007, title="Logs Test")
        await stack.auto.enable_free_plan(INTERNAL)
        outcome = await stack.auto.handle_new_post(INTERNAL, 28)
        assert outcome["reactions"]["ok"] == 4, outcome


# ---------------------------------------------------------------------------
class TestOwnerCommandScope:
    @pytest.mark.asyncio
    async def test_owner_scope_is_applied_once_the_peer_is_resolvable(self):
        """`owner: False` in the log only means the owner had not written yet."""
        from src.commands import register_commands

        class Client:
            def __init__(self, resolvable=True):
                self.scopes = []
                self.resolvable = resolvable

            async def get_input_entity(self, user_id):
                if not self.resolvable:
                    raise ValueError("Could not find the input entity for PeerUser")
                return type("Peer", (), {"user_id": user_id})()

            async def __call__(self, request):
                self.scopes.append(type(request.scope).__name__)
                return True

        client = Client(resolvable=True)
        result = await register_commands(client, owner_id=800000006)
        assert result["owner"] is True
        assert "BotCommandScopePeerUser" in client.scopes

    @pytest.mark.asyncio
    async def test_unresolvable_owner_peer_only_skips_the_menu_not_startup(self):
        from src.commands import register_commands

        class Client:
            def __init__(self):
                self.scopes = []

            async def get_input_entity(self, user_id):
                raise ValueError("not in the session yet")

            async def __call__(self, request):
                self.scopes.append(type(request.scope).__name__)
                return True

        client = Client()
        result = await register_commands(client, owner_id=800000006)
        assert result["user"] is True and result["admin"] is True
        assert result["owner"] is False          # menu only; nothing breaks
        assert "BotCommandScopePeerUser" not in client.scopes

    @pytest.mark.asyncio
    async def test_registration_never_raises_when_both_scopes_fail(self):
        from src.commands import register_commands

        class Client:
            async def __call__(self, request):
                raise ValueError("nope")

        result = await register_commands(Client(), owner_id=1)
        assert isinstance(result, dict) and result["owner"] is False


# ---------------------------------------------------------------------------
class TestClaimingAnUnownedChannel:
    @pytest.mark.asyncio
    async def test_admin_can_claim_an_unowned_channel(self, stack, monkeypatch):
        import src.config as config_module

        admin = 123456
        monkeypatch.setattr(config_module, "OWNER_ID", admin, raising=False)
        monkeypatch.setattr(config_module, "ADMIN_ID", admin, raising=False)
        await stack.channels.register(INTERNAL, None, title="Logs Test")

        listed = FakeEvent(sender_id=admin, chat_id=admin, data="u:ch")
        stack.router._cache.ttl = 0.0
        await stack.router.handle_callback(listed)
        assert any("Assign" in label for label in listed.button_labels()), listed.button_labels()

        claim = FakeEvent(sender_id=admin, chat_id=admin, data=f"u:ch_claim:{INTERNAL}")
        await stack.router.handle_callback(claim)
        doc = await stack.channels.get(INTERNAL)
        assert doc["owner_id"] == admin
        assert "MY CHANNELS" in claim.last_text.upper()

    @pytest.mark.asyncio
    async def test_a_user_cannot_steal_someone_elses_channel(self, stack, monkeypatch):
        await stack.channels.register(INTERNAL, 4242, title="Not yours")
        claim = FakeEvent(sender_id=616161, chat_id=616161, data=f"u:ch_claim:{INTERNAL}")
        stack.router._cache.ttl = 0.0
        await stack.router.handle_callback(claim)
        doc = await stack.channels.get(INTERNAL)
        assert doc["owner_id"] == 4242
        alerts = [a["text"] for a in claim.answers if a.get("alert")]
        assert alerts and "another user" in alerts[-1].lower()

    @pytest.mark.asyncio
    async def test_claim_requires_a_real_channel(self, stack):
        event = FakeEvent(sender_id=616161, chat_id=616161, data="u:ch_claim:999999")
        stack.router._cache.ttl = 0.0
        await stack.router.handle_callback(event)
        alerts = [a["text"] for a in event.answers if a.get("alert")]
        assert alerts and "not found" in alerts[-1].lower()


# ---------------------------------------------------------------------------
class TestFullJourneyFromTheLog:
    """The exact sequence from the production log, end to end."""

    @pytest.mark.asyncio
    async def test_channel_added_with_unknown_actor_still_ends_with_reactions(
            self, stack, monkeypatch):
        import src.config as config_module
        import src.telbot as telbot_module
        from tests.fakes import FakeTelegramClient as FTC

        admin = 800000006            # the owner id from the deployment
        monkeypatch.setattr(config_module, "OWNER_ID", admin, raising=False)
        monkeypatch.setattr(config_module, "ADMIN_ID", admin, raising=False)

        # 1) the bot is added to a private channel; Telegram reports no actor
        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        await telbot_module.TelegramBot._chat_action_handler(bot, ChannelActionEvent())
        doc = await stack.channels.get(INTERNAL)
        assert doc is not None and not doc.get("owner_id")

        # 2) the operator opens the panel and sees it (this used to be empty)
        listed = FakeEvent(sender_id=admin, chat_id=admin, data="u:ch")
        stack.router._cache.ttl = 0.0
        await stack.router.handle_callback(listed)
        assert "Logs Test" in listed.last_text

        # 3) …and assigns it to themselves
        claim = FakeEvent(sender_id=admin, chat_id=admin, data=f"u:ch_claim:{INTERNAL}")
        await stack.router.handle_callback(claim)
        assert (await stack.channels.get(INTERNAL))["owner_id"] == admin

        # 4) accounts are signed in and reachable by the pool
        stack.bot.active_clients.update({
            f"phone{i}": FTC(f"phone{i}", user_id=900 + i,
                             channels={CHANNEL: make_channel(CHANNEL)})
            for i in range(5)
        })
        await stack.registry.sync_from_live_clients()
        assert await stack.registry.available_count() == 5

        # 5) a new post arrives → reactions really happen
        outcome = await stack.auto.handle_new_post(INTERNAL, 28)
        assert outcome["reactions"]["ok"] == 5, outcome
        assert outcome["reactions"]["failed"] == 0
        refreshed = await stack.channels.get(INTERNAL)
        assert refreshed["stats"]["posts_seen"] >= 1
        assert refreshed["stats"]["reactions_sent"] == 5

    @pytest.mark.asyncio
    async def test_settings_edited_by_the_admin_are_used_by_the_engine(self, stack):
        """The settings NameError also froze the admin's price/limit edits."""
        await stack.settings.set_path("reactions.auto.accounts_per_post", 3)
        from src.services.settings import get_settings_service, reset_settings_service

        reset_settings_service()
        service = get_settings_service()          # production path, no store arg
        cfg = await service.section("reactions")
        assert cfg["auto"]["accounts_per_post"] == 3

        from tests.fakes import FakeTelegramClient as FTC
        stack.bot.active_clients.update({
            f"phone{i}": FTC(f"phone{i}", user_id=800 + i,
                             channels={CHANNEL: make_channel(CHANNEL)})
            for i in range(6)
        })
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(INTERNAL, 800000007, title="Logs Test")
        await stack.auto.enable_free_plan(INTERNAL)
        outcome = await stack.auto.handle_new_post(INTERNAL, 1)
        assert outcome["reactions"]["ok"] == 3, outcome


# ---------------------------------------------------------------------------
class TestInviteLinkRecovery:
    """A private channel is useless for reactions without an invite link."""

    @pytest.mark.asyncio
    async def test_invite_is_retried_on_a_later_post(self, stack, monkeypatch):
        import src.telbot as telbot_module

        internal = -100800000003
        await stack.channels.register(internal, 555001, title="Private channel")
        assert not (await stack.channels.get(internal)).get("invite_link")

        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot

        calls = []

        async def fake_export(channel_id):
            calls.append(channel_id)
            return "https://t.me/+freshInvite"

        monkeypatch.setattr(telbot_module.TelegramBot, "_export_invite", staticmethod(fake_export))

        channel = make_channel(800000003, title="Private channel")
        channel.username = None
        await bot._refresh_invite_if_missing(internal, channel)

        assert calls == [800000003]
        doc = await stack.channels.get(internal)
        assert doc["invite_link"] == "https://t.me/+freshInvite"

    @pytest.mark.asyncio
    async def test_public_channels_are_left_alone(self, stack, monkeypatch):
        import src.telbot as telbot_module

        internal = -100800000004
        await stack.channels.register(internal, 555002, title="Public channel")

        called = []

        async def fake_export(channel_id):
            called.append(channel_id)
            return "https://t.me/+nope"

        monkeypatch.setattr(telbot_module.TelegramBot, "_export_invite", staticmethod(fake_export))
        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot

        channel = make_channel(800000004, title="Public channel")
        channel.username = "public_channel"
        await bot._refresh_invite_if_missing(internal, channel)
        assert called == []

    @pytest.mark.asyncio
    async def test_existing_links_are_not_replaced(self, stack, monkeypatch):
        import src.telbot as telbot_module

        internal = -100800000005
        await stack.channels.register(internal, 555003, title="Already linked",
                                      invite_link="https://t.me/+original")
        called = []

        async def fake_export(channel_id):
            called.append(channel_id)
            return "https://t.me/+different"

        monkeypatch.setattr(telbot_module.TelegramBot, "_export_invite", staticmethod(fake_export))
        bot = telbot_module.TelegramBot.__new__(telbot_module.TelegramBot)
        bot.tbot = stack.bot.tbot
        channel = make_channel(800000005, title="Already linked")
        channel.username = None
        await bot._refresh_invite_if_missing(internal, channel)
        assert called == []
        assert (await stack.channels.get(internal))["invite_link"] == "https://t.me/+original"
