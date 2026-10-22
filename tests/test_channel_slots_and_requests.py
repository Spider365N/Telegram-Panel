"""Round-7 acceptance tests: channel slots + locks, plan requests, formatting.

Covers the operator's rules verbatim:
* at most ``channels.free_limit`` channels run auto reactions for free,
* an added channel is **locked** — disable yes, remove no,
* more channels → tell the user to contact the admin and sell a channel plan,
* the admin can change the free-channel count from the admin panel,
* Add Money → Buy Premium with all plans + Request activation + Contact owner,
* polls are gone from the whole bot,
* panel text is professional (real anchors, no markdown links, one Auto
  Reactions button).
"""
from __future__ import annotations

import re

import pytest

from tests.fakes import FakeEvent, build_stack, session_stack

pytestmark = pytest.mark.asyncio


def ev(user_id: int, stack) -> FakeEvent:
    return FakeEvent(sender_id=int(user_id), chat_id=int(user_id), data="u:home")


def reqs(stack):
    from src.services.requests import get_plan_request_service

    return get_plan_request_service(users=stack.users)


# ---------------------------------------------------------------------------
# channel allowance
# ---------------------------------------------------------------------------
async def test_free_allowance_caps_auto_reactions_at_two_channels():
    stack = await build_stack()
    user_id = 7010
    await stack.users.get_or_create(user_id, username="chanuser", first_name="Chan")
    free = (await stack.settings.section("channels")).get("free_limit", 2)
    assert free == 2, "the operator asked for 2 free channels"

    allowed = []
    for index in range(4):
        channel_id = -10070000 - index
        await stack.channels.register(channel_id, owner_id=user_id, title=f"Chan {index}")
        verdict = await stack.channels.can_enable_auto(channel_id, user_id, stack.users)
        allowed.append(bool(verdict.get("allowed")))
        if verdict.get("allowed"):
            await stack.channels.set_auto_reactions(channel_id, True)

    assert allowed == [True, True, False, False], allowed
    slots = await stack.channels.channel_slots_of(user_id, stack.users)
    assert slots["total"] == 2 and slots["used"] == 2 and slots["free"] == 2

    # the third channel stays registered (so nothing is lost) but does not run
    third = await stack.channels.get(-10070002)
    assert third is not None
    assert not ((third.get("auto_reactions") or {}).get("enabled"))


async def test_channel_plan_raises_the_allowance_immediately():
    stack = await build_stack()
    user_id = 7011
    await stack.users.get_or_create(user_id, username="buyer", first_name="Buyer")
    for index in range(2):
        channel_id = -10071000 - index
        await stack.channels.register(channel_id, owner_id=user_id, title=f"Free {index}")
        assert (await stack.channels.can_enable_auto(channel_id, user_id,
                                                     stack.users))["allowed"]
        await stack.channels.set_auto_reactions(channel_id, True)

    # +3 channels plan (the "extra channels" segment)
    cfg = await stack.settings.section("channels")
    plan = next(p for p in cfg["plans"] if p["id"] == "ch_3")
    assert plan["slots"] == 3 and plan["duration_days"] == 30
    await stack.users.grant_plan(user_id, kinds=["channels"], days=plan["duration_days"],
                                 channels=plan["slots"], note="channel plan")

    slots = await stack.channels.channel_slots_of(user_id, stack.users)
    assert slots["total"] == 5, slots
    third = -10071002
    await stack.channels.register(third, owner_id=user_id, title="Extra")
    verdict = await stack.channels.can_enable_auto(third, user_id, stack.users)
    assert verdict["allowed"] is True
    await stack.channels.set_auto_reactions(third, True)
    assert await stack.channels.auto_channel_count(user_id) == 3


async def test_added_channels_are_locked_and_cannot_be_removed():
    stack = await build_stack()
    channel_id = -10072000
    doc = await stack.channels.register(channel_id, owner_id=7020, title="Locked")
    assert doc.get("locked") is True
    assert doc.get("added_by_user")

    assert await stack.channels.remove(channel_id) == 0, "a user may not delete a locked channel"
    assert await stack.channels.get(channel_id) is not None
    # the admin panel (force) can
    assert await stack.channels.remove(channel_id, force=True) == 1
    assert await stack.channels.get(channel_id) is None


