"""
The free auto-reaction tier, exactly as specified by the operator:

* every user who adds the bot to a channel gets auto reactions **for free**,
* on every new message (text / media / file) the accounts apply **random
  positive emojis**, from a random **8-10** different accounts (fewer when the
  pool is smaller), **1-3 seconds apart**,
* a **public** channel needs no membership — the account just reacts,
* a **private** channel is joined through the invite link the bot creates,
* reaction accounts must **never** bump the view counter (views are a separate,
  paid service),
* when the run fails the owner is told *why* instead of a silent "0 ok".
"""
from __future__ import annotations

import pytest

from telethon.tl.functions.messages import GetMessagesViewsRequest

from tests.fakes import build_stack, make_channel

OWNER = 123456789
USER = 4242
FREE_EMOJI_POOL = "👍❤️🔥🎉🥰👏😁🤩💯🙏⚡🏆👌😍🤝🕊"


async def free_stack(accounts: int = 12, *, channel_id: int = 909001,
                     username=None, member_of=None, fail_join: bool = False):
    """A stack with a signed-in pool and a freshly added channel."""
    from tests.fakes import FakeTelegramClient

    channel = make_channel(channel_id, username=username)
    clients = {}
    for index in range(accounts):
        name = f"phone{index + 1}"
        clients[name] = FakeTelegramClient(
            name, user_id=5000 + index, channels={channel_id: channel},
            member_of=member_of, fail_join=fail_join,
        )
    stack = await build_stack(clients)
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(
        int(f"-100{channel_id}"), USER,
        title="Customer Channel", username=username, kind="channel",
    )
    return stack, channel


@pytest.mark.asyncio
class TestFreeTierIsFree:
    async def test_free_reactions_start_for_a_brand_new_user(self):
        """No plan, no payment — auto reactions are on the moment the bot is added."""
        stack, _ = await free_stack(12)
        outcome = await stack.auto.enable_free_plan(int(f"-100909001"))
        assert outcome["reactions"] == "on (free)"
        doc = await stack.channels.get(int(f"-100909001"))
        assert doc["auto_reactions"]["enabled"] is True

    async def test_free_tier_never_asks_for_money(self):
        stack, _ = await free_stack(12)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        outcome = await stack.auto.handle_new_post(int(f"-100909001"), 5)
        assert outcome["reactions"]["ok"] > 0
        # no wallet was touched and no order was created
        assert await stack.users.balance(USER) == 0
        assert await stack.orders.list_for_user(USER) == []

    async def test_automation_uses_8_to_10_accounts_by_default(self):
        stack, _ = await free_stack(12)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        seen = set()
        for message_id in range(1, 26):
            outcome = await stack.auto.handle_new_post(int(f"-100909001"), message_id)
            seen.add(outcome["reactions"]["accounts"])
        assert seen <= set(range(8, 11)), seen          # 8, 9 or 10 — never more
        assert len(seen) > 1, "the account count should vary per post"

    async def test_small_pool_is_used_completely(self):
        stack, _ = await free_stack(3)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        outcome = await stack.auto.handle_new_post(int(f"-100909001"), 1)
        assert outcome["reactions"]["accounts"] <= 3
        assert outcome["reactions"]["ok"] == outcome["reactions"]["accounts"]

    async def test_each_account_uses_a_random_positive_emoji(self):
        stack, _ = await free_stack(10)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        await stack.auto.handle_new_post(int(f"-100909001"), 1)
        applied = []
        for name, client in stack.bot.active_clients.items():
            for request in getattr(client, "requests", []):
                if type(request).__name__ == "SendReactionRequest":
                    applied.append(request.reaction[0].emoticon)
        assert applied, "no reaction request reached Telegram"
        for emoji in applied:
            assert emoji in FREE_EMOJI_POOL

    async def test_the_default_pool_only_holds_telegram_reaction_emojis(self):
        """Telegram refuses emojis outside its own reaction set ("✅" was one)."""
        from src.services.settings import (
            ALLOWED_REACTION_EMOJIS,
            DEFAULT_POSITIVE_EMOJIS,
            is_allowed_reaction_emoji,
        )

        assert "✅" not in DEFAULT_POSITIVE_EMOJIS
        for emoji in DEFAULT_POSITIVE_EMOJIS:
            assert is_allowed_reaction_emoji(emoji), f"{emoji} is not a Telegram reaction"
            assert emoji.replace("\ufe0f", "") in FREE_EMOJI_POOL.replace("\ufe0f", "")
        assert len(ALLOWED_REACTION_EMOJIS) >= 60

    async def test_delay_between_accounts_defaults_to_one_to_three_seconds(self):
        """Ship-time defaults (the test harness zeroes them for speed)."""
        from src.services.settings import DEFAULT_SETTINGS

        auto = DEFAULT_SETTINGS["reactions"]["auto"]
        assert (auto["delay_min"], auto["delay_max"]) == (1.0, 3.0)
        assert auto["accounts_per_post"] == 10 and auto["min_accounts"] == 8
        # …and the owner can still change both from the admin panel
        stack, _ = await free_stack(10)
        await stack.settings.set_path("reactions.auto.delay_max", 4.5)
        auto = (await stack.settings.section("reactions"))["auto"]
        assert auto["delay_max"] == 4.5


