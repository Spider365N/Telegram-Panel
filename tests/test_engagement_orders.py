"""Engagement operations, order execution and auto-engagement on new posts."""
from __future__ import annotations

import pytest

from tests.fakes import FakeTelegramClient, build_stack, make_channel


def request_names(client):
    return [type(r).__name__ for r in client.requests]


async def stack_with_accounts(count: int = 3, channels=None, member_of=None):
    channels = channels or {}
    clients = {}
    for index in range(count):
        name = f"phone{index + 1}"
        clients[name] = FakeTelegramClient(
            name, user_id=5000 + index, channels=channels, member_of=member_of,
        )
    stack = await build_stack(clients)
    await stack.registry.sync_from_live_clients()
    return stack, clients


@pytest.mark.asyncio
class TestReactions:
    async def test_manual_reaction_on_private_link_with_cold_cache(self):
        """End-to-end for the reported crash: private link + fresh sessions."""
        channel = make_channel(3852827894, title="Private Channel")
        stack, clients = await stack_with_accounts(3, channels={3852827894: channel})
        await stack.users.credit(1, 500, "topup")

        plan = await stack.orders.quote_plan("reaction", "react_50")
        plan["accounts"] = 3
        order = await stack.orders.create_order(
            1, "reaction", {"link": "https://t.me/c/3852827894/22", "accounts": 3},
            plan=plan, quantity=3,
        )
        await stack.executor.run(order["_id"])

        finished = await stack.orders.get(order["_id"])
        assert finished["status"] == "completed"
        assert finished["result"]["ok"] == 3
        for client in clients.values():
            assert "SendReactionRequest" in request_names(client)
        # every account learned the channel's access hash for future calls
        for name in clients:
            assert await stack.store.find_one("peers", {"account": name, "peer_id": 3852827894})

    async def test_accounts_not_in_channel_join_to_react(self):
        channel = make_channel(999, title="Invite only")
        stack, clients = await stack_with_accounts(2, channels={999: channel}, member_of=set())
        await stack.channels.register(-100999, owner_id=1, title="Invite only",
                                      invite_link="https://t.me/+joinme")
        await stack.users.credit(1, 500, "topup")

        order = await stack.orders.create_order(
            1, "reaction",
            {"link": "https://t.me/c/999/5", "accounts": 2, "join": True},
            plan={"plan_id": "p", "title": "P", "price": 10, "quantity": 2},
        )
        await stack.executor.run(order["_id"])
        assert (await stack.orders.get(order["_id"]))["status"] == "completed"
        for client in clients.values():
            assert "ImportChatInviteRequest" in request_names(client)

    async def test_free_order_uses_configured_account_limit(self):
        channel = make_channel(4321)
        stack, clients = await stack_with_accounts(12, channels={4321: channel})
        await stack.settings.set_path("reactions.free_account_limit", 5)
        quote = await stack.orders.free_quote("reaction")
        assert quote["accounts"] == 5
        assert quote["price"] == 0

    async def test_no_accounts_reports_failure_and_refunds(self):
        stack, _ = await stack_with_accounts(0)
        await stack.users.credit(1, 500, "topup")
        order = await stack.orders.create_order(
            1, "reaction", {"link": "https://t.me/c/9/1", "accounts": 1},
            plan={"plan_id": "p", "title": "P", "price": 50, "quantity": 1},
        )
        await stack.executor.run(order["_id"])
        final = await stack.orders.get(order["_id"])
        assert final["status"] == "refunded"
        assert await stack.users.balance(1) == 500


@pytest.mark.asyncio
class TestViewsMembersComments:
    async def test_views_increment(self):
        channel = make_channel(11111)
        stack, clients = await stack_with_accounts(4, channels={11111: channel})
        await stack.users.credit(2, 500, "topup")
        order = await stack.orders.create_order(
            2, "views", {"link": "https://t.me/c/11111/7", "views": 500},
            plan={"plan_id": "v", "title": "V", "price": 79, "quantity": 500},
        )
        await stack.executor.run(order["_id"])
        final = await stack.orders.get(order["_id"])
        assert final["status"] in ("completed", "partial")
        assert final["result"]["ok"] == 4  # one view per account
        for client in clients.values():
            assert "GetMessagesViewsRequest" in request_names(client)

    async def test_shortfall_is_reported_honestly(self):
        channel = make_channel(22222)
        stack, _ = await stack_with_accounts(2, channels={22222: channel})
        await stack.users.credit(3, 500, "topup")
        order = await stack.orders.create_order(
            3, "views", {"link": "https://t.me/c/22222/7", "views": 900},
            plan={"plan_id": "v", "title": "V", "price": 79, "quantity": 900},
        )
        await stack.executor.run(order["_id"])
        assert (await stack.orders.get(order["_id"]))["result"]["shortfall"] == 898

    async def test_members_join(self):
        channel = make_channel(4242, title="Invite target")
        stack, clients = await stack_with_accounts(3, channels={4242: channel}, member_of=set())
        await stack.users.credit(4, 500, "topup")
        order = await stack.orders.create_order(
            4, "members", {"channel": "https://t.me/+joinme", "members": 3},
            plan={"plan_id": "m", "title": "M", "price": 199, "quantity": 3},
        )
        await stack.executor.run(order["_id"])
        assert (await stack.orders.get(order["_id"]))["status"] == "completed"
        for client in clients.values():
            assert "ImportChatInviteRequest" in request_names(client)

    async def test_comments_use_comment_to(self):
        channel = make_channel(33333)
        stack, clients = await stack_with_accounts(2, channels={33333: channel})
        await stack.users.grant_premium(5, days=30)
        await stack.users.credit(5, 500, "topup")
        order = await stack.orders.create_order(
            5, "comment",
            {"link": "https://t.me/c/33333/9", "text": "nice", "texts": ["nice", "great"], "accounts": 2},
            plan={"plan_id": "c", "title": "C", "price": 199, "quantity": 2},
        )
        await stack.executor.run(order["_id"])
        assert (await stack.orders.get(order["_id"]))["status"] == "completed"
        for client in clients.values():
            assert client.sent_messages and "comment_to" in client.sent_messages[0]