async def test_admin_can_change_the_free_channel_count():
    stack = await build_stack()
    user_id = 7030
    await stack.users.get_or_create(user_id, username="cfguser", first_name="Cfg")
    await stack.settings.set_path("channels.free_limit", 4)

    limits = await stack.users.effective_limits(user_id)
    assert limits["channels"] == 4 and limits["free_channels"] == 4

    for index in range(4):
        channel_id = -10073000 - index
        await stack.channels.register(channel_id, owner_id=user_id)
        verdict = await stack.channels.can_enable_auto(channel_id, user_id, stack.users)
        assert verdict["allowed"] is True, f"channel {index} should fit inside the new limit"
        await stack.channels.set_auto_reactions(channel_id, True)
    overflow = -10073999
    await stack.channels.register(overflow, owner_id=user_id)
    verdict = await stack.channels.can_enable_auto(overflow, user_id, stack.users)
    assert verdict["allowed"] is False and verdict["slots"]["total"] == 4


async def test_auto_start_can_be_disabled_from_settings():
    stack = await build_stack()
    await stack.settings.set_path("channels.auto_enable", False)
    first = -10074000
    await stack.channels.register(first, owner_id=7099, title="NoAuto")
    outcome = await stack.auto.enable_free_plan(first)
    assert "on (free)" not in str(outcome.get("reactions"))
    doc = await stack.channels.get(first)
    assert doc is not None and not ((doc.get("auto_reactions") or {}).get("enabled"))
    # and switching it back on wires the next channel up again
    await stack.settings.set_path("channels.auto_enable", True)
    second = -10074001
    await stack.channels.register(second, owner_id=7099, title="AutoOn")
    outcome = await stack.auto.enable_free_plan(second)
    assert "on (free)" in str(outcome.get("reactions"))


async def test_channel_limit_notice_points_the_user_at_the_owner(mocker=None):
    stack = await build_stack()
    user_id = 7040
    await stack.users.get_or_create(user_id, username="limituser", first_name="Limit")
    for index in range(2):
        channel_id = -10075000 - index
        await stack.channels.register(channel_id, owner_id=user_id)
        await stack.channels.set_auto_reactions(channel_id, True)

    extra = -10075009
    await stack.channels.register(extra, owner_id=user_id, title="Too Many")
    verdict = await stack.channels.can_enable_auto(extra, user_id, stack.users)
    assert verdict["allowed"] is False
    assert verdict.get("reason")

    panel = stack.user_panel
    event = ev(user_id, stack)
    channel = await stack.channels.get(extra)
    await panel._show_channel_gate(event, channel, verdict)
    text = event.last_text.lower()
    assert "free channel limit reached" in text
    assert "channel plan" in text and "contact owner" in str(
        [b.text for row in (event.last_buttons or []) for b in row]).lower()


# ---------------------------------------------------------------------------
# plan requests (Buy Premium → Request activation → owner approves)
# ---------------------------------------------------------------------------
async def test_buy_premium_lists_every_plan_with_details_then_requests():
    stack = await build_stack()
    user_id = 7050
    await stack.users.get_or_create(user_id, username="requester", first_name="Req")
    panel = stack.user_panel
    event = ev(user_id, stack)

    await panel.show_premium(event)
    text = event.last_text
    assert "BUY PREMIUM" in text.upper()
    plans = await stack.payments.premium_plans()
    for plan in plans:
        assert str(plan.get("title")) in text, f"{plan.get('title')} missing from Buy Premium"
        assert str(plan.get("days")) in text
    labels = [b.text for row in event.last_buttons for b in row]
    assert any("Request activation" in label or "👑" in label for label in labels)

    plan = plans[0]
    await panel.show_premium_plan(event, [str(plan["id"])])
    assert "REQUEST ACTIVATION" in event.last_text.upper()

    await panel.request_premium_plan(event, [str(plan["id"])])
    pending = await reqs(stack).list(status="pending")
    assert len(pending) == 1
    doc = pending[0]
    assert int(doc["user_id"]) == user_id
    assert int(doc["days"]) == int(plan["days"] or 0)
    assert float(doc["price"]) == float(plan.get("price") or 0)
    assert "plan" in event.last_text.lower() or "request" in event.last_text.lower()


