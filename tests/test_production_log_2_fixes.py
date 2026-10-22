"""
The v3.2 fixes from the operator's second production log:

* every notification is rendered as HTML — the log's "Panel is online" and
  "Auto reactions could not be applied" messages showed literal ``</b>`` codes
  because the sends never asked Telegram for HTML;
* a reaction emoji outside Telegram's own set (``✅``) made a whole post fail
  with ``ReactionInvalidError`` — Telegram now gets a second emoji;
* the same post was reacted to twice because two handlers saw the update;
* a newly signed-in account must join the channels that already exist;
* removing the bot from a channel must make our accounts leave it.
"""
from __future__ import annotations

import pytest

from tests.fakes import FakeEvent, FakeTelegramClient, build_stack, drain_executor, make_channel

OWNER = 123456789
USER = 4242


def user_event(data=None, text="", sender_id=USER, message_id=5):
    return FakeEvent(sender_id=sender_id, chat_id=sender_id, data=data, text=text,
                     message_id=message_id)


async def channel_stack(accounts: int = 3, *, channel_id: int = 950001,
                        reaction_allowlist=None, invite_link="https://t.me/+prodInvite"):
    channel = make_channel(channel_id)                     # private channel
    clients = {
        f"phone{i+1}": FakeTelegramClient(
            f"phone{i+1}", user_id=8000 + i, channels={channel_id: channel},
            member_of=set(), reaction_allowlist=reaction_allowlist,
        )
        for i in range(accounts)
    }
    stack = await build_stack(clients)
    stack.router._cache.ttl = 0.0
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(int(f"-100{channel_id}"), USER, title="Logs Test",
                                  invite_link=invite_link)
    return stack, channel


@pytest.mark.asyncio
class TestNotificationsRenderHtml:
    async def test_the_rejection_alert_is_sent_as_html(self):
        stack, _ = await channel_stack(2, reaction_allowlist={"👍"})
        await stack.auto.enable_free_plan(-100950001)
        await stack.settings.set_path("reactions.emojis", ["🔥"])   # refused by the fake
        await stack.auto.handle_new_post(-100950001, 5)

        sent = stack.bot.tbot.sent_messages
        alerts = [m for m in sent if "Auto reactions could not be applied" in m["text"]]
        assert alerts, [m["text"][:60] for m in sent]
        for message in alerts:
            assert message.get("parse_mode") == "html", message
            assert "<b>" in message["text"] and "</b>" in message["text"]
            assert "reactions" in message["text"].lower()

    async def test_a_literal_emoji_is_never_left_unparsed(self):
        """Any notification carrying markup must ask for HTML parsing."""
        stack, _ = await channel_stack(1)
        await stack.notifier.to_user(USER, "🔔 <b>Heads up</b>\n\n<code>id</code>")
        await stack.notifier.to_admins("🔔 <b>Admin</b> notice")
        for message in stack.bot.tbot.sent_messages:
            assert message.get("parse_mode") == "html", message

    async def test_plain_text_notifications_stay_plain(self):
        stack, _ = await channel_stack(1)
        await stack.notifier.to_user(USER, "no markup here")
        message = stack.bot.tbot.sent_messages[-1]
        assert "parse_mode" not in message or message["parse_mode"] is None

    async def test_detect_parse_mode_helper(self):
        from src.utils import detect_parse_mode

        assert detect_parse_mode("<b>x</b>") == "html"
        assert detect_parse_mode("views < 100 and > 5") is None
        assert detect_parse_mode("plain") is None
        assert detect_parse_mode("<b>x</b>", "markdown") == "markdown"
        assert detect_parse_mode("plain", "html") == "html"


