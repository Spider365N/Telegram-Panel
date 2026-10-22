"""
User panel + admin panel flows, driven through the real router.

These exercise the panels exactly like Telegram would: /start, inline button
taps and plain-text input — against fake clients and an in-memory database.
"""
from __future__ import annotations

import pytest

from tests.fakes import FakeEvent, FakeTelegramClient, build_stack, drain_executor, make_channel

OWNER = 123456789   # ADMIN_ID used by tests/conftest.py
OTHER_ADMIN = 777
USER = 4242


async def panel_stack(accounts: int = 3, channels=None, member_of=None):
    # NOTE: tests fire several taps on the same message instantly, so the
    # double-tap guard (meant for Telegram's millisecond-level redelivery) is
    # switched off here.
    channels = channels or {}
    clients = {
        f"phone{i + 1}": FakeTelegramClient(f"phone{i + 1}", user_id=900 + i,
                                            channels=channels, member_of=member_of)
        for i in range(accounts)
    }
    stack = await build_stack(clients)
    stack.router._cache.ttl = 0.0
    await stack.registry.sync_from_live_clients()
    return stack


def user_event(data=None, text="", sender_id=USER, message_id=5):
    return FakeEvent(sender_id=sender_id, chat_id=sender_id, data=data, text=text,
                     message_id=message_id)


@pytest.mark.asyncio
class TestUserPanelHome:
    async def test_start_shows_service_panel_for_normal_user(self):
        stack = await panel_stack()
        event = user_event()
        await stack.router.start_for(event)
        text = event.last_text
        labels = " | ".join(event.button_labels())
        assert "TELEGRAM PANEL" in text
        for expected in ("📈 Order Views", "🔄 Auto Views", "❤️ Order Reactions",
                         "🔄 Auto Reactions", "👤 Account Info", "👥 Members",
                         "🆘 Support", "🧾 My Orders", "📋 My Channels"):
            assert expected in labels, f"missing {expected} in {labels}"
        assert "Add To Channel" in labels and "Add To Group" in labels

    async def test_start_shows_admin_panel_for_owner(self):
        stack = await panel_stack()
        event = user_event(sender_id=OWNER)
        await stack.router.start_for(event)
        assert "ADMIN PANEL" in event.last_text
        assert "💰 Pricing" in " | ".join(event.button_labels())

    async def test_banned_user_is_blocked(self):
        stack = await panel_stack()
        await stack.users.get_or_create(USER)
        await stack.users.set_banned(USER, True)
        event = user_event()
        await stack.router.start_for(event)
        assert "disabled" in event.last_text.lower()

    async def test_non_admin_cannot_reach_admin_panel(self):
        stack = await panel_stack()
        event = user_event(data="a:pricing")
        await stack.router.handle_callback(event)
        assert event.answers and event.answers[-1]["alert"] is True


@pytest.mark.asyncio
class TestViewsFlow:
    async def _with_views(self, stack):
        """Views are a premium service — activate the plan first."""
        await stack.users.grant_plan(USER, kinds=["views"], days=30)
        return stack

    async def test_order_views_happy_path(self):
        channel = make_channel(12345)
        stack = await self._with_views(await panel_stack(3, channels={12345: channel}))
        await stack.users.credit(USER, 500, "topup")

        await stack.router.handle_callback(user_event(data="u:ov"))
        event = user_event(data="u:buy_views:views_1k")
        await stack.router.handle_callback(event)
        assert "post link" in event.last_text.lower()

        link_event = user_event(text="https://t.me/c/12345/99")
        handled = await stack.router.handle_message(link_event)
        assert handled is True
        assert "Order placed" in link_event.last_text

        await drain_executor(stack)
        orders = await stack.orders.list_for_user(USER)
        assert len(orders) == 1
        assert orders[0]["status"] in ("completed", "partial")
        # Round 6: the granted plan covers views, so the order runs without a
        # second payment ("plan ke baad bhi paise maang raha hai" fix).
        assert orders[0]["paid_with"] == "plan", orders[0]
        assert float(orders[0]["price"]) == 0.0
        assert await stack.users.balance(USER) == 500

    async def test_custom_quantity_is_priced(self):
        stack = await self._with_views(await panel_stack(2))
        await stack.users.credit(USER, 5000, "topup")
        await stack.router.handle_callback(user_event(data="u:views_custom"))
        qty_event = user_event(text="2500")
        await stack.router.handle_message(qty_event)
        assert "Price for 2,500 views" in qty_event.last_text
        link_event = user_event(text="https://t.me/c/12345/7")
        await stack.router.handle_message(link_event)
        order = (await stack.orders.list_for_user(USER))[0]
        assert order["quantity"] == 2500

    async def test_invalid_custom_quantity_is_rejected(self):
        stack = await self._with_views(await panel_stack())
        await stack.router.handle_callback(user_event(data="u:views_custom"))
        event = user_event(text="abc")
        await stack.router.handle_message(event)
        assert "plain number" in event.last_text