async def test_channel_plan_request_asks_for_a_count_then_notifies_the_owner():
    stack = await build_stack()
    user_id = 7051
    await stack.users.get_or_create(user_id, username="chanreq", first_name="ChanReq")
    panel = stack.user_panel
    event = ev(user_id, stack)

    await panel.show_channel_plans(event)
    assert "CHANNEL" in event.last_text.upper()
    labels = [b.text for row in event.last_buttons for b in row]
    assert any("channel" in label.lower() and "₹" in label for label in labels), labels
    assert any("custom" in label.lower() for label in labels), labels

    await panel.request_channel_plan(event, ["ch_3"])
    doc = (await reqs(stack).list(status="pending"))[0]
    assert doc["kind"] == "channels" and doc["channels"] == 3

    # custom count: the panel asks for a number, then files the request
    await panel.prompt_custom_channels(event)
    await panel._set_custom_channels(event, {}, "7")
    docs = await reqs(stack).list(status="pending")
    # 7 wanted − 2 free = 5 extra, exactly what the owner has to sell
    assert any(d["channels"] == 5 and d["plan_id"] == "custom" for d in docs), docs


async def test_owner_approval_grants_exactly_what_was_requested():
    stack = await build_stack()
    user_id = 7052
    await stack.users.get_or_create(user_id, username="appr", first_name="Appr")
    doc = await reqs(stack).request(user_id, kind="premium", plan_id="premium_30",
                                       title="Premium • 30 days", price=299, days=30,
                                       accounts=20, channels=5, note="paid ₹299 UPI")
    assert await reqs(stack).pending_count() == 1

    outcome = await reqs(stack).approve(doc["_id"], by=9001, notify=True)
    assert outcome is not None
    plan = await stack.users.plan_of(user_id)
    assert plan and plan.get("days") == 30
    limits = await stack.users.effective_limits(user_id)
    assert limits["channels"] >= 5 and limits["accounts"] >= 20
    assert await reqs(stack).pending_count() == 0
    stored = await reqs(stack).get(doc["_id"])
    assert stored["status"] == "approved" and int(stored["handled_by"]) == 9001


async def test_rejected_request_tells_the_user_and_stays_in_the_log():
    stack = await build_stack()
    user_id = 7053
    await stack.users.get_or_create(user_id, username="rej", first_name="Rej")
    doc = await reqs(stack).request(user_id, kind="channels", title="+10 channels",
                                       price=699, days=30, channels=10)
    await reqs(stack).reject(doc["_id"], by=9001, reason="payment not received")
    stored = await reqs(stack).get(doc["_id"])
    assert stored["status"] == "rejected"
    assert "payment not received" in str(stored["handler_note"])
    assert not await stack.users.is_premium(user_id)


async def test_admin_panel_shows_and_clears_the_request_queue():
    stack = await build_stack()
    admin = 9001
    await stack.users.get_or_create(admin, username="owner", first_name="Owner")
    user_id = 7054
    await stack.users.get_or_create(user_id, username="queue", first_name="Queue")
    doc = await reqs(stack).request(user_id, kind="premium", title="Premium • 90 days",
                                       price=799, days=90, accounts=40, channels=15)
    event = ev(admin, stack)
    panel = stack.admin_panel
    await panel.show_requests(event)
    assert "PLAN REQUESTS" in event.last_text.upper()
    assert doc["_id"] in event.last_text or str(user_id) in event.last_text
    labels = [b.text for row in event.last_buttons for b in row]
    assert any("Approve" in label for label in labels)

    await panel.approve_request(event, [doc["_id"]])
    assert await reqs(stack).pending_count() == 0
    assert await stack.users.is_premium(user_id)
    assert await stack.users.has_feature(user_id, "views")


async def test_admin_can_approve_with_a_custom_duration():
    stack = await build_stack()
    admin = 9001
    await stack.users.get_or_create(admin, username="owner", first_name="Owner")
    user_id = 7055
    await stack.users.get_or_create(user_id, username="custom", first_name="Custom")
    doc = await reqs(stack).request(user_id, kind="premium", title="Premium • 30 days",
                                       price=299, days=30)
    event = ev(admin, stack)
    await stack.admin_panel._set_request_days(event, {"request_id": doc["_id"]}, "45:30:6")
    plan = await stack.users.plan_of(user_id)
    assert plan["days"] == 45 and plan["accounts"] == 30 and plan["channels"] == 6


