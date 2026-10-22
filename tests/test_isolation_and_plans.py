"""
Multi-user isolation + the premium plan system.

Two promises the operator asked for, verified here:

**Isolation** — a user only ever sees and manages the channels *they* added the
bot to. Nobody else's channel shows up in their panel, and a hand-crafted
callback cannot touch it. The owner keeps super powers; admins additionally see
channels whose adder Telegram did not report.

**Premium** — only auto reactions are free. Views, comments, members and extra channels
are activated by the owner/admin, per plan type, with a number of days and an
account cap, plus the special no-limits grant.
"""
from __future__ import annotations

import time

import pytest

from tests.fakes import FakeEvent, build_stack, make_channel

OWNER = 123456789
ALICE = 4242
BOB = 4243


async def iso_stack(accounts: int = 4):
    """Two channels owned by two different users."""
    stack = await build_stack({})
    await stack.channels.register(-100700001, ALICE, title="Alice Channel")
    await stack.channels.register(-100700002, BOB, title="Bob Channel")
    await stack.channels.register(-100700003, None, title="Orphan Channel")
    return stack


def cb(user: int, data: str) -> FakeEvent:
    return FakeEvent(sender_id=user, chat_id=user, data=data)


@pytest.mark.asyncio
class TestChannelIsolation:
    async def test_a_user_only_sees_their_own_channels(self):
        stack = await iso_stack()
        alice = await stack.user_panel.channels_of(ALICE)
        bob = await stack.user_panel.channels_of(BOB)
        assert [c["_id"] for c in alice] == [-100700001]
        assert [c["_id"] for c in bob] == [-100700002]

    async def test_the_panel_never_mentions_another_users_channel(self):
        stack = await iso_stack()
        event = cb(ALICE, "u:ch")
        await stack.router.handle_callback(event)
        assert "Alice Channel" in event.last_text
        assert "Bob Channel" not in event.last_text
        assert "Orphan Channel" not in event.last_text

    async def test_another_users_channel_cannot_be_opened(self):
        stack = await iso_stack()
        event = cb(ALICE, "u:ar_ch:-100700002")
        await stack.router.handle_callback(event)
        alerts = [a for a in event.answers if a["alert"]]
        assert alerts and "not yours" in alerts[-1]["text"].lower()

    async def test_another_users_channel_cannot_be_toggled(self):
        stack = await iso_stack()
        before = await stack.channels.get(-100700002)
        await stack.router.handle_callback(cb(ALICE, "u:ar_toggle:-100700002"))
        after = await stack.channels.get(-100700002)
        assert before["auto_reactions"] == after["auto_reactions"]

    async def test_another_users_channel_settings_cannot_be_changed(self):
        stack = await iso_stack()
        await stack.router.handle_callback(cb(ALICE, "u:ar_accounts:-100700002"))
        await stack.router.handle_message(
            FakeEvent(sender_id=ALICE, chat_id=ALICE, text="9"))
        doc = await stack.channels.get(-100700002)
        assert doc["auto_reactions"]["accounts"] in (0, None)

    async def test_another_users_auto_plan_cannot_be_bought(self):
        stack = await iso_stack()
        await stack.users.credit(ALICE, 1000, "topup")
        await stack.router.handle_callback(cb(ALICE, "u:ar_plan_go:-100700002:react_50"))
        doc = await stack.channels.get(-100700002)
        assert int((doc.get("auto_reactions") or {}).get("posts_left") or 0) in (0, -1)

    async def test_a_normal_user_cannot_claim_someone_elses_channel(self):
        stack = await iso_stack()
        event = cb(BOB, "u:ch_claim:-100700001")
        await stack.router.handle_callback(event)
        doc = await stack.channels.get(-100700001)
        assert doc["owner_id"] == ALICE
        assert event.answers and "another user" in event.answers[-1]["text"].lower()

    async def test_the_owner_sees_everything(self):
        stack = await iso_stack()
        owned = await stack.user_panel.channels_of(OWNER)
        assert {-100700001, -100700002, -100700003} <= {c["_id"] for c in owned}

    async def test_an_admin_sees_own_and_unowned_but_not_other_users(self):
        stack = await iso_stack()
        await stack.channels.register(-100700004, OWNER, title="Admin Own")
        seen = {c["_id"] for c in await stack.user_panel.channels_of(OWNER)}
        # the owner sees everything anyway — check a *second* admin instead
        await stack.users.set_role(5555, "admin")
        seen = {c["_id"] for c in await stack.user_panel.channels_of(5555)}
        assert seen == {-100700003}, seen      # orphan only, never Alice/Bob's

    async def test_owner_can_manage_any_channel(self):
        stack = await iso_stack()
        event = cb(OWNER, "u:ar_ch:-100700002")
        await stack.router.handle_callback(event)
        assert "Bob Channel" in event.last_text