@pytest.mark.asyncio
class TestReactionEmojiIsAlwaysAccepted:
    async def test_a_refused_emoji_is_swapped_for_a_working_one(self):
        """Telegram's own set only: "✅" used to kill the whole post."""
        from src.services import engagement as engagement_module

        engagement_module.REJECTED_REACTION_EMOJIS.clear()
        stack, _ = await channel_stack(2, reaction_allowlist={"👍"})
        await stack.auto.enable_free_plan(-100950001)
        await stack.settings.set_path("reactions.emojis", ["🔥", "👍"])

        outcome = await stack.auto.handle_new_post(-100950001, 1)

        assert outcome["reactions"]["ok"] == outcome["reactions"]["accounts"], outcome
        assert outcome["reactions"]["failed"] == 0
        applied = [e for client in stack.bot.active_clients.values()
                   for e in client.applied_reactions]
        assert applied and set(applied) == {"👍"}, applied

    async def test_an_unsupported_emoji_is_filtered_before_it_is_ever_sent(self):
        """Stored "✅" (the production case) must never reach Telegram at all."""
        stack, _ = await channel_stack(1)
        await stack.auto.enable_free_plan(-100950001)
        await stack.settings.set_path("reactions.emojis", ["✅", "👍"])

        outcome = await stack.auto.handle_new_post(-100950001, 1)

        assert outcome["reactions"]["ok"] == outcome["reactions"]["accounts"]
        tried = [
            request.reaction[0].emoticon
            for client in stack.bot.active_clients.values()
            for request in client.requests
            if type(request).__name__ == "SendReactionRequest"
        ]
        assert tried and "✅" not in tried, tried
        assert await stack.settings.emojis() == ["👍"]      # filtered out of the pool

    async def test_a_channel_refused_emoji_is_remembered_and_never_retried(self, monkeypatch):
        import random as _random

        from src.services import engagement as engagement_module

        engagement_module.REJECTED_REACTION_EMOJIS.clear()
        engagement_module._ALERTED_REACTION_EMOJIS.clear()

        class _OrderedRandom:
            """Deterministic stand-in for the module's ``random``.

            ``send_reaction`` samples the candidate emojis randomly (that
            randomness *is* the product requirement), which made this test
            order-dependent: when 👍 happened to come first, 🔥 was never even
            tried and the assertion failed. Pinning the order keeps the test
            honest about the behaviour it checks.
            """

            def __getattr__(self, name):
                return getattr(_random, name)

            @staticmethod
            def sample(population, k):
                return list(population)[:k]

        monkeypatch.setattr(engagement_module, "random", _OrderedRandom())

        stack, _ = await channel_stack(2, reaction_allowlist={"👍"})
        await stack.auto.enable_free_plan(-100950001)
        # 🔥 is a valid Telegram reaction but this channel only accepts 👍
        await stack.settings.set_path("reactions.emojis", ["🔥", "👍"])
        await stack.auto.handle_new_post(-100950001, 1)
        assert "🔥" in engagement_module.REJECTED_REACTION_EMOJIS
        after_first = [e for client in stack.bot.active_clients.values()
                       for e in client.applied_reactions]
        assert after_first == ["👍", "👍"], after_first      # 🔥 was never accepted

        await stack.auto.handle_new_post(-100950001, 2)
        # the refused emoji is not even tried again on the next post
        attempts = [
            request.reaction[0].emoticon
            for client in stack.bot.active_clients.values()
            for request in client.requests
            if type(request).__name__ == "SendReactionRequest"
        ]
        assert attempts.count("🔥") == 1, attempts
        accepted = [e for client in stack.bot.active_clients.values()
                    for e in client.applied_reactions]
        assert accepted == ["👍"] * 4, accepted
        alerts = [m["text"] for m in stack.bot.tbot.sent_messages if "skipped" in m["text"].lower()]
        assert len(alerts) == 1, alerts

    async def test_every_emoji_refused_reports_the_real_reason(self):
        stack, _ = await channel_stack(1, reaction_allowlist={"🍓"})
        await stack.auto.enable_free_plan(-100950001)
        await stack.settings.set_path("reactions.emojis", ["👍", "🔥"])
        outcome = await stack.auto.handle_new_post(-100950001, 1)

        assert outcome["reactions"]["ok"] == 0
        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        alert = next((m for m in messages if "could not be applied" in m), "")
        assert "reaction" in alert.lower() and "emoji" in alert.lower(), alert

    async def test_the_settings_repair_writes_invalid_emojis_out(self):
        stack, _ = await channel_stack(1)
        await stack.settings.set_path("reactions.emojis", ["👍", "✅", "🚀"])
        kept = await stack.settings.repair_reaction_emojis()
        assert kept == ["👍"]
        stored = (await stack.settings.get(refresh=True))["reactions"]["emojis"]
        assert stored == ["👍"]


