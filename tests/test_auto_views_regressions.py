"""Regressions for view delivery and multi-user channel attribution."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from telethon.tl.functions.messages import GetMessagesViewsRequest

from src.db.store import COLL_CHANNELS
from src.services.engagement import OperationResult
from tests.fakes import FakeTelegramClient, build_stack, make_channel

OWNER = 810000101
ADDED_BY = 810000102
UNLINKED_USER = 810000103
CHANNEL_ID = 810000201
INTERNAL_CHANNEL_ID = int(f"-100{CHANNEL_ID}")
PUBLIC_USERNAME = "autoviews_regression_channel"


@pytest.mark.asyncio
async def test_existing_channel_keeps_adder_and_uses_that_users_views_plan():
    """A different creator and bot-adder must both be considered for entitlement."""
    tg_channel = make_channel(CHANNEL_ID, username=PUBLIC_USERNAME, title="Regression")
    account = FakeTelegramClient(
        "views-account", user_id=810000301, channels={CHANNEL_ID: tg_channel},
        member_of=set(), known_entities=set(),
    )
    stack = await build_stack({"views-account": account})
    await stack.registry.sync_from_live_clients()

    # Simulate an older record with a known creator but no recorded bot-adder.
    await stack.channels.register(
        INTERNAL_CHANNEL_ID, OWNER, title="Regression", username=PUBLIC_USERNAME,
        owner_source="web admin",
    )
    # The bot-add event must record the adder without stealing the creator.
    await stack.channels.register(
        INTERNAL_CHANNEL_ID, ADDED_BY, title="Regression", username=PUBLIC_USERNAME,
        added_by=ADDED_BY, owner_source="added_by",
    )
    await stack.users.grant_plan(ADDED_BY, kinds=["views"], days=30, notify=False)

    channel = await stack.channels.get(INTERNAL_CHANNEL_ID)
    assert channel["owner_id"] == OWNER
    assert channel["added_by"] == ADDED_BY
    assert channel["auto_views"]["enabled"] is True

    outcome = await stack.auto.handle_new_post(INTERNAL_CHANNEL_ID, 31)

    assert outcome["views"]["ok"] == 1, outcome
    assert any(isinstance(req, GetMessagesViewsRequest) for req in account.requests)
    # Public channels must resolve via username even when the account has no
    # dialog membership or cached numeric access hash.
    assert any(type(req).__name__ == "ResolveUsernameRequest" for req in account.requests)


@pytest.mark.asyncio
async def test_linked_adder_can_enable_views_even_when_owner_is_unknown():
    stack = await build_stack({})
    await stack.users.grant_plan(ADDED_BY, kinds=["views"], days=30, notify=False)
    await stack.channels.register(
        INTERNAL_CHANNEL_ID, None, title="Unowned regression", username=PUBLIC_USERNAME,
        added_by=ADDED_BY, owner_source="added_by",
    )

    outcome = await stack.auto.enable_free_plan(INTERNAL_CHANNEL_ID)

    channel = await stack.channels.get(INTERNAL_CHANNEL_ID)
    assert outcome["views"] == "on (premium)"
    assert channel.get("owner_id") is None
    assert channel["auto_views"]["enabled"] is True


@pytest.mark.asyncio
async def test_public_auto_views_and_results_are_logged(caplog):
    tg_channel = make_channel(CHANNEL_ID, username=PUBLIC_USERNAME, title="Regression")
    account = FakeTelegramClient(
        "views-account", user_id=810000302, channels={CHANNEL_ID: tg_channel},
        member_of=set(), known_entities=set(),
    )
    stack = await build_stack({"views-account": account})
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(
        INTERNAL_CHANNEL_ID, OWNER, title="Regression", username=PUBLIC_USERNAME,
    )
    await stack.users.grant_plan(OWNER, kinds=["views"], days=30, notify=False)
    await stack.channels.set_auto_views(INTERNAL_CHANNEL_ID, True, views_per_post=1)

    with caplog.at_level("INFO"):
        outcome = await stack.auto.handle_new_post(INTERNAL_CHANNEL_ID, 32)

    assert outcome["views"]["ok"] == 1, outcome
    assert "Auto view requests on" in caplog.text
    assert "1 accepted" in caplog.text


@pytest.mark.asyncio
async def test_private_auto_views_refreshes_a_stale_invite_once(caplog):
    tg_channel = make_channel(CHANNEL_ID, title="Private regression")
    account = FakeTelegramClient(
        "views-account", user_id=810000304, channels={CHANNEL_ID: tg_channel},
        member_of=set(), known_entities=set(), stale_hashes={"old-invite"},
    )
    stack = await build_stack({"views-account": account})
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(
        INTERNAL_CHANNEL_ID, OWNER, title="Private regression",
        invite_link="https://t.me/+old-invite",
    )
    await stack.users.grant_plan(OWNER, kinds=["views"], days=30, notify=False)
    await stack.channels.set_auto_views(INTERNAL_CHANNEL_ID, True, views_per_post=1)

    with caplog.at_level("INFO"):
        outcome = await stack.auto.handle_new_post(INTERNAL_CHANNEL_ID, 35)

    assert outcome["views"]["ok"] == 1, outcome
    assert "Retrying auto views" in caplog.text
    assert any(isinstance(req, GetMessagesViewsRequest) for req in account.requests)
    assert account.member_of == {CHANNEL_ID}


@pytest.mark.asyncio
async def test_failed_views_keep_post_credit_and_do_not_log_account_names(caplog):
    tg_channel = make_channel(CHANNEL_ID, username=PUBLIC_USERNAME, title="Regression")
    account = FakeTelegramClient(
        "private-session-label", user_id=810000303, channels={CHANNEL_ID: tg_channel},
        member_of=set(), known_entities=set(),
    )
    stack = await build_stack({"private-session-label": account})
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(
        INTERNAL_CHANNEL_ID, OWNER, title="Regression", username=PUBLIC_USERNAME,
    )
    await stack.users.grant_plan(OWNER, kinds=["views"], days=30, notify=False)
    await stack.channels.set_auto_views(
        INTERNAL_CHANNEL_ID, True, views_per_post=1, posts_left=1,
    )
    stack.engagement.auto_views = AsyncMock(return_value=OperationResult(
        ok=0, failed=1,
        errors=["private-session-label: TimeoutError: private transport details"],
    ))

    with caplog.at_level("INFO"):
        outcome = await stack.auto.handle_new_post(INTERNAL_CHANNEL_ID, 33)

    channel = await stack.channels.get(INTERNAL_CHANNEL_ID)
    assert outcome["views"]["ok"] == 0
    assert outcome["views"]["posts_left"] == 1
    assert channel["auto_views"]["enabled"] is True
    assert channel["auto_views"]["posts_left"] == 1
    assert "Auto view requests on" in caplog.text
    assert "private-session-label" not in caplog.text
    assert "private transport details" not in caplog.text


@pytest.mark.asyncio
async def test_plan_granted_to_unlinked_user_does_not_enable_another_users_channel():
    stack = await build_stack({})
    await stack.channels.register(INTERNAL_CHANNEL_ID, OWNER, title="Other user's channel")

    await stack.users.grant_plan(UNLINKED_USER, kinds=["views"], days=30)

    channel = await stack.channels.get(INTERNAL_CHANNEL_ID)
    assert channel["owner_id"] == OWNER
    assert channel["auto_views"]["enabled"] is False
    notice = " ".join(m["text"] for m in stack.bot.tbot.sent_messages)
    assert "No channel is linked" in notice
    assert "Admin Dashboard" in notice


@pytest.mark.asyncio
async def test_duplicate_channel_post_claim_is_atomic(monkeypatch):
    stack = await build_stack({})
    await stack.channels.register(INTERNAL_CHANNEL_ID, OWNER, title="Regression")

    # Force both handlers to observe the same old marker before either writes;
    # this reproduces the Mongo round-trip interleaving that a memory store
    # normally executes too quickly to expose.
    barrier = asyncio.Barrier(2)
    original_find_one = stack.store.find_one
    waiting = 0

    async def delayed_read(collection, query):
        nonlocal waiting
        doc = await original_find_one(collection, query)
        if (collection == COLL_CHANNELS and query == {"_id": INTERNAL_CHANNEL_ID}
                and waiting < 2):
            waiting += 1
            await barrier.wait()
        return doc

    monkeypatch.setattr(stack.store, "find_one", delayed_read)
    results = await asyncio.gather(*[
        stack.channels.mark_post_seen(INTERNAL_CHANNEL_ID, 34) for _ in range(2)
    ])
    assert sum(results) == 1


@pytest.mark.asyncio
async def test_concurrent_sponsor_updates_do_not_overwrite_each_other():
    stack = await build_stack({})
    await stack.channels.register(INTERNAL_CHANNEL_ID, OWNER, title="Regression")

    barrier = asyncio.Barrier(2)
    original_find_one = stack.store.find_one
    waiting = 0

    async def delayed_read(collection, query):
        nonlocal waiting
        doc = await original_find_one(collection, query)
        if (collection == COLL_CHANNELS and query == {"_id": INTERNAL_CHANNEL_ID}
                and waiting < 2):
            waiting += 1
            await barrier.wait()
        return doc

    stack.store.find_one = delayed_read
    await asyncio.gather(
        stack.channels.add_sponsor(INTERNAL_CHANNEL_ID, ADDED_BY),
        stack.channels.add_sponsor(INTERNAL_CHANNEL_ID, UNLINKED_USER),
    )
    channel = await stack.channels.get(INTERNAL_CHANNEL_ID)
    assert {ADDED_BY, UNLINKED_USER}.issubset(set(channel["sponsors"]))