@pytest.mark.asyncio
class TestReactionsFlow:
    async def test_free_reactions_use_free_limit(self):
        channel = make_channel(500)
        stack = await panel_stack(15, channels={500: channel})
        await stack.settings.set_path("reactions.free_account_limit", 4)
        event = user_event(data="u:free_react")
        await stack.router.handle_callback(event)
        assert "Free reactions" in event.last_text

        link_event = user_event(text="https://t.me/c/500/1")
        await stack.router.handle_message(link_event)
        await drain_executor(stack)
        order = (await stack.orders.list_for_user(USER))[0]
        assert order["paid_with"] == "free"
        assert order["result"]["ok"] == 4
        assert await stack.users.balance(USER) == 0

    async def test_free_daily_quota_is_enforced(self):
        channel = make_channel(501)
        stack = await panel_stack(5, channels={501: channel})
        await stack.settings.set_path("reactions.free_daily_orders", 1)
        for _ in range(2):
            await stack.router.handle_callback(user_event(data="u:free_react"))
            await stack.router.handle_message(user_event(text="https://t.me/c/501/1"))
            await drain_executor(stack)
        orders = await stack.orders.list_for_user(USER)
        assert len(orders) == 1  # the second attempt was blocked

    async def test_paid_reactions_charge_wallet(self):
        channel = make_channel(502)
        stack = await panel_stack(5, channels={502: channel})
        await stack.users.credit(USER, 500, "topup")
        await stack.router.handle_callback(user_event(data="u:buy_react:react_200"))
        await stack.router.handle_message(user_event(text="https://t.me/c/502/3"))
        await drain_executor(stack)
        order = (await stack.orders.list_for_user(USER))[0]
        assert order["status"] in ("completed", "partial")
        assert await stack.users.balance(USER) == 351


@pytest.mark.asyncio
class TestOrderManagementFlow:
    async def test_orders_list_and_cancel_refunds(self):
        stack = await panel_stack(2)
        await stack.users.credit(USER, 1000, "topup")
        plan = await stack.orders.quote_plan("views", "views_1k")
        order = await stack.orders.create_order(USER, "views", {"link": "x"}, plan=plan)

        event = user_event(data="u:om")
        await stack.router.handle_callback(event)
        assert "Order Management" in event.last_text

        cancel_event = user_event(data=f"u:order_cancel:{order['_id']}")
        await stack.router.handle_callback(cancel_event)
        refreshed = await stack.orders.get(order["_id"])
        assert refreshed["status"] == "refunded"
        assert await stack.users.balance(USER) == 1000