# ---------------------------------------------------------------------------
# polls removed / formatting / one Auto Reactions button
# ---------------------------------------------------------------------------
def test_polls_are_gone_from_the_whole_bot():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for needle in ("poll_plan", "create_poll", "send_poll", "show_polls", "\"poll\"", "'poll'",
                       "poll_option", "buy_poll"):
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle}")
    assert not offenders, f"poll leftovers: {offenders}"


async def test_auto_reactions_button_appears_once_in_the_user_panel():
    stack = await build_stack()
    event = ev(7060, stack)
    await stack.user_panel.show_home(event)
    labels = [b.text for row in event.last_buttons for b in row]
    hits = [label for label in labels if "auto reaction" in label.lower()]
    assert len(hits) == 1, labels
    assert "add money" not in " ".join(labels).lower()
    assert any("buy premium" in label.lower() for label in labels), labels


async def test_panel_text_uses_real_anchor_links_not_markdown():
    stack = await build_stack()
    await stack.settings.set_path("support.username", "kustsupport")
    event = ev(7061, stack)
    await stack.user_panel.show_contact_owner(event)
    text = event.last_text
    assert "[https://" not in text and "](http" not in text, text
    assert '<a href="https://t.me/kustsupport">' in text
    assert "t.me/kustsupport" in text


async def test_channel_screen_shows_slots_and_lock_state():
    stack = await build_stack()
    user_id = 7062
    await stack.users.get_or_create(user_id, username="screens", first_name="Screens")
    await stack.channels.register(-10076000, owner_id=user_id, title="Mine")
    await stack.channels.set_auto_reactions(-10076000, True)
    event = ev(user_id, stack)
    await stack.user_panel.show_my_channels(event)
    text = event.last_text
    assert "MY CHANNELS" in text.upper()
    assert re.search(r"Auto-reaction channels: <b>\d+/\d+</b>", text), text
    assert "🔒" in text
    labels = [b.text for row in event.last_buttons for b in row]
    assert any("channel slots" in label.lower() for label in labels), labels


async def test_plan_slots_flow_into_effective_limits_for_premium_plans():
    stack = await build_stack()
    user_id = 7063
    await stack.users.get_or_create(user_id, username="slots", first_name="Slots")
    base = (await stack.users.effective_limits(user_id))["channels"]
    assert base == 2

    # a full premium plan carries an own channel allowance
    cfg = await stack.settings.section("premium")
    plan = next(p for p in cfg["plans"] if p["id"] == "premium_90")
    await stack.users.grant_plan(user_id, kinds=["all"], days=plan["days"],
                                 channels=(plan.get("channels") or
                                           cfg.get("default_channels")))
    limits = await stack.users.effective_limits(user_id)
    assert limits["channels"] == 2 + int(plan.get("channels") or 0)

    # unlimited (god-mode style) has no ceiling at all
    await stack.users.grant_plan(user_id, kinds=["all"], unlimited=True)
    assert (await stack.users.effective_limits(user_id))["channels"] >= 10 ** 6


async def test_free_reaction_cap_and_timing_still_hold_with_channel_plans():
    """The standing rules: 8-10 random positive emoji, 1-3 s apart, no view bump."""
    from src.services.settings import DEFAULT_SETTINGS

    stack = await session_stack(accounts=10)
    auto = DEFAULT_SETTINGS["reactions"]["auto"]
    assert 8 <= int(auto["accounts_per_post"]) <= 10
    assert 1.0 <= float(auto["delay_min"]) and 1.0 <= float(auto["delay_max"]) <= 3.0
    assert auto["min_accounts"] >= 8
    user = 7064
    await stack.users.get_or_create(user, username="free", first_name="Free")
    assert not await stack.users.is_premium(user)
    # free tier = auto reactions keep working on a fresh channel …
    channel_id = -10077000
    await stack.channels.register(channel_id, owner_id=user, title="Free Tier")
    outcome = await stack.auto.enable_free_plan(channel_id)
    assert "on (free)" in str(outcome.get("reactions"))
    chan = await stack.channels.get(channel_id)
    assert (chan.get("auto_reactions") or {}).get("enabled") is True
    # … everything else stays premium-only
    assert not await stack.users.has_feature(user, "views")
    assert not await stack.users.has_feature(user, "comments")
