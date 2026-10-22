"""
The free *order* path, end to end.

Auto reactions are free the moment the bot joins a channel — but a user may
also tap ``❤️ Order Reactions`` ▸ ``🎁 Free`` and hand the bot one post link.
That path has to work for a private channel the bot has only just been added
to (no invite link stored yet), it has to cost nothing, and if the stored
invite link has died the order must self-heal instead of failing with

    Could not resolve chat -100… for this account (private channel …)

Both of those were real production complaints, so they are pinned down here.
"""
from __future__ import annotations

import pytest

from tests.fakes import FakeEvent, FakeTelegramClient, build_stack, drain_executor, make_channel

OWNER = 123456789
USER = 4242
CHANNEL_ID = 920001
INTERNAL = int(f"-100{CHANNEL_ID}")
POST_LINK = f"https://t.me/c/{CHANNEL_ID}/12"


def user_event(data=None, text="", sender_id=USER, message_id=5):
    return FakeEvent(sender_id=sender_id, chat_id=sender_id, data=data, text=text,
                     message_id=message_id)


async def order_stack(accounts: int = 4, *, invite_link=None, stale_hashes=None,
                      member_of=None):
    """A pool that is NOT inside the private channel, plus the registered channel."""
    channel = make_channel(CHANNEL_ID)          # no username → private
    clients = {
        f"phone{i+1}": FakeTelegramClient(
            f"phone{i+1}", user_id=7000 + i, channels={CHANNEL_ID: channel},
            member_of=set() if member_of is None else member_of,
            stale_hashes=stale_hashes,
        )
        for i in range(accounts)
    }
    stack = await build_stack(clients)
    # several taps may arrive on the same panel within the same second
    stack.router._cache.ttl = 0.0
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(INTERNAL, USER, title="Customer Channel",
                                  invite_link=invite_link)
    return stack


async def place_free_order(stack, link: str = POST_LINK):
    """Tap ❤️ Order Reactions ▸ 🎁 Free and paste a post link, like a user does."""
    open_screen = user_event(data="u:or")
    await stack.router.handle_callback(open_screen)
    assert "Order Reactions" in open_screen.last_text

    tap = user_event(data="u:free_react")
    await stack.router.handle_callback(tap)
    assert "Free reactions" in tap.last_text, tap.last_text

    link_event = user_event(text=link)
    handled = await stack.router.handle_message(link_event)
    assert handled is True
    await drain_executor(stack)
    return link_event


@pytest.mark.asyncio
class TestFreeOrderIsFree:
    async def test_free_order_costs_nothing_and_really_reacts(self):
        stack = await order_stack(4)
        await stack.users.get_or_create(USER)          # brand-new user, empty wallet

        link_event = await place_free_order(stack)

        orders = await stack.orders.list_for_user(USER)
        assert len(orders) == 1, orders
        order = orders[0]
        assert order["kind"] == "reaction"
        assert float(order["price"]) == 0.0, "free reactions must never cost money"
        assert order["status"] in ("completed", "partial"), order
        assert await stack.users.balance(USER) == 0, "the wallet was charged"
        assert "Reaction order placed" in link_event.last_text

        reacted = [
            name for name, client in stack.bot.active_clients.items()
            for request in client.requests
            if type(request).__name__ == "SendReactionRequest"
        ]
        assert reacted, "no reaction request reached Telegram"
        # …and they had to get inside first: joining is what the free order does
        joined = [n for n, c in stack.bot.active_clients.items() if c.joined]
        assert joined, "accounts never joined the private channel"

    async def test_the_same_user_cannot_react_for_free_all_day(self):
        stack = await order_stack(2)
        await stack.settings.set_path("reactions.free_daily_orders", 1)

        await place_free_order(stack)
        assert len(await stack.orders.list_for_user(USER)) == 1

        again = user_event(data="u:free_react")
        await stack.router.handle_callback(again)
        assert any(a.get("alert") for a in again.answers), again.answers
        assert "limit" in " ".join(
            str(a.get("text", "")) for a in again.answers).lower()
        assert len(await stack.orders.list_for_user(USER)) == 1, "a second free order slipped through"

    async def test_paid_reaction_plan_still_needs_money(self):
        """The free path must not leak a paid plan to a broke user."""
        stack = await order_stack(2)
        plans = (await stack.settings.section("reactions")).get("plans") or []
        assert plans, "no paid reaction plans configured"
        plan_id = plans[0]["id"]

        tap = user_event(data=f"u:buy_react:{plan_id}")
        await stack.router.handle_callback(tap)
        if "post link" in tap.last_text.lower():        # the panel asked for a link
            await stack.router.handle_message(user_event(text=POST_LINK))
        assert await stack.orders.list_for_user(USER) == [], "an unpaid premium order was created"


