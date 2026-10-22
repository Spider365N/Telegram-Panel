#!/usr/bin/env python3
"""
Live check for the Telegram Panel — run it before/after deploying.

It needs **no** Telegram credentials: it drives the real panels, services and
order pipeline against an in-process fake Telegram transport, so you can see
exactly what the bot will do and catch regressions in seconds.

Usage
-----
    python scripts/live_check.py demo                     # full scripted journey
    python scripts/live_check.py demo --mongo mongodb://127.0.0.1:27017
    python scripts/live_check.py crash-write --mongo …    # phase 1 (then kill -9)
    python scripts/live_check.py crash-verify --mongo …   # phase 2 (after restart)

Exit code 0 means everything behaved.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The demo must work with no .env at all — give the config something to read
# *before* src.config is imported anywhere.
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "demo_hash")
os.environ.setdefault("BOT_TOKEN", "123456:DEMO")
os.environ.setdefault("ADMIN_ID", "123456789")

PASS = "\033[92m✔\033[0m"
FAIL = "\033[91m✘\033[0m"
INFO = "\033[94mℹ\033[0m"

failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(f"  {PASS if condition else FAIL} {label}")
    if not condition:
        failures.append(label)
    return condition


async def wipe_database(store) -> None:
    """Make the demo repeatable: clear every collection of the demo database."""
    from src.db.store import ALL_COLLECTIONS  # type: ignore[attr-defined]

    for collection in ALL_COLLECTIONS:
        try:
            docs = await store.find(collection, {}, limit=10000)
        except Exception:  # pragma: no cover - collection may not exist yet
            continue
        for doc in docs:
            await store.delete_one(collection, {"_id": doc["_id"]})


async def make_stack(mongo_uri: str | None, fresh: bool = False):
    """Wire up store + services, with the fake Telegram layer."""
    from src import db as db_module
    from src.db.store import JsonStore, MemoryStore, MongoStore, reset_store
    from tests.fakes import FakeTelegramClient, make_channel

    if mongo_uri:
        store = MongoStore(mongo_uri, os.getenv("MONGODB_DB", "telegram_panel_live"))
        await store.connect()
        if fresh:
            await wipe_database(store)
            print(f"{INFO} using MongoDB at {mongo_uri} (database wiped for a repeatable run)")
        else:
            print(f"{INFO} using MongoDB at {mongo_uri}")
    else:
        store = MemoryStore()
        print(f"{INFO} using in-memory store (pass --mongo for MongoDB)")
    await reset_store(store)

    from src.services.settings import get_settings_service, reset_settings_service
    from src.services.users import get_user_service, reset_user_service
    from src.services.orders import get_order_service, reset_order_service
    from src.services.payments import get_payment_service, reset_payment_service
    from src.services.channels import get_channel_service, reset_channel_service
    from src.services.engagement import get_engagement_service, reset_engagement_service
    from src.services.executor import get_order_executor, reset_order_executor
    from src.services.autoengage import get_auto_engagement, reset_auto_engagement
    from src.services.accounts import AccountStore, AccountRegistry, reset_account_registry
    from src.services.forcesub import get_force_sub_service, reset_force_sub_service
    from src.services.notify import get_notifier, reset_notifier
    from src.panels import get_panel_router, reset_panel_router
    from src.runtime import set_bot
    from src.peers import reset_peer_resolver

    for resetter in (reset_settings_service, reset_user_service, reset_order_service,
                     reset_payment_service, reset_channel_service, reset_engagement_service,
                     reset_order_executor, reset_auto_engagement, reset_account_registry,
                     reset_notifier, reset_panel_router, reset_peer_resolver,
                     reset_force_sub_service):
        resetter()

    settings = get_settings_service(store)
    users = get_user_service(store, settings)
    orders = get_order_service(store, settings, users)
    payments = get_payment_service(store, settings, users)
    channels = get_channel_service(store, settings)
    registry = AccountRegistry(AccountStore(store))
    engagement = get_engagement_service(registry, settings)
    notifier = get_notifier()
    executor = get_order_executor(engagement, orders, notifier, channels, settings, users)
    auto = get_auto_engagement(engagement, channels, users, settings, orders)
    force_sub = get_force_sub_service(settings)

    # Demo accounts: pretend the owner signed in 6 accounts.
    channel = make_channel(3852827894, title="Demo Private Channel")
    clients = {
        f"phone{i+1}": FakeTelegramClient(f"phone{i+1}", user_id=900 + i,
                                         channels={3852827894: channel})
        for i in range(6)
    }

    class DemoBot:
        """Same contract as ``TelegramBot`` for the bits services reach for."""

        async def _export_invite(self, channel_id):
            from src.peers import PeerResolver
            internal = PeerResolver.to_internal_channel_id(channel_id)
            chat = clients["phone1"].channels.get(internal) if clients else None
            if chat is not None and getattr(chat, "username", None):
                return None                     # public channels need no link
            return f"https://t.me/+fake{internal}"

    bot = DemoBot()
    bot.active_clients = clients
    import asyncio
    bot.active_clients_lock = asyncio.Lock()
    bot.tbot = FakeTelegramClient("bot", user_id=1)
    bot.store = store
    bot.account_registry = registry.bind(bot)
    bot.panel_router = None
    for path in ("reactions.delay_min", "reactions.delay_max", "reactions.auto.delay_min",
                 "reactions.auto.delay_max", "views.auto.delay_min", "views.auto.delay_max",
                 "members.delay_min", "members.delay_max",
                 "comments.delay_min", "comments.delay_max",
                 "comments.auto.delay_min", "comments.auto.delay_max"):
        await settings.set_path(path, 0)
    set_bot(bot)
    notifier.bind(bot.tbot)
    router = get_panel_router(bot)
    bot.panel_router = router
    await registry.sync_from_live_clients()

    return {
        "store": store, "settings": settings, "users": users, "orders": orders,
        "payments": payments, "channels": channels, "registry": registry,
        "engagement": engagement, "executor": executor, "auto": auto,
        "notifier": notifier, "router": router, "bot": bot, "clients": clients,
        "force_sub": force_sub,
        "channel": channel,
        "user_panel": getattr(router, "user_panel", None),
        "admin_panel": getattr(router, "admin_panel", None),
    }


async def demo(mongo_uri: str | None) -> None:
    from tests.fakes import FakeEvent, drain_executor

    OWNER = int(os.getenv("ADMIN_ID", "123456789"))
    USER = 555000111

    stack = await make_stack(mongo_uri, fresh=True)
    router = stack["router"]
    router._cache.ttl = 0.0

    print("\n1) /start as a normal user → service panel")
    start = FakeEvent(sender_id=USER, chat_id=USER)
    await router.start_for(start)
    labels = " | ".join(start.button_labels())
    check("TELEGRAM PANEL" in start.last_text, "user panel rendered")
    for expected in ("Order Views", "Auto Views", "Order Reactions", "Auto Reactions",
                     "Account Info", "Members", "Support", "My Orders",
                     "My Channels", "Add To Channel", "Add To Group"):
        check(expected in labels, f"button present: {expected}")

    print("\n2) /start as the owner → admin panel")
    admin_start = FakeEvent(sender_id=OWNER, chat_id=OWNER)
    await router.start_for(admin_start)
    check("ADMIN PANEL" in admin_start.last_text, "admin panel rendered")
    check("Send PV" in " | ".join(admin_start.button_labels()), "owner-only Send PV visible")

    print("\n3) Channel added by an unknown actor (the production case)")
    # Telegram sometimes hides who added the bot to a channel. The channel must
    # still be registered, visible to the operator and claimable.
    await stack["channels"].register(-1003852827894, None, title="Logs Test")
    listing = FakeEvent(sender_id=OWNER, chat_id=OWNER, data="u:ch")
    await router.handle_callback(listing)
    check("Logs Test" in (listing.last_text or ""), "channel visible in the panel")
    labels = listing.button_labels()
    claim_button = [l for l in labels if "Assign" in l]
    check(bool(claim_button), "an 'Assign to me' button is offered")
    if claim_button and listing.last_buttons:
        data = None
        for row in listing.last_buttons:
            for button in row:
                if isinstance(getattr(button, "data", b""), bytes):
                    text = button.data.decode()
                    if text.startswith("u:ch_claim"):
                        data = text
        if data:
            claim = FakeEvent(sender_id=OWNER, chat_id=OWNER, data=data)
            await router.handle_callback(claim)
            doc = await stack["channels"].get(-1003852827894)
            check(doc is not None and doc.get("owner_id") == OWNER,
                  "channel assigned to the operator")

    print("\n3b) Free auto reactions (only reactions are free)")
    # hand the channel to the normal demo user (the claim above went to the admin)
    await stack["channels"].set_owner(-1003852827894, USER)
    await stack["auto"].enable_free_plan(-1003852827894)
    plan_info = await stack["auto"].enable_free_plan(-1003852827894)
    check(plan_info.get("reactions") == "on (free)", f"free plan started ({plan_info})")
    check("views" not in plan_info or plan_info.get("views") == "premium only",
          "auto views stay premium-only")
    outcome = await stack["auto"].handle_new_post(-1003852827894, 1)
    reactions = outcome.get("reactions", {})
    pool = len(stack["bot"].active_clients)
    check(min(8, pool) <= reactions.get("accounts", 0) <= min(10, pool),
          f"8-10 accounts per post, capped by the pool of {pool} "
          f"({reactions.get('accounts')})")
    check(reactions.get("ok", 0) > 0, f"reactions applied ({reactions})")
    check("views" not in outcome or not outcome.get("views"),
          "a post does not touch the view counter")
    # every applied reaction came from the positive emoji pool
    from src.services.settings import DEFAULT_POSITIVE_EMOJIS as positive_list
    positive = set(positive_list)
    applied = []
    for _, client in stack["bot"].active_clients.items():
        for request in client.requests:
            if type(request).__name__ == "SendReactionRequest":
                applied.append(request.reaction[0].emoticon)
    check(applied and all(e in positive for e in applied),
          f"only positive emojis were used ({len(applied)} reactions)")

    print("\n3c) Private channel: accounts are joined, then every post gets reactions")
    # The production case: the bot is an admin with every permission, but the
    # accounts are still OUTSIDE the channel. They must be joined through the
    # invite link the bot exported, and then react on every new post.
    for client in stack["bot"].active_clients.values():
        client.member_of.discard(3852827894)
        client.known_entities.discard(3852827894)
    warm = await stack["auto"].warm_channel(-1003852827894)
    check(warm.get("ok", 0) > 0, f"accounts joined through the bot's invite link ({warm})")
    joined = [n for n, c in stack["bot"].active_clients.items() if 3852827894 in c.member_of]
    check(len(joined) == warm.get("ok"),
          f"membership recorded for all of them ({len(joined)})")
    for message_id in (7, 8, 9):
        outcome = await stack["auto"].handle_new_post(-1003852827894, message_id)
        got = outcome["reactions"]
        check(got["ok"] > 0 and got["failed"] == 0,
              f"post {message_id}: {got['ok']} reactions, {got['failed']} failed")
    doc = await stack["channels"].get(-1003852827894)
    check(bool(doc.get("invite_link")), "the invite link stays stored for later posts")

    print("\n4) Free reaction order from the panel (the bug from the crash log)")
    free = FakeEvent(sender_id=USER, chat_id=USER, data="u:free_react")
    await router.handle_callback(free)
    check("Free reactions" in free.last_text, "free plan screen shown")
    link = FakeEvent(sender_id=USER, chat_id=USER, text="https://t.me/c/3852827894/22")
    await router.handle_message(link)
    await drain_executor(stack)
    orders = await stack["orders"].list_for_user(USER)
    check(bool(orders), "order created")
    if orders:
        result = orders[0].get("result") or {}
        check(orders[0]["status"] == "completed", f"order completed ({orders[0]['status']})")
        check(not any("Could not find the input entity" in e for e in result.get("errors", [])),
              "no 'Could not find the input entity' error (private-channel fix)")
        check(result.get("ok", 0) > 0, f"reactions applied: {result.get('ok')}")

    print("\n4b) Manual order on a private channel with NO invite link stored yet")
    # Regression: a user orders reactions on a private channel before any post
    # ran, so no invite link was ever exported. The order must export one itself
    # (a *different* user, because the first one already used their free quota).
    user2 = 918273645
    await stack["store"].update_one("channels", {"_id": -1003852827894},
                                    {"$unset": {"invite_link": ""}})
    doc = await stack["channels"].get(-1003852827894)
    check(not doc.get("invite_link"), "invite link removed (fresh channel state)")
    for client in stack["bot"].active_clients.values():
        client.member_of.discard(3852827894)
        client.known_entities.discard(3852827894)
        client.joined.clear()
    before = {o["_id"] for o in await stack["orders"].list_for_user(user2)}
    tap = FakeEvent(sender_id=user2, chat_id=user2, data="u:free_react")
    await router.handle_callback(tap)
    await router.handle_message(FakeEvent(sender_id=user2, chat_id=user2,
                                          text="https://t.me/c/3852827894/31"))
    await drain_executor(stack)
    fresh_orders = [o for o in await stack["orders"].list_for_user(user2) if o["_id"] not in before]
    check(bool(fresh_orders), "a new free order was created for the second user")
    if fresh_orders:
        latest = fresh_orders[0]
        result = latest.get("result") or {}
        check(latest["status"] == "completed",
              f"order completed without a stored link ({latest['status']})")
        check(result.get("ok", 0) > 0, f"reactions applied: {result.get('ok')}")
        check(not any("Could not resolve chat" in e for e in result.get("errors", [])),
              "no 'Could not resolve chat' error (the production failure)")
    refreshed = await stack["channels"].get(-1003852827894)
    check(str(refreshed.get("invite_link") or "").endswith("fake3852827894"),
          f"a fresh invite link was exported and stored ({refreshed.get('invite_link')})")
    joined = [n for n, c in stack["bot"].active_clients.items() if 3852827894 in c.member_of]
    check(bool(joined), f"accounts joined again through the new link ({len(joined)})")

    print("\n4c) Reaction emojis: Telegram's own set only")
    # Production: the pool picked "✅" (not a Telegram reaction) and a whole post
    # lost its reactions with ReactionInvalidError.
    from src.services.settings import ALLOWED_REACTION_EMOJIS, is_allowed_reaction_emoji
    check(all(is_allowed_reaction_emoji(e) for e in ALLOWED_REACTION_EMOJIS),
          f"the allow-list holds {len(ALLOWED_REACTION_EMOJIS)} real Telegram reactions")
    await stack["settings"].set_path("reactions.emojis", ["✅", "👍", "🔥", "🚀"])
    check(await stack["settings"].emojis() == ["👍", "🔥"],
          f"unsupported emojis never reach the pool ({await stack['settings'].emojis()})")
    seen_before = {name: len(c.requests) for name, c in stack["bot"].active_clients.items()}
    outcome = await stack["auto"].handle_new_post(-1003852827894, 51)
    check(outcome["reactions"]["ok"] > 0 and outcome["reactions"]["failed"] == 0,
          f"post 51: {outcome['reactions']['ok']} reactions, {outcome['reactions']['failed']} failed")
    applied = [
        r.reaction[0].emoticon
        for name, c in stack["bot"].active_clients.items()
        for r in c.requests[seen_before.get(name, 0):]
        if type(r).__name__ == "SendReactionRequest"
    ]
    check(applied and all(e in ("👍", "🔥") for e in applied),
          f"only supported emojis were sent ({applied})")

    print("\n4d) One reaction run per post (no double reactions)")
    again = await stack["auto"].handle_new_post(-1003852827894, 51)
    check(again.get("skipped") == "post already handled",
          f"a repeated update is ignored ({again.get('skipped')})")
    older = await stack["auto"].handle_new_post(-1003852827894, 50)
    check(older.get("skipped") == "post already handled", "an older redelivered post is ignored")
    later = await stack["auto"].handle_new_post(-1003852827894, 52)
    check(later["reactions"]["ok"] > 0, f"a genuinely new post still reacts ({later['reactions']['ok']})")

    print("\n4e) A newly signed-in account joins the channels that already exist")
    from tests.fakes import FakeTelegramClient
    from src.peers import PeerResolver
    internal = PeerResolver.to_internal_channel_id(-1003852827894)
    newcomer = FakeTelegramClient("phone99", user_id=9999,
                                  channels={internal: stack["channel"]}, member_of=set())
    stack["bot"].active_clients["phone99"] = newcomer
    await stack["registry"].sync_from_live_clients()
    warm = await stack["auto"].warm_all_channels(reason="live check sign-in")
    check(newcomer.joined or internal in newcomer.member_of,
          f"the new account joined the existing channel ({warm})")
    check(warm.get("joined", 0) >= 1, f"membership(s) added for the pool ({warm.get('joined')})")
    pool_outcome = await stack["auto"].handle_new_post(-1003852827894, 53)
    check(pool_outcome["reactions"]["accounts"] <= 10,
          f"the free tier still caps the pool at 10 ({pool_outcome['reactions']['accounts']})")

    print("\n4f) Bot removed → our accounts leave the channel")
    members_before = [n for n, c in stack["bot"].active_clients.items() if internal in c.member_of]
    check(len(members_before) > 1, f"accounts were inside before removal ({len(members_before)})")
    left = await stack["auto"].leave_channel(-1003852827894)
    check(left.get("left", 0) >= 1, f"accounts left the channel ({left.get('left')})")
    still_in = [n for n, c in stack["bot"].active_clients.items() if internal in c.member_of]
    check(not still_in, f"nobody is left inside ({still_in})")
    check(await stack["channels"].get(-1003852827894) is None, "the channel record is gone")
    notes = [m["text"] for m in stack["bot"].tbot.sent_messages
             if "removed from a channel" in m["text"]]
    check(bool(notes), "the owner was told that automation stopped")
    check(all(m.get("parse_mode") == "html" for m in stack["bot"].tbot.sent_messages),
          "every notification is sent as HTML (no literal </b> codes)")
    # put the channel back for the remaining steps
    await stack["channels"].register(-1003852827894, USER, title="Logs Test",
                                     invite_link="https://t.me/+fake3852827894")
    for client in stack["bot"].active_clients.values():
        client.member_of.add(internal)
        client.known_entities.add(internal)

    print("\n5) Paid views order + wallet (views need a plan first)")
    locked = FakeEvent(sender_id=USER, chat_id=USER, data="u:buy_views:views_1k")
    await router.handle_callback(locked)
    check("premium" in (locked.last_text or "").lower(),
          "a free user cannot buy views (premium gate)")
    await stack["users"].credit(USER, 500, "demo topup")
    await stack["users"].grant_plan(USER, kinds=["views"], days=30, accounts=15)
    views = FakeEvent(sender_id=USER, chat_id=USER, data="u:buy_views:views_1k")
    await router.handle_callback(views)
    await router.handle_message(FakeEvent(sender_id=USER, chat_id=USER,
                                          text="https://t.me/c/3852827894/22"))
    await drain_executor(stack)
    balance = await stack["users"].balance(USER)
    first = (await stack["orders"].list_for_user(USER))[0]
    check(balance == 500 and float(first["price"]) == 0 and first["paid_with"] == "plan",
          f"a granted plan covers its own order — no second payment (balance {balance})")

    print("\n5a2) Per-order charging still available for the admin (regression guard)")
    await stack["settings"].set_path("premium.plan_orders_included", False)
    charged = FakeEvent(sender_id=USER, chat_id=USER, data="u:buy_views:views_1k")
    await router.handle_callback(charged)
    await router.handle_message(FakeEvent(sender_id=USER, chat_id=USER,
                                          text="https://t.me/c/3852827894/23"))
    await drain_executor(stack)
    balance = await stack["users"].balance(USER)
    check(balance == 421, f"wallet charged correctly when the plan does not cover it ({balance})")
    await stack["settings"].set_path("premium.plan_orders_included", True)

    print("\n5c) Auto comments: random positive comments on every new post")
    await stack["users"].grant_plan(USER, kinds=["views", "comments"], days=30)
    comments_channel = -1003852827894
    await stack["channels"].set_auto_comments(comments_channel, True, accounts=3)
    await stack["settings"].set_path("comments.auto.delay_min", 0)
    await stack["settings"].set_path("comments.auto.delay_max", 0)
    before_comments = sum(len(c.sent_messages) for c in stack["bot"].active_clients.values())
    comment_outcome = await stack["auto"].handle_new_post(comments_channel, 70)
    posted = [
        message["text"]
        for client in stack["bot"].active_clients.values()
        for message in client.sent_messages
        if message.get("comment_to") == 70
    ]
    check(comment_outcome.get("comments", {}).get("ok", 0) >= 1,
          f"auto comments posted ({comment_outcome.get('comments')})")
    check(all(text.strip() for text in posted), f"every comment has text ({posted[:3]})")

    print("\n5d) Auto views: enabled channel + plan = views on the next post")
    from tests.fakes import make_channel
    stranger = USER + 40
    await stack["channels"].register(-1003852827895, stranger, title="Stranger Channel",
                                     invite_link="https://t.me/+fake3852827895")
    for client in stack["bot"].active_clients.values():
        client.channels[3852827895] = make_channel(3852827895, title="Stranger Channel")
        client.member_of.add(3852827895)
        client.known_entities.add(3852827895)
    await stack["channels"].set_auto_views(-1003852827895, True)
    await stack["settings"].set_path("views.auto.enabled", True)
    without = await stack["auto"].handle_new_post(-1003852827895, 71)
    check("views" not in without, "no views for a channel nobody has a plan for")
    check((await stack["channels"].get(-1003852827895)).get("auto_views", {}).get("enabled") is True,
          "the auto-views switch stays on (it used to be disabled permanently)")
    await stack["users"].grant_plan(stranger, kinds=["views"], days=30)
    with_plan = await stack["auto"].handle_new_post(-1003852827895, 72)
    check(with_plan.get("views", {}).get("ok", 0) >= 1,
          f"views ran once the plan was granted ({with_plan.get('views')})")

    print("\n6) Top-up request → admin approval")
    await router.handle_callback(FakeEvent(sender_id=USER, chat_id=USER, data="u:topup"))
    await router.handle_message(FakeEvent(sender_id=USER, chat_id=USER, text="300"))
    await router.handle_message(FakeEvent(sender_id=USER, chat_id=USER, text="123412341234"))
    pending = await stack["payments"].pending_topups()
    check(bool(pending), "top-up request stored as pending")
    if pending:
        before = await stack["users"].balance(USER)
        await router.handle_callback(FakeEvent(sender_id=OWNER, chat_id=OWNER,
                                              data=f"a:topup_ok:{pending[0]['_id']}"))
        after = await stack["users"].balance(USER)
        check(after == before + 300, f"wallet credited after approval ({before} → {after})")

    print("\n7) Admin pricing change → users pay the new price")
    await router.handle_callback(FakeEvent(sender_id=OWNER, chat_id=OWNER, data="a:pricing"))
    await router.handle_callback(FakeEvent(sender_id=OWNER, chat_id=OWNER,
                                          data="a:plan_edit:reactions:react_50:price"))
    await router.handle_message(FakeEvent(sender_id=OWNER, chat_id=OWNER, text="59"))
    plan = await stack["settings"].get_plan("reactions", "react_50")
    check(plan and plan["price"] == 59, "price updated from the admin panel")

    print("\n8) Owner-only Send PV")
    other_admin = OWNER + 1
    await stack["users"].set_role(other_admin, "admin")
    denied = FakeEvent(sender_id=other_admin, chat_id=other_admin, data="a:sendpv")
    await router.handle_callback(denied)
    check(denied.answers and denied.answers[-1]["alert"] is True,
          "non-owner admin is refused Send PV")

    print("\n9) Accounts persisted with session strings")
    docs = await stack["registry"].store.list_accounts()
    check(all(d.get("session_string") for d in docs) if docs else False,
          f"{len(docs)} accounts stored with session strings")

    print("\n10) Crash recovery (same store, fresh registry)")
    stack["bot"].active_clients.clear()
    registry = stack["registry"]
    registry._doc_by_session.clear()
    registry._restore_attempted = False
    from tests.fakes import FakeTelegramClient as _FTC
    import src.services.accounts as accounts_module
    original_builder = accounts_module.build_client_from_session
    accounts_module.build_client_from_session = lambda s, **k: _FTC("restored", user_id=1)
    try:
        restored = await registry.restore_missing_clients()
    finally:
        accounts_module.build_client_from_session = original_builder
    check(restored > 0, f"{restored} accounts restored from the database")

    print("\n10b) Private-channel join + public channel without joining")
    public = 700700001
    from tests.fakes import FakeTelegramClient, make_channel as _make_channel
    pub_channel = _make_channel(public, username="demopublic")
    clients = stack["bot"].active_clients
    for name, client in clients.items():
        client.channels[public] = pub_channel
        client.known_entities.add(public)
    await stack["channels"].register(-100700700001, USER, title="Demo Public",
                                     username="demopublic")
    await stack["auto"].enable_free_plan(-100700700001)
    pub_outcome = await stack["auto"].handle_new_post(-100700700001, 3)
    check(pub_outcome.get("reactions", {}).get("ok", 0) > 0,
          f"public channel reacted without joining ({pub_outcome.get('reactions')})")
    check(all(not c.joined for c in clients.values()),
          "no join was attempted for the public channel")

    print("\n10c) Multi-user isolation")
    other = USER + 1
    await stack["channels"].register(-100999999999, other, title="Other User Channel")
    other_event = FakeEvent(sender_id=other, chat_id=other, data="u:ch")
    await router.handle_callback(other_event)
    check("Other User Channel" in other_event.last_text, "owner sees their channel")
    check("Logs Test" not in other_event.last_text and "Demo Private Channel" not in other_event.last_text,
          "other people's channels are not listed")
    mine = FakeEvent(sender_id=USER, chat_id=USER, data="u:ch")
    await router.handle_callback(mine)
    check("Other User Channel" not in mine.last_text, "isolation holds both ways")
    intruder = FakeEvent(sender_id=USER, chat_id=USER, data="u:ar_ch:-100999999999")
    await router.handle_callback(intruder)
    check(any(a["alert"] for a in intruder.answers), "cross-user channel access refused")

    print("\n10d) Premium plan: locked → request → owner grants → unlocked")
    newcomer = USER + 7                     # nobody has granted them anything
    locked = FakeEvent(sender_id=newcomer, chat_id=newcomer, data="u:av")
    await router.handle_callback(locked)
    check("premium" in locked.last_text.lower(), "auto views are locked for a free user")
    request = FakeEvent(sender_id=newcomer, chat_id=newcomer, data="u:reqplan:views")
    await router.handle_callback(request)
    check("Request sent" in request.last_text, "the user can ask the owner")
    await stack["users"].grant_plan(newcomer, kinds=["views"], days=30, accounts=15)
    check(await stack["users"].has_feature(newcomer, "views"), "views plan granted")
    check(await stack["users"].has_feature(newcomer, "comments") is False,
          "other services stay locked (per-kind plans)")
    unlocked = FakeEvent(sender_id=newcomer, chat_id=newcomer, data="u:av")
    await router.handle_callback(unlocked)
    check("premium service" not in unlocked.last_text.lower(),
          "the unlocked screen renders normally")
    await stack["users"].grant_plan(newcomer, unlimited=True)
    limits = await stack["users"].effective_limits(newcomer)
    check(limits["unlimited"] is True and limits["accounts"] >= 10 ** 6,
          "unlimited plan removes every limit")

    print("\n10e) Pool self-healing: a dropped account comes back by itself")
    live_before = await stack["registry"].available_count()
    victim = next(iter(stack["bot"].active_clients))
    await stack["bot"].active_clients[victim].disconnect()
    dropped = await stack["registry"].available_count()
    check(dropped == live_before - 1,
          f"the crashed account left the pool ({live_before} → {dropped})")
    healed = await stack["registry"].revive_disconnected()
    check(healed["reconnected"] >= 1 and await stack["registry"].available_count() == live_before,
          f"the supervisor put it back ({healed}, pool {await stack['registry'].available_count()})")
    picked = await stack["registry"].pick(live_before)
    check(len(picked) == live_before,
          f"the next operation uses the full pool again ({len(picked)} of {live_before})")

    print("\n10f) Never log out on a connection error — only on a ban")
    from src.utils import is_fatal_session_error
    check(is_fatal_session_error(RuntimeError("Cannot send requests while disconnected")) is False,
          "a disconnect is not treated as a logout")
    check(is_fatal_session_error(RuntimeError("sqlite3.OperationalError: database is locked")) is False,
          "a sqlite lock is not treated as a logout")
    check(is_fatal_session_error(RuntimeError("The phone number is banned")) is True,
          "a ban does log the account out")

    print("\n10g) 3 OTP / 2FA attempts before the flow restarts")
    import asyncio as _asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock as _Mock
    from telethon.errors import PhoneCodeInvalidError
    from src.client import AccountHandler, MAX_AUTH_ATTEMPTS

    sent: list = []

    class _FakeBotApi:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append(str(text))
            return SimpleNamespace(delete=AsyncMock())

    fake_client = SimpleNamespace(
        is_connected=lambda: True,
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        sign_in=AsyncMock(side_effect=PhoneCodeInvalidError(request=_Mock())),
        is_user_authorized=AsyncMock(return_value=True),
    )
    handler_tbot = SimpleNamespace(
        handlers={}, _conversations={}, _conversations_lock=_asyncio.Lock(),
        active_clients={}, config={"clients": {}}, client_manager=None,
        tbot=_FakeBotApi(),
    )
    handler = AccountHandler(handler_tbot)
    handler.finalize_client_setup = AsyncMock()
    handler_tbot.handlers["temp_client"] = fake_client
    handler_tbot.handlers["temp_phone"] = "+911234567890"
    event = FakeEvent(sender_id=USER, chat_id=USER, text="00000")
    check(MAX_AUTH_ATTEMPTS == 3, "three attempts are configured")
    await handler.code_handler(event)
    check(handler_tbot.handlers.get("temp_attempts") == 1
          and handler_tbot.handlers.get("temp_client") is fake_client,
          "a wrong code keeps the sign-in alive (attempt 1/3)")
    check(any("attempt(s) left" in message for message in sent),
          "the user is told how many tries are left")
    await handler.code_handler(event)
    check(handler_tbot.handlers.get("temp_attempts") == 2,
          "the second wrong code still keeps the client (attempt 2/3)")
    await handler.code_handler(event)
    check("temp_client" not in handler_tbot.handlers,
          "after the third wrong code the flow is cancelled cleanly")
    check(any("cancelled" in message for message in sent),
          "the user is told the setup was cancelled")

    print("\n10h) Plan activation notification with details + duration")
    notified = USER + 55
    plan = await stack["users"].grant_plan(notified, kinds=["all"], days=90)
    notices = [m["text"] for m in stack["bot"].tbot.sent_messages
               if "plan is now active" in m["text"]]
    target = [text for text in notices if "90 day" in text]
    check(bool(target), "the user was told their plan went live, with the duration")
    if target:
        check("views" in target[0].lower() and "comments" in target[0].lower(),
              "the notification lists the services the plan covers")

    print("\n10i) Auto-join bookkeeping stays in the logs")
    chat_texts = [m["text"] for m in stack["bot"].tbot.sent_messages]
    check(not any("joined your channels" in text for text in chat_texts),
          "no 'accounts joined your channels' message is sent to the chat")

    print("\n10j) Round 7 — Buy Premium, channel slots, locks and plan requests")
    buyer = USER + 7
    await stack["users"].get_or_create(buyer, username="round7", first_name="Round7")
    premium_event = FakeEvent(sender_id=buyer, chat_id=buyer, data="u:prem")
    await router.handle_callback(premium_event)
    premium_text = premium_event.last_text or ""
    check("BUY PREMIUM" in premium_text.upper(), "Buy Premium screen replaces Add Money")
    plans = await stack["payments"].premium_plans()
    check(all(str(p.get("title")) in premium_text for p in plans),
          "every plan is listed with its details")
    check("REQUEST ACTIVATION" in premium_text.upper(),
          "the screen offers Request activation")

    premium_event = FakeEvent(sender_id=buyer, chat_id=buyer,
                              data=f"u:prem_plan:{plans[0]['id']}")
    await router.handle_callback(premium_event)
    check("REQUEST ACTIVATION" in (premium_event.last_text or "").upper(),
          "the plan detail screen shows the numbers and the request button")
    contact_event = FakeEvent(sender_id=buyer, chat_id=buyer, data="u:contact")
    await router.handle_callback(contact_event)
    check("CONTACT THE OWNER" in (contact_event.last_text or "").upper(),
          "Contact owner screen renders")
    check("](http" not in (contact_event.last_text or ""),
          "links are real anchors, never markdown")

    request_event = FakeEvent(sender_id=buyer, chat_id=buyer,
                              data=f"u:prem_req:{plans[0]['id']}")
    await router.handle_callback(request_event)
    from src.services.requests import get_plan_request_service
    queue = get_plan_request_service(users=stack["users"])
    pending = await queue.list(status="pending")
    check(len(pending) == 1 and int(pending[0]["user_id"]) == buyer,
          "the request lands in the owner's queue")

    requests_event = FakeEvent(sender_id=OWNER, chat_id=OWNER, data="a:requests")
    await router.handle_callback(requests_event)
    check("PLAN REQUESTS" in (requests_event.last_text or "").upper(),
          "the admin panel shows the requests screen")
    check(str(buyer) in (requests_event.last_text or ""),
          "the waiting user is listed for the owner")

    approve_event = FakeEvent(sender_id=OWNER, chat_id=OWNER,
                              data=f"a:req_ok:{pending[0]['_id']}")
    await router.handle_callback(approve_event)
    plan_now = await stack["users"].plan_of(buyer)
    check(bool(plan_now), "approving activates the plan for that user")
    check(await queue.pending_count() == 0, "the queue is cleared after approval")

    # channel allowance: 2 free, then plan slots, and channels stay locked
    free_limit = (await stack["settings"].section("channels")).get("free_limit", 2)
    check(free_limit == 2, "2 free auto-reaction channels per user")
    ch_user = USER + 8
    await stack["users"].get_or_create(ch_user, username="chan7", first_name="Chan7")
    for index in range(3):
        cid = -10090000 - index
        await stack["channels"].register(cid, ch_user, title=f"Round7 {index}")
        verdict = await stack["channels"].can_enable_auto(cid, ch_user, stack["users"])
        if verdict["allowed"]:
            await stack["channels"].set_auto_reactions(cid, True)
        if index == 2:
            check(verdict["allowed"] is False,
                  "the 3rd channel needs a paid channel plan")
    slots = await stack["channels"].channel_slots_of(ch_user, stack["users"])
    check(slots["used"] == 2 and slots["total"] == 2, "exactly 2 channels run for free")
    check(await stack["channels"].remove(-10090000) == 0, "an added channel is locked")
    check(await stack["channels"].remove(-10090000, force=True) == 1,
          "only the owner can delete a locked channel")
    await stack["users"].grant_plan(ch_user, kinds=["channels"], days=30, channels=3)
    check((await stack["channels"].channel_slots_of(ch_user, stack["users"]))["total"] == 5,
          "the channel plan raises the allowance instantly")

    channel_plan_event = FakeEvent(sender_id=ch_user, chat_id=ch_user, data="u:chplan")
    await router.handle_callback(channel_plan_event)
    check("CHANNEL" in (channel_plan_event.last_text or "").upper(),
          "the extra-channel plans screen renders")
    channel_admin_event = FakeEvent(sender_id=OWNER, chat_id=OWNER, data="a:limits")
    await router.handle_callback(channel_admin_event)
    check("free channels" in (channel_admin_event.last_text or "").lower(),
          "the admin panel shows the free-channel setting")

    print("\n10k) Round 7 — the web dashboard is mounted and live-synced")
    from src.health import HealthServer
    from src.webadmin import WebAdmin
    from aiohttp import web
    web_app = web.Application()
    HealthServer(0, bot=stack["bot"])._json  # sanity: the health app still builds
    dashboard = WebAdmin(stack["bot"])
    dashboard.install(web_app)
    routes = {route.resource.canonical for route in web_app.router.routes()}
    for path in ("/admin", "/admin/login", "/admin/api/login", "/admin/api/overview",
                 "/admin/api/requests", "/admin/api/users", "/admin/api/plans",
                 "/admin/api/channels", "/admin/api/operators", "/admin/api/settings"):
        check(path in routes, f"web route mounted: {path}")
    settings_now = await stack["settings"].get()
    check("channels" in settings_now, "settings carry the channels section the web editor edits")

    print("\n10l) Round 8 — force subscribe (owner-only join gate)")
    from tests.fakes import make_channel as _make_channel
    from src.services.forcesub import get_force_sub_service
    gate_chat = _make_channel(9966001, username="livecheckgate", title="Live Check Gate")
    gate_chat.id = 9966001
    stack["bot"].tbot.channels[9966001] = gate_chat
    stack["bot"].tbot.known_entities.add(9966001)
    stack["bot"].tbot.channel_members[9966001] = set()
    gate = get_force_sub_service(settings=stack["settings"], bot=stack["bot"])
    added = await gate.add_channel("@livecheckgate", title="Live Check Gate", added_by=OWNER)
    check(added["id"] == 9966001, "owner added a force-subscribe channel")
    check(await gate.set_enabled(True) is True, "the gate is switched on")

    gated_user = USER + 9
    gated = FakeEvent(sender_id=gated_user, chat_id=gated_user)
    await router.start_for(gated)
    labels = gated.button_labels()
    check(any("I joined" in label for label in labels), "a non-member sees the join screen")
    check(not any("Auto Reactions" in label for label in labels),
          "the panel is not reachable before joining")

    stack["bot"].tbot.channel_members[9966001].add(gated_user)
    verify = FakeEvent(sender_id=gated_user, chat_id=gated_user, data="u:fscheck")
    await router.handle_callback(verify)
    check(any("Auto Reactions" in label for label in verify.button_labels()),
          "after joining, verify opens the panel")

    owner_event = FakeEvent(sender_id=OWNER, chat_id=OWNER)
    await router.start_for(owner_event)
    check("ADMIN PANEL" in (owner_event.last_text or "").upper(),
          "the owner is never blocked by the gate")

    admin_screen = FakeEvent(sender_id=OWNER, chat_id=OWNER, data="a:fs")
    await router.handle_callback(admin_screen)
    check("FORCE SUBSCRIBE" in (admin_screen.last_text or "").upper()
          and "Live Check Gate" in (admin_screen.last_text or ""),
          "the owner sees the channel list in the admin panel")

    await gate.set_enabled(False)
    await gate.remove_channel(9966001)
    check(await gate.channels() == [], "the owner removed the channel again")

    if mongo_uri:
        print("\n11) Raw database contents")
        for collection in ("users", "accounts", "orders", "settings", "transactions",
                           "channels", "peers", "plan_requests"):
            count = await stack["store"].count(collection, {})
            check(count > 0 or collection == "peers", f"{collection}: {count} documents")

    print("\n" + ("=" * 60))
    if failures:
        print(f"{FAIL} {len(failures)} check(s) failed:")
        for item in failures:
            print(f"   - {item}")
        raise SystemExit(1)
    print(f"{PASS} all checks passed — the panel behaves as expected")


async def crash_write(mongo_uri: str) -> None:
    """Write state, then die hard (simulating a crash) without cleanup."""
    stack = await make_stack(mongo_uri, fresh=True)
    await stack["users"].credit(777001, 1000, "crash test")
    await stack["channels"].register(-100777001, 777001, title="Crash Channel")
    docs = await stack["registry"].store.list_accounts()
    print(f"{INFO} wrote user 777001, channel -100777001 and {len(docs)} accounts")
    print(f"{INFO} killing the process hard (exit code 9) — data must survive")
    sys.stdout.flush()
    os._exit(9)


async def crash_verify(mongo_uri: str) -> None:
    """Verify everything written by the crashed process is still there."""
    store_bundle = await make_stack(mongo_uri)
    users = store_bundle["users"]
    channels = store_bundle["channels"]
    accounts = store_bundle["registry"].store

    print("\nVerifying data written before the crash:")
    check(await users.balance(777001) == 1000, "wallet balance survived")
    channel = await channels.get(-100777001)
    check(channel is not None, "channel registration survived")
    docs = await accounts.list_accounts()
    check(bool(docs) and all(d.get("session_string") for d in docs),
          f"{len(docs)} account session strings survived")
    orders = await store_bundle["orders"].stats()
    check(orders["total"] >= 0, "orders collection readable")

    print("\n" + ("=" * 60))
    if failures:
        print(f"{FAIL} {len(failures)} check(s) failed after the crash")
        raise SystemExit(1)
    print(f"{PASS} everything survived the hard crash")


def main() -> None:
    parser = argparse.ArgumentParser(description="Live checks for Telegram Panel")
    parser.add_argument("mode", choices=["demo", "crash-write", "crash-verify"])
    parser.add_argument("--mongo", default=os.getenv("MONGODB_URI", ""),
                        help="MongoDB URI (optional for demo, required for crash-*)")
    args = parser.parse_args()

    if args.mode in ("crash-write", "crash-verify") and not args.mongo:
        print("crash-* modes need --mongo mongodb://…")
        raise SystemExit(2)

    if args.mode == "demo":
        asyncio.run(demo(args.mongo or None))
    elif args.mode == "crash-write":
        asyncio.run(crash_write(args.mongo))
    else:
        asyncio.run(crash_verify(args.mongo))


if __name__ == "__main__":
    main()