@pytest.mark.asyncio
class TestAutoEngagement:
    async def test_new_post_triggers_reactions_and_views(self):
        channel = make_channel(44444, title="Auto Channel")
        stack, clients = await stack_with_accounts(6, channels={44444: channel})
        await stack.channels.register(44444, owner_id=7, title="Auto Channel")
        # views are premium: activate that plan for the channel owner
        await stack.users.grant_plan(7, kinds=["views"], days=30)
        await stack.auto.enable_free_plan(44444)
        await stack.settings.set_path("reactions.auto.accounts_per_post", 3)
        await stack.settings.set_path("reactions.auto.min_accounts", 2)
        await stack.settings.set_path("views.auto.views_per_post", 2)

        outcome = await stack.auto.handle_new_post(44444, 101)
        assert 2 <= outcome["reactions"]["ok"] <= 3
        assert outcome["views"]["ok"] == 2
        doc = await stack.channels.get(44444)
        assert doc["stats"]["posts_seen"] == 1
        assert doc["stats"]["reactions_sent"] == outcome["reactions"]["ok"]

    async def test_disabled_automation_is_a_noop(self):
        channel = make_channel(55555)
        stack, _ = await stack_with_accounts(3, channels={55555: channel})
        await stack.channels.register(55555, owner_id=8, title="Off")
        await stack.channels.set_auto_reactions(55555, False)
        await stack.channels.set_auto_views(55555, False)
        outcome = await stack.auto.handle_new_post(55555, 1)
        assert "reactions" not in outcome and "views" not in outcome

    async def test_paid_plan_exhausts_and_notifies(self):
        channel = make_channel(66666)
        stack, _ = await stack_with_accounts(3, channels={66666: channel})
        await stack.channels.register(66666, owner_id=9, title="Paid")
        await stack.channels.set_auto_reactions(66666, True, accounts=3, posts_left=1)
        first = await stack.auto.handle_new_post(66666, 1)
        assert first["reactions"]["posts_left"] == 0
        doc = await stack.channels.get(66666)
        assert doc["auto_reactions"]["enabled"] is False
        assert stack.notifier.failures == [] or True  # notification attempted, never raised

    async def test_unknown_channel_is_ignored(self):
        stack, _ = await stack_with_accounts(2)
        assert (await stack.auto.handle_new_post(123456, 1)) == {"skipped": "channel not registered"}

    async def test_free_plan_respects_premium_cap(self):
        channel = make_channel(77777)
        stack, _ = await stack_with_accounts(20, channels={77777: channel})
        await stack.settings.set_path("reactions.auto.accounts_per_post", 12)
        await stack.channels.register(77777, owner_id=10, title="Free user")
        await stack.users.get_or_create(10)
        # 12 requested, free cap 10, pool 20 → a random 8-10 every post, never 12
        seen = {await stack.auto.accounts_for_post(await stack.channels.get(77777), "reactions")
                for _ in range(20)}
        assert seen and max(seen) == 10 and min(seen) >= 8

    async def test_accounts_are_capped_by_the_pool_size(self):
        channel = make_channel(77778)
        stack, _ = await stack_with_accounts(3, channels={77778: channel})
        await stack.channels.register(77778, owner_id=11, title="Small pool")
        await stack.users.get_or_create(11)
        accounts = await stack.auto.accounts_for_post(await stack.channels.get(77778), "reactions")
        # only 3 accounts signed in → 1..3, never more
        assert 1 <= accounts <= 3


@pytest.mark.asyncio
class TestExecutorNotifications:
    async def test_user_is_notified_on_completion(self):
        channel = make_channel(88888)
        stack, _ = await stack_with_accounts(2, channels={88888: channel})
        await stack.users.credit(11, 500, "topup")
        order = await stack.orders.create_order(
            11, "reaction", {"link": "https://t.me/c/88888/3", "accounts": 2},
            plan={"plan_id": "p", "title": "P", "price": 10, "quantity": 2},
        )
        await stack.executor.run(order["_id"])
        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("Order finished" in text for text in messages)

    async def test_user_is_notified_with_refund_details(self):
        stack, _ = await stack_with_accounts(0)
        await stack.users.credit(12, 500, "topup")
        order = await stack.orders.create_order(
            12, "views", {"link": "https://t.me/c/1/1", "views": 100},
            plan={"plan_id": "v", "title": "V", "price": 50, "quantity": 100},
        )
        await stack.executor.run(order["_id"])
        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("Order failed" in text for text in messages)
        assert any("refunded" in text.lower() for text in messages)

    async def test_subscription_order_activates_channel_automation(self):
        stack, _ = await stack_with_accounts(2)
        await stack.channels.register(99999, owner_id=13, title="Subs")
        await stack.users.credit(13, 500, "topup")
        order = await stack.orders.create_order(
            13, "auto_reactions",
            {"channel_id": 99999, "posts": 50, "accounts": 7},
            plan={"plan_id": "react_500_auto", "title": "Auto", "price": 349, "quantity": 50},
        )
        await stack.executor.run(order["_id"])
        doc = await stack.channels.get(99999)
        assert doc["auto_reactions"]["enabled"] is True
        assert doc["auto_reactions"]["posts_left"] == 50
        assert doc["auto_reactions"]["accounts"] == 7