@pytest.mark.asyncio
class TestPrivateChannelOrderSelfHeals:
    async def test_order_on_a_channel_without_a_stored_invite_still_joins(self):
        """The bot was just added: no invite link has ever been exported."""
        stack = await order_stack(3, invite_link=None)
        assert not (await stack.channels.get(INTERNAL)).get("invite_link")

        await place_free_order(stack)

        order = (await stack.orders.list_for_user(USER))[0]
        assert order["status"] == "completed", order
        assert any("fake920001" in link for link in stack.bot.exported_invites), \
            "the bot was never asked for an invite link"
        doc = await stack.channels.get(INTERNAL)
        assert doc["invite_link"].endswith("fake920001"), "the fresh link was not stored"
        joined = [n for n, c in stack.bot.active_clients.items() if c.joined]
        assert joined, "nobody joined, so no reaction could have been applied"

    async def test_a_dead_invite_link_is_replaced_and_the_batch_retried(self):
        """The stored link was revoked: refresh it and try again, same order."""
        stack = await order_stack(3, invite_link="https://t.me/+deadHash",
                                  stale_hashes={"deadHash"})

        await place_free_order(stack)

        order = (await stack.orders.list_for_user(USER))[0]
        assert order["status"] == "completed", (order.get("error"), order)
        assert int(order.get("result", {}).get("ok") or 0) > 0
        doc = await stack.channels.get(INTERNAL)
        assert doc["invite_link"].endswith("fake920001"), "the dead link was not replaced"
        assert any(client.joined for client in stack.bot.active_clients.values())

    async def test_a_dead_link_does_not_reach_the_user_as_a_failure(self):
        stack = await order_stack(2, invite_link="https://t.me/+deadHash",
                                  stale_hashes={"deadHash"})
        await place_free_order(stack)
        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert not any("Order failed" in text for text in messages), messages


@pytest.mark.asyncio
class TestAutoPathRetriesImmediately:
    async def test_first_post_succeeds_even_with_a_dead_link(self):
        stack = await order_stack(6, invite_link="https://t.me/+deadHash",
                                  stale_hashes={"deadHash"})
        await stack.auto.enable_free_plan(INTERNAL)

        outcome = await stack.auto.handle_new_post(INTERNAL, 3)

        assert outcome["reactions"]["ok"] > 0, outcome
        assert outcome["reactions"]["posts_left"] == -1      # free tier untouched
        doc = await stack.channels.get(INTERNAL)
        assert doc["invite_link"].endswith("fake920001")

    async def test_the_retry_does_not_burn_two_credits(self):
        stack = await order_stack(4, invite_link="https://t.me/+deadHash",
                                  stale_hashes={"deadHash"})
        await stack.channels.set_auto_reactions(INTERNAL, True, accounts=2, posts_left=2)

        await stack.auto.handle_new_post(INTERNAL, 1)

        doc = await stack.channels.get(INTERNAL)
        assert doc["auto_reactions"]["posts_left"] == 1, "the retry spent a second credit"


@pytest.mark.asyncio
class TestOwnerIsToldTheTruth:
    async def test_a_genuinely_broken_channel_explains_itself(self):
        """Nothing the code can fix → the owner gets the real reason, not "0 ok"."""
        stack = await order_stack(2, invite_link="https://t.me/+deadHash",
                                  stale_hashes={"deadHash"})
        # even a fresh export comes back dead (e.g. the link was revoked again)
        async def dead_export(channel_id):
            return "https://t.me/+deadHash"

        stack.bot._export_invite = dead_export
        await stack.auto.enable_free_plan(INTERNAL)
        await stack.auto.handle_new_post(INTERNAL, 5)

        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("could not be applied" in text.lower() for text in messages), messages