@pytest.mark.asyncio
class TestOneReactionPerPost:
    async def test_a_post_is_only_automated_once(self):
        stack, _ = await channel_stack(3)
        await stack.auto.enable_free_plan(-100950001)

        first = await stack.auto.handle_new_post(-100950001, 35)
        second = await stack.auto.handle_new_post(-100950001, 35)      # duplicate update

        assert first["reactions"]["ok"] > 0
        assert second.get("skipped") == "post already handled", second
        reactions = [
            type(request).__name__
            for client in stack.bot.active_clients.values()
            for request in client.requests
            if type(request).__name__ == "SendReactionRequest"
        ]
        assert len(reactions) == first["reactions"]["ok"], reactions

    async def test_an_older_redelivered_post_is_ignored(self):
        stack, _ = await channel_stack(3)
        await stack.auto.enable_free_plan(-100950001)
        await stack.auto.handle_new_post(-100950001, 40)
        again = await stack.auto.handle_new_post(-100950001, 39)       # restart redelivery
        assert again.get("skipped") == "post already handled"

    async def test_the_newest_post_still_runs(self):
        stack, _ = await channel_stack(3)
        await stack.auto.enable_free_plan(-100950001)
        await stack.auto.handle_new_post(-100950001, 40)
        fresh = await stack.auto.handle_new_post(-100950001, 41)
        assert "reactions" in fresh and fresh["reactions"]["ok"] > 0


@pytest.mark.asyncio
class TestNewAccountsJoinTheChannels:
    async def test_a_late_account_joins_the_channels_that_already_exist(self):
        channel = make_channel(960001)
        first = FakeTelegramClient("phone1", user_id=9001, channels={960001: channel},
                                   member_of=set())
        stack = await build_stack({"phone1": first})
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(-100960001, USER, title="Existing Channel",
                                      invite_link="https://t.me/+prodInvite")
        await stack.auto.enable_free_plan(-100960001)
        assert first.joined, "the first account should have joined during the warm-up"

        # …now a second account signs in (the operator's exact scenario)
        second = FakeTelegramClient("phone2", user_id=9002, channels={960001: channel},
                                    member_of=set())
        stack.bot.active_clients["phone2"] = second
        await stack.registry.sync_from_live_clients()
        outcome = await stack.auto.warm_all_channels(reason="test sign-in")

        assert outcome["joined"] >= 1, outcome
        assert second.joined, "the new account never joined the existing channel"
        assert 960001 in second.member_of

    async def test_public_channels_are_not_joined_by_new_accounts(self):
        channel = make_channel(960002, username="publiclogs")
        first = FakeTelegramClient("phone1", user_id=9001, channels={960002: channel},
                                   member_of=set())
        stack = await build_stack({"phone1": first})
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(-100960002, USER, title="Public", username="publiclogs",
                                      invite_link=None)
        await stack.auto.enable_free_plan(-100960002)

        second = FakeTelegramClient("phone2", user_id=9002, channels={960002: channel},
                                    member_of=set())
        stack.bot.active_clients["phone2"] = second
        await stack.registry.sync_from_live_clients()
        await stack.auto.warm_all_channels(reason="test sign-in")

        assert second.joined == [], "a public channel needs no membership"

    async def test_the_channel_without_automation_is_skipped(self):
        stack, _ = await channel_stack(1)
        outcome = await stack.auto.warm_all_channels(reason="test")
        assert outcome["channels"] == 0, outcome

    async def test_the_pool_completes_up_to_the_free_limit(self):
        """10 signed-in accounts must end up in the channel (free tier cap)."""
        channel = make_channel(960003)
        clients = {
            f"phone{i+1}": FakeTelegramClient(f"phone{i+1}", user_id=9100 + i,
                                              channels={960003: channel}, member_of=set())
            for i in range(12)
        }
        stack = await build_stack(clients)
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(-100960003, USER, title="Big Channel",
                                      invite_link="https://t.me/+prodInvite")
        await stack.auto.enable_free_plan(-100960003)

        members = [n for n, c in stack.bot.active_clients.items() if 960003 in c.member_of]
        assert len(members) == 10, f"free tier should join 10 of 12 accounts, got {members}"