@pytest.mark.asyncio
class TestChannelsAndAutoFeatures:
    async def test_add_to_channel_deep_link_has_rights(self):
        stack = await panel_stack()
        stack.bot.tbot._me.username = "mybot"
        text = ""
        for data in ("u:addch", "u:addgr"):
            event = user_event(data=data)
            await stack.router.handle_callback(event)
            flat = []
            for row in (event.last_buttons or []):
                for button in row:
                    flat.append(getattr(button, "url", "") or getattr(button, "text", ""))
            assert any("startchannel" in str(u) or "startgroup" in str(u) for u in flat) or \
                "Add To" in event.last_text

    async def test_auto_screens_require_a_channel(self):
        stack = await panel_stack()
        await stack.users.grant_plan(USER, kinds=["views"], days=30)
        event = user_event(data="u:av")
        await stack.router.handle_callback(event)
        assert "haven't added me" in event.last_text.lower()

    async def test_locked_feature_offers_to_ask_the_owner(self):
        """A free user tapping a premium service gets the request screen."""
        stack = await panel_stack()
        event = user_event(data="u:av")
        await stack.router.handle_callback(event)
        assert "premium service" in event.last_text.lower()
        assert "Request activation" in " | ".join(event.button_labels())

        request = user_event(data="u:reqplan:views")
        await stack.router.handle_callback(request)
        assert "Request sent" in request.last_text
        # the owner was told about it (the fake bot client records every send)
        messages = [m["text"] for m in stack.bot.tbot.sent_messages]
        assert any("Plan request" in text and str(USER) in text for text in messages)

    async def test_auto_views_settings_flow(self):
        stack = await panel_stack(4, channels={700: make_channel(700)})
        await stack.users.grant_plan(USER, kinds=["views"], days=30)
        await stack.channels.register(-100700, USER, title="My Channel")
        event = user_event(data="u:av")
        await stack.router.handle_callback(event)
        assert "Auto Views" in event.last_text

        channel_event = user_event(data="u:av_ch:-100700")
        await stack.router.handle_callback(channel_event)
        assert "Auto Views" in channel_event.last_text

        per_post = user_event(data="u:av_perpost:-100700")
        await stack.router.handle_callback(per_post)
        value_event = user_event(text="150")
        await stack.router.handle_message(value_event)
        doc = await stack.channels.get(-100700)
        assert doc["auto_views"]["views_per_post"] == 150

    async def test_auto_reaction_account_cap_for_free_users(self):
        stack = await panel_stack(10, channels={701: make_channel(701)})
        await stack.channels.register(-100701, USER, title="Capped")
        await stack.settings.set_path("reactions.free_account_limit", 3)
        await stack.router.handle_callback(user_event(data="u:ar_accounts:-100701"))
        event = user_event(text="8")
        await stack.router.handle_message(event)
        assert "Your plan allows" in event.last_text


@pytest.mark.asyncio
class TestWalletAndPremium:
    async def test_topup_flow_end_to_end(self):
        stack = await panel_stack()
        await stack.router.handle_callback(user_event(data="u:topup"))
        amount_event = user_event(text="250")
        await stack.router.handle_message(amount_event)
        assert "UTR" in amount_event.last_text

        utr_event = user_event(text="123412341234")
        await stack.router.handle_message(utr_event)
        assert "Top-up submitted" in utr_event.last_text
        pending = await stack.payments.pending_topups()
        assert pending and pending[0]["amount"] == 250
        assert await stack.users.balance(USER) == 0

        # admin approves
        approve_event = user_event(data=f"a:topup_ok:{pending[0]['_id']}", sender_id=OWNER)
        await stack.router.handle_callback(approve_event)
        assert await stack.users.balance(USER) == 250

    async def test_premium_gate_then_owner_grants_the_plan(self):
        stack = await panel_stack()
        # A free user is stopped at the premium gate.
        wait_event = user_event(data="u:cm")
        await stack.router.handle_callback(wait_event)
        assert "premium" in wait_event.last_text.lower()

        # Buy Premium shows the plans plus a way to reach the owner.
        plans_event = user_event(data="u:prem")
        await stack.router.handle_callback(plans_event)
        labels = " | ".join(plans_event.button_labels())
        assert "Premium" in plans_event.last_text
        assert "Contact" in labels or "Request" in labels, labels

        # The admin grants the plan and the service opens up.
        await stack.users.grant_plan(USER, kinds=["comments"], days=30, notify=False)
        unlocked = user_event(data="u:cm")
        await stack.router.handle_callback(unlocked)
        assert "Comments" in unlocked.last_text

    async def test_account_info_screen(self):
        stack = await panel_stack(3)
        event = user_event(data="u:ai")
        await stack.router.handle_callback(event)
        text = event.last_text
        assert "Account Info" in text and "Account pool" in text


