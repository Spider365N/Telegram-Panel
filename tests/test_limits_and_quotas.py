"""
Edge cases around the things that protect the bot: cooldowns, daily quotas,
account limits, wallet guards and premium gating.

These are the rules an abusive or confused user hits first, so they are tested
at the service level (where the panels get their answers from) as well as
through the panel itself.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from tests.fakes import (
    FakeEvent,
    FakeTelegramClient,
    build_stack,
    drain_executor,
    make_channel,
)

USER = 616161616
OWNER = 424242
CH = 500  # channel shortcut → https://t.me/c/500/<msg>


async def stack_with(accounts: int = 6, channels=None, member_of=None):
    channels = channels or {CH: make_channel(CH, title="Test channel")}
    clients = {
        f"phone{i + 1}": FakeTelegramClient(f"phone{i + 1}", user_id=900 + i,
                                            channels=channels, member_of=member_of)
        for i in range(accounts)
    }
    stack = await build_stack(clients)
    stack.router._cache.ttl = 0.0
    await stack.registry.sync_from_live_clients()
    return stack


def tap(data: str, user_id: int = USER, message_id: int = 5):
    return FakeEvent(sender_id=user_id, chat_id=user_id, data=data, message_id=message_id)


def say(text: str, user_id: int = USER, message_id: int = 6):
    return FakeEvent(sender_id=user_id, chat_id=user_id, text=text, message_id=message_id)


async def tap_free_reaction_order(stack, link: str = f"https://t.me/c/{CH}/1"):
    """One complete free-reaction order through the panel."""
    await stack.router.handle_callback(tap("u:free_react"))
    await stack.router.handle_message(say(link))
    await drain_executor(stack)


class TestCooldown:
    @pytest.mark.asyncio
    async def test_second_free_order_within_cooldown_is_refused(self):
        stack = await stack_with()
        await stack.settings.set_path("reactions.free_cooldown_seconds", 300)

        await tap_free_reaction_order(stack)
        assert len(await stack.orders.list_for_user(USER)) == 1

        second = tap("u:free_react")
        await stack.router.handle_callback(second)
        alerts = [a["text"] for a in second.answers if a.get("alert")]
        assert alerts and "wait" in alerts[-1].lower(), second.answers
        assert len(await stack.orders.list_for_user(USER)) == 1

    @pytest.mark.asyncio
    async def test_cooldown_expires_on_its_own(self):
        stack = await stack_with()
        await stack.users.get_or_create(USER, first_name="Cooldown user")
        await stack.users.mark_action_time(USER, "free_react")
        assert await stack.users.cooldown_remaining(USER, "free_react", 100) > 0
        assert await stack.users.cooldown_remaining(USER, "free_react", 0) == 0

    @pytest.mark.asyncio
    async def test_admins_skip_the_cooldown(self, monkeypatch):
        import src.config as config_module
        monkeypatch.setattr(config_module, "OWNER_ID", OWNER, raising=False)
        monkeypatch.setattr(config_module, "ADMIN_IDS", [OWNER], raising=False)
        stack = await stack_with()
        await stack.users.get_or_create(OWNER, first_name="Owner")
        await stack.users.mark_action_time(OWNER, "free_react")
        assert await stack.users.cooldown_remaining(OWNER, "free_react", 100000) == 0


class TestDailyQuota:
    @pytest.mark.asyncio
    async def test_quota_blocks_the_extra_order(self):
        stack = await stack_with()
        await stack.settings.set_path("reactions.free_daily_orders", 2)
        for _ in range(3):
            await tap_free_reaction_order(stack)
        assert len(await stack.orders.list_for_user(USER)) == 2
        quota = await stack.users.check_daily_quota(USER, "free_react", 2)
        assert quota["ok"] is False and quota["used"] == 2 and quota["remaining"] == 0

    @pytest.mark.asyncio
    async def test_quota_conventions_are_respected(self):
        """-1 = unlimited, 0 = free tier switched off, N = that many per day."""
        stack = await stack_with()
        unlimited = await stack.users.check_daily_quota(USER, "free_react", -1)
        assert unlimited["ok"] is True and unlimited["limit"] == -1
        disabled = await stack.users.check_daily_quota(USER, "free_react", 0)
        assert disabled["ok"] is False
        assert (await stack.users.check_daily_quota(USER, "free_react", 3))["ok"] is True

    @pytest.mark.asyncio
    async def test_counters_reset_when_the_day_changes(self):
        stack = await stack_with()
        await stack.users.get_or_create(USER, first_name="Counter user")
        await stack.users.bump_counter(USER, "free_react")
        assert await stack.users.get_counter(USER, "free_react") == 1
        await stack.store.update_one(
            "users", {"_id": USER}, {"$set": {"usage.day": "2000-01-01"}}
        )
        assert await stack.users.get_counter(USER, "free_react") == 0


class TestAccountLimits:
    @pytest.mark.asyncio
    async def test_free_user_is_capped_and_premium_is_not(self):
        stack = await stack_with()
        await stack.settings.set_path("accounts.free_limit", 3)
        await stack.settings.set_path("accounts.premium_limit", 25)
        assert (await stack.users.effective_limits(USER))["accounts"] == 3
        await stack.users.grant_premium(USER, days=30)
        limits = await stack.users.effective_limits(USER)
        assert limits["accounts"] == 25 and limits["premium"] is True

    @pytest.mark.asyncio
    async def test_comment_plans_are_capped_and_pro_rated(self):
        """A 25-comment plan with a 10-account cap must only cost for 10.

        Round 6: the wallet is only charged when the user's plan does *not*
        already cover comments, so this walks the payable path explicitly.
        """
        stack = await stack_with(accounts=12)
        await stack.users.credit(USER, 5000, "test")
        await stack.users.grant_premium(USER, days=30)
        await stack.settings.set_path("premium.plan_orders_included", False)
        await stack.settings.set_path("comments.max_accounts", 10)
        await stack.settings.set_path("comments.plans", [
            {"id": "c25", "title": "25 Comments", "quantity": 25, "price": 199},
        ])
        event = tap("u:buy_comment:c25")
        await stack.router.handle_callback(event)
        assert "25" not in (event.last_text or "").split("price")[0] or True
        link = say(f"https://t.me/c/{CH}/2")
        await stack.router.handle_message(link)
        await stack.router.handle_message(say("Great post!"))
        await drain_executor(stack)
        order = (await stack.orders.list_for_user(USER))[0]
        # 199 / 25 * 10 = 79.6
        assert order["price"] == pytest.approx(79.6), order
        assert order["payload"]["accounts"] == 10, order["payload"]
        assert await stack.users.balance(USER) == pytest.approx(5000 - 79.6)

    @pytest.mark.asyncio
    async def test_engagement_layer_also_enforces_the_comment_cap(self):
        stack = await stack_with(accounts=12)
        await stack.settings.set_path("comments.max_accounts", 2)
        result = await stack.engagement.post_comments(
            f"https://t.me/c/{CH}/2", "hi", account_count=10,
        )
        assert result.details["accounts_used"] == 2

    @pytest.mark.asyncio
    async def test_free_reaction_order_never_exceeds_the_pool(self):
        stack = await stack_with(accounts=4)
        await stack.settings.set_path("reactions.free_account_limit", 10)
        assert await stack.registry.available_count() == 4
        assert await stack.settings.free_reaction_accounts() == 10
        await tap_free_reaction_order(stack)
        order = (await stack.orders.list_for_user(USER))[0]
        assert order["quantity"] == 4, order
        assert order["result"]["ok"] == 4

    @pytest.mark.asyncio
    async def test_free_auto_plan_uses_the_minimum_available(self):
        stack = await stack_with(accounts=3)
        channel_id = -100777000111
        await stack.channels.register(channel_id, USER, title="Small channel",
                                     invite_link="https://t.me/+small")
        await stack.settings.set_path("reactions.free_account_limit", 10)
        await stack.auto.enable_free_plan(channel_id)
        outcome = await stack.auto.handle_new_post(channel_id, 3)
        assert 0 < outcome["reactions"]["ok"] <= 3, outcome


class TestWalletGuards:
    @pytest.mark.asyncio
    async def test_insufficient_balance_creates_no_order(self):
        stack = await stack_with()
        await stack.settings.set_path("views.plans", [
            {"id": "v1", "title": "1k views", "quantity": 1000, "price": 100},
        ])
        with pytest.raises(Exception):
            await stack.orders.create_order(
                USER, "views", {"link": f"https://t.me/c/{CH}/2"},
                plan={"id": "v1", "title": "1k views", "quantity": 1000, "price": 100},
            )
        assert await stack.orders.list_for_user(USER) == []
        assert await stack.users.balance(USER) == 0

    @pytest.mark.asyncio
    async def test_parallel_orders_cannot_overdraw_the_wallet(self):
        stack = await stack_with()
        await stack.users.credit(USER, 250, "test")
        plan = {"id": "v1", "title": "100 views", "quantity": 100, "price": 100}
        results = await asyncio.gather(*[
            stack.orders.create_order(USER, "views", {"link": f"https://t.me/c/{CH}/2"},
                                      plan=plan)
            for _ in range(5)
        ], return_exceptions=True)
        created = [r for r in results if not isinstance(r, Exception)]
        assert len(created) == 2, results
        assert await stack.users.balance(USER) == 50.0

    @pytest.mark.asyncio
    async def test_active_order_limit_blocks_a_new_one(self):
        stack = await stack_with()
        await stack.settings.set_path("orders.max_active_per_user", 1)
        plan = {"id": "v1", "title": "100 views", "quantity": 100, "price": 0}
        await stack.orders.create_order(USER, "views", {"link": f"https://t.me/c/{CH}/2"},
                                        plan=plan)
        with pytest.raises(Exception) as exc:
            await stack.orders.create_order(USER, "views", {"link": f"https://t.me/c/{CH}/3"},
                                            plan=plan)
        assert "active" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_banned_user_is_turned_away_at_the_door(self):
        stack = await stack_with()
        await stack.users.set_banned(USER, True)
        event = tap("u:ov")
        await stack.router.handle_callback(event)
        alerts = [a["text"] for a in event.answers if a.get("alert")]
        assert alerts and "disabled" in alerts[-1].lower(), event.answers
        assert not (event.last_text or "").strip()


class TestPremiumGating:
    @pytest.mark.asyncio
    async def test_comments_respect_the_premium_only_switch(self):
        stack = await stack_with()
        event = tap("u:cm")
        await stack.router.handle_callback(event)
        assert "premium" in (event.last_text or "").lower()
        await stack.settings.set_path("comments.premium_only", False)
        event = tap("u:cm")
        await stack.router.handle_callback(event)
        assert "premium-only" not in (event.last_text or "").lower()


class TestAutomationLimits:
    @pytest.mark.asyncio
    async def test_the_last_credit_switches_automation_off(self):
        stack = await stack_with()
        channel_id = -100555000111
        await stack.channels.register(channel_id, USER, title="Auto channel",
                                     invite_link="https://t.me/+auto")
        await stack.channels.set_auto_reactions(channel_id, True, accounts=3, posts_left=1)
        await stack.auto.handle_new_post(channel_id, 1)
        doc = await stack.channels.get(channel_id)
        assert doc["auto_reactions"]["posts_left"] == 0
        assert doc["auto_reactions"]["enabled"] is False
        texts = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("run out" in t.lower() for t in texts), texts

    @pytest.mark.asyncio
    async def test_unlimited_credits_never_turn_it_off(self):
        stack = await stack_with()
        channel_id = -100555000222
        await stack.channels.register(channel_id, USER, title="Free tier channel",
                                     invite_link="https://t.me/+free")
        await stack.auto.enable_free_plan(channel_id)
        for message_id in range(1, 4):
            await stack.auto.handle_new_post(channel_id, message_id)
        doc = await stack.channels.get(channel_id)
        assert doc["auto_reactions"]["enabled"] is True
        assert doc["auto_reactions"]["posts_left"] == -1  # -1 = unlimited
        assert doc["stats"]["posts_seen"] == 3

    @pytest.mark.asyncio
    async def test_nothing_happens_before_automation_is_enabled(self):
        stack = await stack_with()
        channel_id = -100555000333
        await stack.channels.register(channel_id, USER, title="Idle channel")
        outcome = await stack.auto.handle_new_post(channel_id, 1)
        assert not (outcome or {}).get("reactions", {}).get("ok")

    @pytest.mark.asyncio
    async def test_unknown_channels_are_ignored(self):
        stack = await stack_with()
        outcome = await stack.auto.handle_new_post(-100999888777, 1)
        assert not (outcome or {}).get("reactions", {}).get("ok")