@pytest.mark.asyncio
class TestRemovingTheBotClearsEverything:
    async def _removed_event(self, stack, channel):
        class RemovedEvent:
            user_added = False
            user_kicked = True
            user_id = 424242              # the bot's own id
            chat_id = 0

            async def get_chat(self_inner):
                return channel

        return RemovedEvent()

    async def test_accounts_leave_the_channel_and_the_record_is_dropped(self):
        from src.telbot import TelegramBot

        channel = make_channel(970001)
        clients = {
            f"phone{i+1}": FakeTelegramClient(f"phone{i+1}", user_id=9200 + i,
                                              channels={970001: channel}, member_of=set())
            for i in range(3)
        }
        stack = await build_stack(clients)
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(-100970001, USER, title="Leaving Soon",
                                      invite_link="https://t.me/+prodInvite")
        await stack.auto.enable_free_plan(-100970001)
        assert all(970001 in c.member_of for _, c in stack.bot.active_clients.items())

        bot = TelegramBot.__new__(TelegramBot)
        bot.tbot = FakeTelegramClient("bot", user_id=424242)
        bot.store = stack.store
        bot.active_clients = stack.bot.active_clients
        await bot._chat_action_handler(await self._removed_event(stack, channel))

        for name, client in stack.bot.active_clients.items():
            assert 970001 not in client.member_of, f"{name} is still inside the channel"
            assert client.left == [970001], name
        assert await stack.channels.get(-100970001) is None, "the channel record survived"

    async def test_the_owner_is_told_that_everything_stopped(self):
        from src.telbot import TelegramBot

        channel = make_channel(970002)
        client = FakeTelegramClient("phone1", user_id=9200, channels={970002: channel},
                                    member_of=set())
        stack = await build_stack({"phone1": client})
        await stack.registry.sync_from_live_clients()
        await stack.channels.register(-100970002, USER, title="Leaving Too",
                                      invite_link="https://t.me/+prodInvite")
        await stack.auto.enable_free_plan(-100970002)

        bot = TelegramBot.__new__(TelegramBot)
        bot.tbot = FakeTelegramClient("bot", user_id=424242)
        bot.store = stack.store
        bot.active_clients = stack.bot.active_clients
        await bot._chat_action_handler(await self._removed_event(stack, channel))

        notes = [m["text"] for m in stack.bot.tbot.sent_messages if "removed" in m["text"].lower()]
        assert notes, [m["text"][:70] for m in stack.bot.tbot.sent_messages]
        assert "accounts that left" in notes[-1]

    async def test_a_public_channel_only_loses_its_record(self):
        stack, _ = await channel_stack(1)
        public = FakeTelegramClient("phone1", user_id=9300,
                                    channels={950001: make_channel(950001, username="pubchan")},
                                    member_of=set())
        stack.bot.active_clients = {"phone1": public}
        await stack.channels.register(-100950001, USER, title="Public Gone",
                                      username="pubchan", invite_link=None)

        outcome = await stack.auto.leave_channel(-100950001)
        assert outcome["left"] == 0, outcome
        assert public.left == []
        assert await stack.channels.get(-100950001) is None


@pytest.mark.asyncio
class TestOrderErrorUx:
    async def test_a_broke_user_gets_the_topup_button(self):
        stack, _ = await channel_stack(2)
        await stack.users.grant_plan(USER, kinds=["views"], days=30, accounts=5)
        # A granted plan covers its own orders for free (round 6), so this walks
        # the payable configuration: per-order charging is still admin-selectable
        # and the insufficient-balance UX has to stay in place for it.
        await stack.settings.set_path("premium.plan_orders_included", False)
        event = user_event(data="u:buy_views:views_1k")
        await stack.router.handle_callback(event)
        link = user_event(text="https://t.me/c/950001/7")
        await stack.router.handle_message(link)

        assert "Not enough balance" in link.last_text, link.last_text
        assert "Add Money" in " | ".join(link.button_labels())
        assert any("top up" in str(a.get("text", "")).lower() for a in link.answers), link.answers