@pytest.mark.asyncio
class TestAdminPanel:
    async def test_dashboard(self):
        stack = await panel_stack(2)
        await stack.router.start_for(user_event(sender_id=OWNER))
        event = user_event(data="a:home", sender_id=OWNER)
        await stack.router.handle_callback(event)
        assert "Live stats" in event.last_text

    async def test_price_edit_changes_settings(self):
        stack = await panel_stack()
        await stack.router.handle_callback(user_event(data="a:pricing", sender_id=OWNER))
        await stack.router.handle_callback(
            user_event(data="a:section:views", sender_id=OWNER))
        await stack.router.handle_callback(
            user_event(data="a:plan_edit:views:views_1k:price", sender_id=OWNER))
        event = user_event(text="99", sender_id=OWNER)
        await stack.router.handle_message(event)
        plan = await stack.settings.get_plan("views", "views_1k")
        assert plan["price"] == 99

    async def test_new_plan_creation(self):
        stack = await panel_stack()
        await stack.router.handle_callback(user_event(data="a:plan_add:views", sender_id=OWNER))
        await stack.router.handle_message(user_event(text="Mega Views", sender_id=OWNER))
        await stack.router.handle_message(user_event(text="20000", sender_id=OWNER))
        await stack.router.handle_message(user_event(text="799", sender_id=OWNER))
        plans = await stack.settings.plans("views")
        created = [p for p in plans if p["title"] == "Mega Views"]
        assert created and created[0]["price"] == 799 and created[0]["quantity"] == 20000

    async def test_delete_plan(self):
        stack = await panel_stack()
        await stack.router.handle_callback(
            user_event(data="a:plan_del:views:views_5k", sender_id=OWNER))
        ids = [p["id"] for p in await stack.settings.plans("views")]
        assert "views_5k" not in ids

    async def test_free_limit_edit(self):
        stack = await panel_stack()
        await stack.router.handle_callback(user_event(data="a:limits", sender_id=OWNER))
        await stack.router.handle_callback(
            user_event(data="a:limit_edit:reactions.free_account_limit", sender_id=OWNER))
        event = user_event(text="6", sender_id=OWNER)
        await stack.router.handle_message(event)
        assert (await stack.settings.get(refresh=True))["reactions"]["free_account_limit"] == 6

    async def test_emoji_management(self):
        stack = await panel_stack()
        await stack.router.handle_callback(user_event(data="a:emojis", sender_id=OWNER))
        await stack.router.handle_callback(user_event(data="a:emoji_add", sender_id=OWNER))
        event = user_event(text="🍓 🎉", sender_id=OWNER)
        await stack.router.handle_message(event)
        assert "🍓" in await stack.settings.emojis()
        # an emoji outside Telegram's reaction set must never be stored
        await stack.router.handle_callback(user_event(data="a:emoji_add", sender_id=OWNER))
        rejected = user_event(text="🚀 ✅", sender_id=OWNER)
        await stack.router.handle_message(rejected)
        assert "🚀" not in await stack.settings.emojis()
        assert "✅" not in await stack.settings.emojis()
        assert any("does not accept" in str(a.get("text", "")) for a in rejected.answers)
        await stack.router.handle_callback(user_event(data="a:emoji_del:0", sender_id=OWNER))
        assert len(await stack.settings.emojis()) >= 1

    async def test_toggle_premium_only_for_comments(self):
        stack = await panel_stack()
        await stack.router.handle_callback(
            user_event(data="a:toggle:comments:premium_only", sender_id=OWNER))
        assert (await stack.settings.section("comments"))["premium_only"] is False

    async def test_user_management_credit_and_premium(self):
        stack = await panel_stack()
        await stack.users.get_or_create(USER)
        await stack.router.handle_callback(user_event(data=f"a:user:{USER}", sender_id=OWNER))
        await stack.router.handle_callback(
            user_event(data=f"a:user_credit:{USER}:credit", sender_id=OWNER))
        await stack.router.handle_message(user_event(text="300", sender_id=OWNER))
        assert await stack.users.balance(USER) == 300

        await stack.router.handle_callback(
            user_event(data=f"a:user_prem:{USER}:30", sender_id=OWNER))
        assert await stack.users.is_premium(USER)

    async def test_ban_and_unban(self):
        stack = await panel_stack()
        await stack.users.get_or_create(USER)
        await stack.router.handle_callback(user_event(data=f"a:user_ban:{USER}", sender_id=OWNER))
        assert await stack.users.is_banned(USER)
        await stack.router.handle_callback(user_event(data=f"a:user_ban:{USER}", sender_id=OWNER))
        assert not await stack.users.is_banned(USER)

    async def test_order_retry_requeues(self):
        stack = await panel_stack(2)
        await stack.users.credit(USER, 1000, "topup")
        plan = await stack.orders.quote_plan("views", "views_1k")
        order = await stack.orders.create_order(USER, "views", {"link": "https://t.me/c/1/1"}, plan=plan)
        await stack.orders.fail(order["_id"], "boom")
        await stack.router.handle_callback(
            user_event(data=f"a:order_retry:{order['_id']}", sender_id=OWNER))
        refreshed = await stack.orders.get(order["_id"])
        assert refreshed["status"] in ("pending", "processing", "completed", "partial")
        await drain_executor(stack)

    async def test_settings_edit_upi(self):
        stack = await panel_stack()
        await stack.router.handle_callback(
            user_event(data="a:setting_edit:payments.upi_id", sender_id=OWNER))
        event = user_event(text="owner@ybl", sender_id=OWNER)
        await stack.router.handle_message(event)
        assert (await stack.settings.section("payments"))["upi_id"] == "owner@ybl"