@pytest.mark.asyncio
class TestPremiumGates:
    async def test_only_reactions_are_free(self):
        stack = await build_stack({})
        await stack.users.get_or_create(ALICE)
        limits = await stack.users.effective_limits(ALICE)
        assert limits["premium"] is False
        assert limits["free_reaction_accounts"] == 10
        assert limits["reaction_accounts"] == 10          # free tier is generous
        assert limits["comment_accounts"] == 0
        assert limits["auto_view_accounts"] == 0
        assert limits["features"]["views"] is False

    async def test_each_gate_renders_the_request_screen(self):
        stack = await build_stack({})
        for data, title in (("u:ov", "Order Views"), ("u:av", "Auto Views"),
                            ("u:mb", "Members")):
            event = cb(ALICE, data)
            await stack.router.handle_callback(event)
            assert "premium service" in event.last_text.lower(), data
            assert title.split()[0] in event.last_text, data
            assert "Request activation" in " | ".join(event.button_labels())

    async def test_plan_unlocks_only_the_services_it_covers(self):
        stack = await build_stack({})
        await stack.users.grant_plan(ALICE, kinds=["views"], days=30)
        assert await stack.users.has_feature(ALICE, "views") is True
        assert await stack.users.has_feature(ALICE, "comments") is False
        assert await stack.users.has_feature(ALICE, "channels") is False
        event = cb(ALICE, "u:av")
        await stack.router.handle_callback(event)
        assert "premium service" not in event.last_text.lower()

    async def test_all_kinds_plan_unlocks_everything(self):
        stack = await build_stack({})
        await stack.users.grant_plan(ALICE, kinds="all", days=7)
        for feature in ("reactions", "views", "comments", "members", "channels"):
            assert await stack.users.has_feature(ALICE, feature) is True

    async def test_unlimited_plan_has_no_limits(self):
        stack = await build_stack({})
        await stack.users.grant_plan(ALICE, unlimited=True)
        plan = await stack.users.plan_of(ALICE)
        limits = await stack.users.effective_limits(ALICE)
        assert plan["unlimited"] is True and plan.get("expires_at") is None
        assert limits["accounts"] >= 10 ** 6
        assert limits["unlimited"] is True

    async def test_plan_days_and_account_cap_are_honoured(self):
        stack = await build_stack({})
        before = time.time()
        plan = await stack.users.grant_plan(ALICE, kinds=["comments"], days=15, accounts=42)
        assert plan["accounts"] == 42
        assert 15 * 86400 - 60 <= plan["expires_at"] - before <= 15 * 86400 + 60

    async def test_expired_plan_falls_back_to_free(self):
        stack = await build_stack({})
        await stack.users.get_or_create(ALICE)
        await stack.store.update_one("users", {"_id": ALICE}, {"$set": {
            "plan": {"type": "premium", "kinds": ["views"], "expires_at": time.time() - 10}
        }})
        assert await stack.users.has_feature(ALICE, "views") is False
        limits = await stack.users.effective_limits(ALICE)
        assert limits["premium"] is False
        assert limits["reaction_accounts"] == 10       # free tier still works

    async def test_owner_and_admins_are_always_unlimited(self):
        stack = await build_stack({})
        assert await stack.users.has_feature(OWNER, "comments") is True
        limits = await stack.users.effective_limits(OWNER)
        assert limits["unlimited"] is True


@pytest.mark.asyncio
class TestOwnerPlanCommands:
    async def _bot(self, stack):
        from src.telbot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.tbot = stack.bot.tbot
        bot.services_ready = True
        from src.runtime import set_bot
        set_bot(bot)          # _notify_plan uses the notifier, which uses get_bot()
        return bot

    async def test_grant_a_single_service(self):
        stack = await build_stack({})
        bot = await self._bot(stack)
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, text="/grant 4242 views 30")
        await bot._plan_command(event, stack.router, "grant")
        assert await stack.users.has_feature(ALICE, "views") is True
        assert await stack.users.has_feature(ALICE, "comments") is False

    async def test_grant_everything_with_a_cap(self):
        stack = await build_stack({})
        bot = await self._bot(stack)
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, text="/grant 4242 all 90 25")
        await bot._plan_command(event, stack.router, "grant")
        limits = await stack.users.effective_limits(ALICE)
        assert limits["accounts"] == 25
        assert limits["features"]["channels"] is True

    async def test_unlimited_command(self):
        stack = await build_stack({})
        bot = await self._bot(stack)
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, text="/unlimited 4242")
        await bot._plan_command(event, stack.router, "unlimited")
        assert (await stack.users.plan_of(ALICE))["unlimited"] is True

    async def test_revoke_command(self):
        stack = await build_stack({})
        bot = await self._bot(stack)
        await stack.users.grant_plan(ALICE, kinds="all", days=30)
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, text="/revoke 4242")
        await bot._plan_command(event, stack.router, "revoke")
        assert (await stack.users.plan_of(ALICE))["type"] == "free"

    async def test_a_normal_user_cannot_use_them(self):
        stack = await build_stack({})
        bot = await self._bot(stack)
        event = FakeEvent(sender_id=ALICE, chat_id=ALICE, text="/unlimited 4242")
        await bot._plan_command(event, stack.router, "unlimited")
        assert (await stack.users.plan_of(ALICE)).get("unlimited") is not True
        assert "owner" in event.last_text.lower()

    async def test_the_user_is_notified_about_the_new_plan(self):
        stack = await build_stack({})
        bot = await self._bot(stack)
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, text="/grant 4242 views 30")
        await bot._plan_command(event, stack.router, "grant")
        texts = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("Plan activated" in t for t in texts), texts

    async def test_usage_message_is_shown_for_bad_input(self):
        stack = await build_stack({})
        bot = await self._bot(stack)
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, text="/grant")
        await bot._plan_command(event, stack.router, "grant")
        assert "/grant" in event.last_text