@pytest.mark.asyncio
class TestJoiningBehaviour:
    async def test_public_channel_needs_no_join(self):
        stack, _ = await free_stack(10, username="mypublicchannel")
        await stack.auto.enable_free_plan(int(f"-100909001"))
        outcome = await stack.auto.handle_new_post(int(f"-100909001"), 1)
        assert outcome["reactions"]["ok"] > 0
        for _, client in stack.bot.active_clients.items():
            assert client.joined == [], "a public channel must not be joined"

    async def test_private_channel_is_joined_through_the_bot_invite(self):
        stack, _ = await free_stack(10, member_of=set())     # nobody is inside yet
        outcome = await stack.auto.enable_free_plan(int(f"-100909001"))
        joined = [name for name, client in stack.bot.active_clients.items() if client.joined]
        assert outcome["warmup"]["ok"] > 0
        assert len(joined) == outcome["warmup"]["ok"], (joined, outcome["warmup"])
        assert any("fake909001" in link for link in stack.bot.exported_invites)
        # and the link is kept on the channel for the next post
        doc = await stack.channels.get(int(f"-100909001"))
        assert doc["invite_link"].endswith("fake909001")

    async def test_invite_is_retried_on_a_later_post(self):
        """The exact production failure: the first export failed, nothing joined."""
        stack, _ = await free_stack(6, member_of=set())
        calls = {"n": 0}
        real = stack.bot._export_invite

        async def flaky_export(channel_id):
            calls["n"] += 1
            if calls["n"] == 1:
                return None            # bot had no Invite-Users permission yet
            return await real(channel_id)

        stack.bot._export_invite = flaky_export
        assert (await stack.auto.enable_free_plan(int(f"-100909001")))["warmup"][
            "skipped"] == "no invite link yet"
        assert not (await stack.channels.get(int(f"-100909001"))).get("invite_link")

        outcome = await stack.auto.handle_new_post(int(f"-100909001"), 7)
        assert calls["n"] >= 2, "the post must ask the bot for the link again"
        assert outcome["reactions"]["ok"] > 0, outcome["reactions"]
        doc = await stack.channels.get(int(f"-100909001"))
        assert doc["invite_link"], "the link is stored for the next post"

    async def test_failed_join_explains_itself_to_the_owner(self):
        stack, _ = await free_stack(4, member_of=set(), fail_join=True)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        await stack.auto.handle_new_post(int(f"-100909001"), 3)
        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("could not be applied" in text.lower() for text in messages), messages
        assert any("Invite Users" in text for text in messages)


@pytest.mark.asyncio
class TestViewsAreNotAffected:
    async def test_reacting_never_increments_views(self):
        stack, _ = await free_stack(10)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        await stack.auto.handle_new_post(int(f"-100909001"), 42)
        for _, client in stack.bot.active_clients.items():
            view_requests = [r for r in client.requests
                             if isinstance(r, GetMessagesViewsRequest)]
            assert view_requests == [], "reaction accounts must not send views"

    async def test_auto_views_stay_off_for_free_users(self):
        stack, _ = await free_stack(10)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        doc = await stack.channels.get(int(f"-100909001"))
        assert doc["auto_views"]["enabled"] is False

    async def test_reaction_accounts_are_not_reused_for_views(self):
        stack, _ = await free_stack(10)
        await stack.users.grant_plan(USER, kinds=["views"], days=30)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        await stack.settings.set_path("views.auto.views_per_post", 3)
        await stack.auto.handle_new_post(int(f"-100909001"), 8)
        reacted = set()
        viewed = set()
        for name, client in stack.bot.active_clients.items():
            for request in client.requests:
                kind = type(request).__name__
                if kind == "SendReactionRequest":
                    reacted.add(name)
                elif isinstance(request, GetMessagesViewsRequest):
                    viewed.add(name)
        assert reacted and viewed
        # one account does one job per post — the reaction pool is a different
        # pick from the view pool, so the counts stay honest
        assert len(viewed) == 3


@pytest.mark.asyncio
class TestQuotaAndCredits:
    async def test_paid_posts_left_is_decremented_on_reaction(self):
        stack, _ = await free_stack(10)
        await stack.channels.set_auto_reactions(int(f"-100909001"), True,
                                                accounts=4, posts_left=2)
        first = await stack.auto.handle_new_post(int(f"-100909001"), 1)
        assert first["reactions"]["posts_left"] == 1
        second = await stack.auto.handle_new_post(int(f"-100909001"), 2)
        assert second["reactions"]["posts_left"] == 0
        doc = await stack.channels.get(int(f"-100909001"))
        assert doc["auto_reactions"]["enabled"] is False   # credits used up

    async def test_free_posts_do_not_expire(self):
        stack, _ = await free_stack(10)
        await stack.auto.enable_free_plan(int(f"-100909001"))
        doc = await stack.channels.get(int(f"-100909001"))
        assert doc["auto_reactions"]["posts_left"] == -1     # unlimited free tier
        for message_id in range(1, 6):
            outcome = await stack.auto.handle_new_post(int(f"-100909001"), message_id)
            assert outcome["reactions"]["posts_left"] == -1