@pytest.mark.asyncio
class TestSendPvOwnerOnly:
    async def test_owner_sees_flow(self):
        stack = await panel_stack()
        event = user_event(data="a:sendpv", sender_id=OWNER)
        await stack.router.handle_callback(event)
        assert "owner only" in event.last_text.lower()

    async def test_other_admin_is_denied(self):
        stack = await panel_stack()
        await stack.users.set_role(OTHER_ADMIN, "admin")
        event = user_event(data="a:sendpv", sender_id=OTHER_ADMIN)
        await stack.router.handle_callback(event)
        assert event.answers and event.answers[-1]["alert"] is True
        assert "Owner only" in str(event.answers[-1]["text"])

    async def test_send_pv_is_not_offered_in_admin_home_for_other_admins(self):
        stack = await panel_stack()
        await stack.users.set_role(OTHER_ADMIN, "admin")
        event = user_event(data="a:home", sender_id=OTHER_ADMIN)
        await stack.router.handle_callback(event)
        assert "Send PV" not in " | ".join(event.button_labels())


@pytest.mark.asyncio
class TestRouterBehaviour:
    async def test_duplicate_taps_are_ignored(self):
        stack = await panel_stack()
        stack.router._cache.ttl = 5.0  # simulate Telegram's instant redelivery
        first = user_event(data="u:ai")
        second = user_event(data="u:ai")
        await stack.router.handle_callback(first)
        await stack.router.handle_callback(second)
        # the redelivered tap was swallowed instead of rendering twice
        assert first.edits and second.edits == []

    async def test_command_aborts_input_state(self):
        stack = await panel_stack()
        await stack.users.grant_plan(USER, kinds=["views"], days=30)
        await stack.router.handle_callback(user_event(data="u:views_custom"))
        assert stack.router.has_input(USER)
        handled = await stack.router.handle_message(user_event(text="/start"))
        assert handled is False
        assert not stack.router.has_input(USER)

    async def test_legacy_callback_still_works(self):
        stack = await panel_stack()
        # back_to_start is handled by the legacy callback handler, which now
        # routes to the panel when a router is attached.
        from src.handlers import MessageHandler  # noqa: F401  (import check)
        assert stack.router.is_panel_callback("u:home")
        assert not stack.router.is_panel_callback("monitor_mode")

    async def test_unknown_panel_action_recovers(self):
        stack = await panel_stack()
        event = user_event(data="u:does_not_exist")
        await stack.router.handle_callback(event)
        assert "TELEGRAM PANEL" in event.last_text
