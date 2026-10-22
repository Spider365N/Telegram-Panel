# Changelog

## [3.5.1] - 2026-10-17

**Hotfix — buttons and badges were printed as code on the dashboard.**

Every table cell went through ``fmtCell()``, which escapes HTML. That is right for
data (a channel title must never inject markup), but the cells the renderers build
themselves — action buttons, plan/ban/status tags, the on/off and lock tags, the
force-sub link — were escaped too, so the operator saw
``<button class="b sm d" data-act="op_del" …>remove</button>`` printed inside the
cell instead of a button. Visible on Users, Channels, Accounts, Operators, Force
Sub, Requests and Orders — on desktop and (worse) on a phone, where the escaped
string stretched the card off screen.

* Pre-built markup is now wrapped in ``html(…)`` and passed through untouched;
  every value interpolated inside it still goes through ``esc()``.
* Requests now colour their status tag again (the duplicate ``status_tag`` column
  bypassed the tag renderer).
* Cells wrap long tokens (``overflow-wrap:anywhere``), so an id or invite link can
  not push a column off the screen.
* Phone: the tab strip now sticks below the **measured** header height
  (``--hdr``) instead of a guessed 52 px, so it is no longer half hidden when the
  header wraps; action buttons wrap onto a second line instead of overflowing.
* ``scripts/dashboard_dom_check.py`` now also walks **every tab** and fails if any
  markup is ever rendered as text; new static tests guard the ``html(…)`` contract.
* ``scripts/make_dashboard_preview.py`` builds ``dashboard-preview.html``: the
  real dashboard running live next to a real 390 px render of itself.

## [3.5.0] - 2026-10-17

**Round 8 — a dashboard you can actually use on a phone, edits that survive the
auto refresh, and an owner-only force-subscribe gate.**

### Web dashboard: soft refresh (no more wiped edits)
* Auto refresh no longer replaces the page. Every tab exposes *regions*
  (``#kpis``, ``#recent-orders``, ``#recent-users``, the table of each tab) and a
  12 s tick patches **only the regions whose data changed** — unchanged regions
  are not touched at all (content fingerprint), so scroll position, focus and
  typed text stay exactly where they were.
* The tick is skipped entirely while a field is focused or has unsaved changes;
  the header badge switches to **“✎ editing — live paused”** so it is obvious.
* Tabs that are pure forms (Plans, Pricing, Settings) have **no** auto refresh at
  all — those screens say so.
* “↻” still does a deliberate full re-render of the current tab.

### Web dashboard: mobile/Android layout
* Phone breakpoint (≤760px): sticky compact header, swipeable tab strip with
  44 px touch targets, 16 px inputs (iOS no longer zooms), full-width controls.
* Tables become **stacked label/value cards** on phones — nothing is clipped and
  no horizontal scrolling is needed; every cell carries its label.
* Recent orders / latest users and every long table live in a **fixed-height
  scroll box** with a sticky header, so the dashboard page never grows with the
  data.
* ``viewport-fit=cover``, theme colour and an installable-web-app hint for
  Android/iPhone browsers.

### Force subscribe (owner only)
* New ``force_sub`` settings section + ``ForceSubService``: add/remove/list
  channels, enable/disable, 60 s per-user cache.
* Accepts ``@username``, ``t.me/…`` links, private invite links (the bot must
  already be a member) and raw ``-100…`` ids; adding a title is optional
  (``@channel | Updates``).
* Users who are not members of **all** channels see a join screen with one button
  per channel plus **✅ I joined — verify**; the gate runs before ``/start`` and
  before every other panel button.
* Owners/admins are never blocked, banned users still see the ban message, and a
  channel that cannot be checked (bot kicked, network error, deleted channel)
  **fails open** — a mistake here can never lock every user out.
* Managed from the bot (owner-only **📢 Force Subscribe** screen in the admin
  panel) and from the dashboard (**Force Sub** tab, owner-only, same list).
* Web-side hardening: only owners can add/remove operators or touch the gate, so
  an admin can no longer promote themselves.

### Verification
* **897 tests** green against a real MongoDB (17 new: gate service, bot UX,
  owner-only screens, web API, dashboard contract).
* ``scripts/live_check.py``: **137 checks** green (adds the whole gate flow).
* ``scripts/web_admin_smoke.py``: **36 checks** green over real HTTP.
* ``scripts/dashboard_dom_check.py``: real-DOM (jsdom) check proving a tick keeps
  a typed value, patches regions in place and never re-renders the page.
* ``scripts/railway_boot_sim.py``: production-style boot healthy.

## [3.4.0] - 2026-10-17

**Round 7 — polls out, Buy Premium in, channel slots with locks, and a brutalist
web admin panel that is live-synced with the bot.**

### Polls are gone
* The poll feature is removed from **every** place: user panel, admin panel,
  plan segment, engagement service, order kinds, help text, config defaults and
  the live check. Existing data is untouched — nothing tries to read it.
* Plan defaults that used to carry "poll votes" now carry the channel slots
  instead, so nothing has to be re-bought.

### Add Money → 👑 Buy Premium
* The wallet button is now **Buy Premium**: every published plan is listed with
  days • accounts • extra channels • what it unlocks, exactly like the order
  view, plus the *how to activate* steps.
* Each plan opens a detail screen with **📨 Request activation** and
  **💬 Contact owner**. The owner receives the request with the plan, price and
  duration, takes the payment and activates it — or pays from the wallet if the
  balance is already there.
* Requests are stored in MongoDB (``plan_requests``) so a restart never loses a
  pending one; approve/reject works from the bot panel, the notification and the
  web dashboard.

### Auto-reaction channels: 2 free, then a paid allowance — and locked
* ``settings.channels.free_limit`` (default **2**) is the free allowance; the
  owner can change it from the admin panel or the dashboard.
* A **separate channel plan segment** (``ch_1`` ₹99 / ``ch_3`` ₹249 /
  ``ch_10`` ₹699) and matching slots on the regular plans. A user who needs more
  channels picks a plan or asks for a custom count ("how many channels do you
  need?" → the exact number is requested from the owner).
* Every channel a user adds is **locked**: it can be disabled, but only an
  admin/owner can remove or re-assign it — the free allowance cannot be farmed
  by deleting and re-adding.
* Trying to switch on a third channel explains the limit and offers the channel
  plans plus *Contact owner*.

### 🖤 Web admin panel (brutalism, live)
* ``/admin`` on the same HTTP port as ``/health`` — nothing else to deploy. One
  page, zero build step, all styling inline (works behind any proxy).
* Tabs: Dashboard · **Requests** · Users · Plans · Pricing · Channels · Orders ·
  Accounts · **Operators** · Settings. Everything the bot knows is editable:
  plans (create/edit/delete, including channel slots), every settings section as
  raw JSON, wallet credit/debit, plan grant/revoke, ban/unban, DM a user,
  channel lock/owner/auto toggles/delete, order cancel/retry, pool revive, and a
  broadcast tool.
* **Multiple owners**: the Operators tab adds/removes owners and admins; the new
  role applies instantly in the bot, the panels and the dashboard.
* Login = operator Telegram user id + ``ADMIN_PANEL_PASSWORD`` (or the generated
  password printed in the log). Signed http-only session cookie, owner/admin
  check against the live user service. ``WEB_ADMIN=0`` disables the panel.

### User-friendly formatting + speed
* Real anchor links everywhere (``tg_link`` / ``html_link``) — no more markdown
  brackets leaking into the chat.
* One screen, one header style; the duplicated *Buy Premium*/Auto Reactions rows
  are gone (a single button per feature).
* Cheaper hot paths: channel allowance lookups read one section, the dashboard
  and the panel reuse the same service objects, and the live check exercises the
  full round-7 flow end to end.

### Verification
* **879 tests** green against a real MongoDB (18 new round-7 tests: channel
  slots, locks, requests, formatting, polls-are-gone).
* ``scripts/live_check.py``: **129 checks** green, including the whole
  Buy Premium → request → approve → channel plan → lock flow.
* ``scripts/web_admin_smoke.py``: **25 checks** green — boots the real health
  server, logs in over HTTP and proves a dashboard write is visible to the bot's
  own services instantly.
* ``scripts/railway_boot_sim.py``: production-style boot healthy.

## [3.3.1] - 2026-10-16

**Cleanup release: one source of truth for account status.**

* The legacy ``config['inactive_accounts']`` dict is no longer written anywhere.
  An account only ever becomes "inactive" when Telegram reports a ban/block/
  revoked session, and that lives in the account registry (MongoDB) — the old
  key is merged in read-only so an existing deployment keeps its history.
* ``client.SessionManager.show_inactive_accounts``, the operator CLI view and
  the bot's *Groups per account* screen all read the registry now; an account
  that authorizes again is marked active there automatically.
* ``AccountRegistry.doc_id_for`` resolves every spelling of a session name
  (``919000000002``, ``+919000000002``, ``919000000002.session``), so usage
  counters (reactions/views/comments/poll votes) and status updates are never
  silently skipped for real accounts.
* 2 new regression tests (878 total) + the full live check (112 checks) green.

## [3.3.0] - 2026-10-16

**The "every error in the operator's log, fixed at the root" release.**

### Sign-in survives mistakes (3 attempts)

* A wrong OTP or a wrong 2-step password used to throw the whole flow away: the
  client was closed, the state was wiped and the operator had to start from
  ``/start`` again (the production log showed ``The phone code entered was
  invalid`` → ``AuthRestartError`` → *SendCodeRequest* from scratch).
* The same client and phone now stay in place for **3 attempts** (invalid code,
  invalid password, "not authorized after sign_in"), with the remaining count in
  every message. Only after the third failure is the flow cancelled cleanly with
  an explanation; expired codes and ``AuthRestartError`` re-send a fresh code
  without spending an attempt, and a FloodWait keeps the state so the user can
  continue when the wait is over.

### Accounts never log out — they recover

* Connection problems, ``sqlite3.OperationalError: database is locked``,
  ``Cannot send requests while disconnected``, timeouts, EOF, "invalid Peer":
  **none of them** remove an account any more. Only a ban/block/revoked session
  does (``is_fatal_session_error``).
* Two clients on one ``.session`` file was the actual cause of the sqlite lock —
  and of the *"6 accounts signed in, only 5 react"* report. A second sign-in
  attempt for the same phone now reuses the client it already has, and the
  abandoned client is disconnected before a new session file is opened.
* A background pool supervisor reconnects dropped clients, rebuilds database-only
  accounts from their session string and re-checks the pool every pass;
  ``pick_for_operation`` heals the pool before selecting accounts, so a batch
  can no longer run with a shrunken pool.

### One plan unlocks everything it lists

* ``orders.create_order`` asks *who pays*: owner/admin → never charged, an active
  plan that covers the service → charged to the plan (``paid_with="plan"``), and
  a plan-covered quote is recorded as a **plan** payment instead of a "free"
  order with no owner.
* Auto views was switched **off** permanently for a channel as soon as the
  account that was *attributed* to it had no views plan — while the paying user
  had one (the production log: ``Auto views switched off …``). The flag is now
  kept, the reason is logged once, and entitlement is resolved from the owner,
  the person who added the bot **and** every sponsor (the buyer of a paid order),
  so granting one plan turns the service back on.
* A channel whose owner was only *guessed* (Telegram hid the adder) is
  re-attributed to the real adder; an explicit owner is never stolen, and
  ``set_owner`` registers the new owner as a sponsor.

### Polls: every kind

* ``resolve_poll_target`` understands native polls, quizzes, multiple-choice
  polls, closed polls (with the reason) and **bot polls whose options are inline
  buttons** — the production failure (``Target message is not a poll`` on a paid
  poll order that then refunded) cannot happen any more.
* Votes are cast per account with ``SendVoteRequest`` (or a real button press
  with ``GetBotCallbackAnswerRequest``), the account's usage counter moves, and
  a paid poll order completes instead of refunding.

### Auto comments, notifications, log noise

* New **Auto Comments**: every new post gets random positive comments from the
  configured pool (admin-editable), N accounts per post, plan-gated.
* Auto-join bookkeeping is written to the log only — it is no longer posted into
  the channel.
* Plan activation sends the user a notification with the plan title, the
  duration in days and the services it covers.
* An unreachable user is remembered once instead of being re-notified (and
  re-logged) on every message.
* Telegram's normal reconnect chatter (``Server closed the connection: 0 bytes
  read``, ``Server resent the older message``) is filtered out of the log.

## [3.2.0] - 2026-10-16

**The "notifications leak their tags, emojis get refused, accounts never fill the
pool, removing the bot leaves accounts behind" release.**

### Notifications are rendered, not printed

* ``Notifier.send`` and ``TelegramBot.notify_admin`` never asked Telegram for a
  parse mode, so the operator received literal ``</b>`` and ``<code>`` codes.
  Both now use ``src.utils.detect_parse_mode``: any message carrying markup is
  sent with ``parse_mode="html"``, plain text stays plain.

### Reactions use Telegram's own emoji set

* Telegram accepts **only** its own reaction list — the production pool picked
  ``✅``, which the server refuses with ``ReactionInvalidError`` and a whole post
  lost its reactions. ``ALLOWED_REACTION_EMOJIS`` (73 entries) is now enforced:
  stored lists are filtered, ``reactions.emojis`` is repaired in the database on
  boot, and the default positive set no longer contains ``✅``.
* If a channel still refuses an emoji, ``send_reaction()`` swaps in another one
  (up to 3 tries), remembers the refused emoji for the rest of the run and tells
  the admins once. A post can no longer be lost to a bad emoji.
* The admin panel refuses unsupported emojis with a clear message instead of
  storing them.
* The failure alert now explains the real cause (refused emoji / flood wait /
  revoked account / manual approval / invite problem) instead of always blaming
  channel permissions, with the reason trimmed at a word boundary.

### The pool actually fills up

* A newly signed-in account now joins **every** channel that needs members —
  private channels added earlier included — up to the free cap (10) or the
  owner's plan. Runs in the background right after the sign-in message, and once
  after every boot (``boot warm-up``).
* The warm-up joins the whole allowed pool (not a random 8-10 slice), so each
  post can still pick a random 8-10 accounts from a full pool.

### Removing the bot cleans up

* Removing the bot from a channel now makes **every** pool account leave that
  channel (``channels.LeaveChannelRequest``), drops the channel record and the
  cached access hashes, and tells the owner what happened.

### No more double reactions

* Both the channel-post handler and the generic message handler saw the same
  update, so every post was reacted to twice (the log showed two runs ~8 s
  apart). The highest message id per channel is stored and re-delivered or older
  posts are skipped.

### Log noise and small UX fixes

* Optional environment variables (``ADMIN_IDS``, ``OWNER_ID``, ``STORAGE_BACKEND``
  …) log at INFO instead of WARNING, so platforms stop showing them as errors;
  a missing ``clients.json`` is INFO too (normal on a fresh deployment).
* "Insufficient balance" now tells the price, the balance and the way to top up,
  and the panel opens a **💳 Add Money** screen instead of a bare toast.

### Tests

* ``tests/test_production_log_2_fixes.py`` (20) — HTML notifications, emoji
  filtering/fallback, one-reaction-per-post, new accounts joining existing
  channels, leaving on removal, the top-up screen.
* Live check gained steps 4c–4f; the boot simulation now resets its own database
  (quoted env values are parsed first) so it is repeatable.
* Full suite: **839 passed**, live check green, boot sim green.


## [3.1.0] - 2026-10-16

**The "accounts cannot join my channel" release.** The operator's log showed every
reaction failing with `Could not resolve chat -1003852827894 …` even though the bot
had all channel permissions — this was a code bug, not a permission problem.

### The real join bug (root cause)

* Telegram answers `messages.CheckChatInvite` in three shapes: `ChatInviteAlready`
  and `ChatInvitePeek` **have** a `.chat`, a plain `ChatInvite` — the *not a member
  yet* answer, i.e. the only one that matters — has **none**. The old `join()`
  read `getattr(result, "chat", None)` and treated the missing chat as "nothing to
  do", so `ImportChatInviteRequest` was **never sent**: `join()` returned `None`
  and every account stayed outside the private channel.
* `PeerResolver.join()` now always imports a plain `ChatInvite`
  (`join_by_invite()`), re-resolves the entity afterwards, maps
  `InviteRequestSentError` to "this channel approves new members manually",
  expired/invalid hashes to "the invite link is no longer valid", and re-raises
  `FloodWaitError` instead of swallowing it.
* The test fake now mirrors Telegram's three real answer shapes — the old fake
  returned a `.chat` on the check, which is why the suite stayed green for two
  rounds while production was broken.

### Invite links heal themselves

* `EngagementService.invite_hash_for(chat_id, force=True)` asks the running bot for
  a fresh invite link whenever none is stored (or the stored one is dead) and saves
  it on the channel. Manual orders now work on a channel the bot has *just* been
  added to, before a single post has run.
* A batch that failed because nobody could join is retried **once** with a fresh
  link — for orders (`OrderExecutor._run_reactions`) and for auto reactions
  (`AutoEngagement._react`). The retry does not consume a second credit.
* Public channels still never join; a channel that genuinely cannot be joined is
  reported to the owner with the real reason instead of a silent "0 ok".

### Also fixed / polished

* **Channel ownership**: when Telegram hides who added the bot, the channel is now
  attributed to its creator (or to the only human admin), so the owner sees it in
  the panel immediately instead of `owner=None`.
* **Restored accounts keep their name** (`build_client_from_session(session_name=)`)
  — the pool no longer logs "Unknown".
* **Auto views** are skipped for owners without the `views` entitlement and never
  spend the pool on ownerless channels.
* **Order Reactions** screen is honest about an empty pool ("pool is empty", plus
  how to sign in) and the free quote never promises more accounts than are signed
  in.
* **Post links are validated**: a non-link message re-prompts instead of creating
  a doomed order ("Unrecognised Telegram link: Test Message" is gone).

### Tests

* `tests/test_free_order_flow.py` (9) — the free order path, invite self-healing,
  dead-link replacement, retry cost, honest failure reporting.
* `tests/test_join_flow.py` (16) — Telegram's three invite shapes, approval-only
  channels, self-healing links and channel attribution.
* Full suite: **818 passed**, every live-check step green, Railway boot simulation
  healthy — the fake clients must always mirror Telegram, not the other way round.


## [3.0.0] - 2026-10-16

Free-tier auto reactions rebuilt to the operator's specification, a full premium
plan system, strict per-user isolation, and the panel/UX pass.

### The free tier (auto reactions)

* **Free for every user, forever** — the moment the bot is added (or a channel is
  claimed) reactions start on *every* new message, media, file or post.
* **8–10 random accounts per post** (defaults; admin-editable), automatically
  reduced to the pool size, capped by the user's plan, each account applying a
  **random positive emoji**.
* **1–3 seconds** random delay between accounts (was 5–25s) so the activity
  looks human.
* **Public channels are never joined** — accounts resolve `@username` and react.
  **Private channels** are joined through an invite link the bot creates, and if
  the export failed the bot asks for a fresh link on the next post and retries.
* Accounts are **warmed into the channel right after the bot is added**, so post
  #1 already gets its reactions.
* Reactions **never touch the view counter** — views stay a separate, paid
  service with its own accounts (`tests/test_free_reactions_spec.py`).
* When nothing could be applied the owner gets the *reason* (empty pool, missing
  invite permission) instead of a silent `0 ok, 0 failed`.

### Premium (everything except reactions)

* `UserService.grant_plan(kinds, days, accounts)` — per-service plans:
  `reactions`, `views`, `comments`, `polls`, `members`, `all`, plus the special
  **unlimited** grant (no limits, no expiry) reserved for the owner.
* Only reactions are free; views, auto views, comments, polls and members are
  **activated by the owner/admin** after a chat. Every locked service shows a
  *Request activation* button that pings the owner with the user's id.
* Owner commands: `/grant <id> [service|all] [days] [accounts]`,
  `/unlimited <id>`, `/revoke <id>` — all auto-registered in the bot menu.
* Admin panel → 👥 Users → *Give plan* walks service → duration → unlimited.
* Expired plans fall back to the free tier automatically; the per-kind gate
  means a views plan does **not** unlock comments.

### Isolation

* A user sees and manages **only their own** channels; another user's channel is
  invisible in lists and every direct callback is refused
  (`PanelBase.manage_target`).
* Owner keeps super powers (sees everything). Admins see their own channels plus
  channels whose adder Telegram never reported — never somebody else's.
* `/mychannels` lists the caller's channels with live automation state.

### Panel & UX

* One message per panel: every button and every text prompt **edits** the same
  message; no more chat spam (`tests/test_panel_polish.py` renders all screens).
* Reorganised user panel: *Auto Reactions (free)* first, premium services marked
  👑, *My Channels* is the hub, paged listing with per-channel status.
* Formatting pass: fixed the `</b>accounts` glue and added a regression test
  that scans all rendered screens **and** the source for glued HTML.
* Logging goes to **stdout** so hosted platforms stop tagging INFO lines as
  errors, telethon/pymongo noise is quietened, and per-message/per-tap INFO logs
  moved to DEBUG.

### Fixed (found while testing)

* `UserService.list_users` sliced the coroutine (`await find()[skip:]`), so the
  admin **Users** page crashed with *"'coroutine' object is not subscriptable"*.
* `PeerResolver.join` no longer tries `JoinChannelRequest` on a basic group
  (`Cannot cast InputPeerChat to any kind of InputChannel`) — it explains that
  groups are joined by an admin or an invite link instead.
* `PeerResolver.resolve` tries the public username before the numeric-id ladder,
  so public channels resolve on the first attempt.
* `PanelRouter` has no `.settings`; the `/grant` handler used one and crashed.
* A channel handed to a different owner re-evaluates auto views against the new
  owner's plan instead of keeping the previous owner's entitlement.

### Tests

`tests/test_free_reactions_spec.py` (15), `tests/test_isolation_and_plans.py`
(25), `tests/test_panel_polish.py` (38) are new; the suite is **793 green**
against a real MongoDB, and `scripts/live_check.py demo` now also proves the
free tier, the public/private join rules, isolation and the premium flow.

## [2.1.0] - 2026-10-16

Fixes for three bugs visible in a real Railway deployment log.

### Fixed
- **Settings were never read from the database.** The settings service called
  ``get_store_sync()`` without importing it, so every read raised `NameError`,
  was swallowed, and fell back to the built-in defaults — admin-panel price and
  limit edits therefore had no effect. Guarded by
  `tests/test_service_wiring.py` (a missing-import check across `src/`).
- **A channel added by a user whose id Telegram does not report stayed
  unowned** (`Channel registered: … owner=None`), so it never showed up in the
  user panel. The actor is now detected from `added_by`, the action message or
  the original update; when it is genuinely unknown the channel is still
  registered, the admins are notified, and the panel offers a one-tap
  **“Assign … to me”** button (`ChannelService.set_owner`).
- **Auto reactions reported a silent success with an empty account pool**
  (`0 ok, 0 failed`). The channel owner (or the admins) is now told that no
  accounts are signed in, at most once per 30 minutes per channel.
- The invite link for a private channel is re-exported on a later post if the
  first attempt failed (permission granted after the fact, temporary error), and
  failures are logged as warnings instead of debug. `_export_invite` accepts
  both the raw and the `-100…` channel id.
- The start-up log now explains `owner:False` in the command-menu line: a
  per-user command scope needs the owner's input peer, which only exists after
  the owner writes to the bot (all commands keep working regardless).

### Added
- `tests/test_production_log_regressions.py` — 26 tests reproducing the log
  line by line, including the full journey: unknown-actor channel add → panel
  visibility → claim → accounts signed in → new post → reactions.
- `tests/test_service_wiring.py` — every service must build the way production
  builds it (no injected store) and reach the global store.
- `scripts/live_check.py` gained a "channel added by an unknown actor" step.

## [2.0.0] - 2026-10-15

The release the panel was rebuilt around: full user + admin panels, MongoDB
persistence, a free reaction engine, paid services and a very large test suite.

### Added

**Panels**
- **User panel** for every non-admin account, matching the reference design:
  Order Views, Auto Views, Order Reactions, Auto Reactions, Account Info,
  Members, Add Money, Support, Order Management, Add To Channel / Add To Group,
  Premium, Poll Votes and Comments.
- **Add To Channel / Add To Group** deep links (`?startchannel` / `?startgroup`
  with `post_messages+edit_messages+delete_messages+invite_users`), plus a
  configurable welcome/onboarding message.
- **Admin panel**: Dashboard, Pricing (add/edit/delete plans for every service),
  Free Limits, Emojis, Users (wallet, premium, role, ban), Orders (detail,
  cancel + refund, retry), Top-ups (approve / reject / fix amount), Channels,
  Accounts (restore, sync), Broadcast, Settings and a Legacy menu.
- Every price, quantity, limit, toggle and emoji set is editable from the admin
  panel at runtime — no code or database edits.
- `Send PV` is **owner-only**: hidden from other admins and refused server-side.

**Reactions & view automation**
- Free reaction tier for all users: random positive emojis, human-like delays,
  `reactions.free_account_limit` accounts per order (10 by default, or the
  minimum available), daily quota and cooldown, all admin-configurable.
- Adding the bot to a channel (with the right permissions) registers the channel
  and enables the free auto plan immediately; every new post then gets
  reactions/views from a random subset of accounts.
- Private channels are supported end to end: a fresh invite link is exported and
  stored, accounts join through it, and the entity access-hash is cached in
  MongoDB — this fixes the production crash
  `Could not find the input entity for PeerChannel(channel_id=3852827894)`.

**Paid services**
- Plans for reactions, views, members, polls, comments and premium; custom
  quantities are priced from the cheapest per-unit rate.
- Polls and comments are premium-only by default (switchable by the owner), and
  comments are hard-capped by `comments.max_accounts` — plans that exceed the cap
  are pro-rated instead of overcharging.
- Wallet top-ups: UPI details, UTR submission, duplicate-UTR protection, admin
  approval that credits the wallet atomically.
- Order lifecycle: pending → processing → completed / partial / failed /
  cancelled, with automatic refunds and user notifications.

**Persistence & operations**
- New storage layer (`src/db/store.py`) with a **MongoDB store** (AsyncMongoClient)
  and a JSON/Memory fallback; `auto | mongo | json | memory` via `STORAGE_BACKEND`.
- Signed-in accounts (session strings) are stored in the database and restored
  automatically after a crash, restart or rebuilt container; revoked sessions are
  detected and marked instead of breaking startup.
- Commands are auto-registered with Telegram on every start (default scope +
  admin scope), so the ☰ menu is always correct.
- Notifications for the owner/admins and for users (order states, top-ups,
  premium grants, exhausted auto plans, new channels).
- Peer resolution rewrite (`src/peers.py`): link parsing, access-hash cache,
  dialog refresh, public/private join handling and honest error reporting.
- Deployment: `Dockerfile`, `docker-compose.yml` (bot + MongoDB + volumes),
  refreshed `env.example`, CI now runs against a real MongoDB service.
- `scripts/live_check.py` — a credential-free live check of the whole panel,
  with a crash-safety mode (`crash-write` / `crash-verify`).

### Deployment
- `Dockerfile` rewritten to work on a plain VPS **and** on Railway: removed the
  persistent-storage and container-check instructions Railway rejects, added a
  non-root-safe writable `/data`, and kept a single `python main.py` entrypoint.
- Added `railway.json` (Dockerfile builder + `/health` healthcheck) and
  `nixpacks.toml` as a fallback for platforms that build with Nixpacks.
- `docker-compose.yml` (VPS) now waits for MongoDB, keeps named volumes and
  publishes the status endpoint on port 8080.
- MongoDB connection can now come from `MONGODB_URI`, `MONGO_URI`,
  `MONGODB_URL`, `MONGO_URL`, or Railway's `MONGOHOST`/`MONGOUSER`/
  `MONGOPASSWORD` parts (auto-assembled with `authSource=admin`).
- New optional health endpoint (`/`, `/health`, `/healthz`, `/ping`) that starts
  when `PORT` or `HEALTH_PORT` is set — Railway/Render/Fly healthchecks pass,
  values on a VPS are unaffected.
- Data directory is auto-detected: explicit `DATA_DIR` → writable `/data`
  (Railway Volume) → `./data`.
- New guide: [docs/DEPLOY.md](docs/DEPLOY.md) with step-by-step VPS, Railway and
  Render/Fly/Koyeb instructions plus a troubleshooting table.

### Changed
- `/start` routes users to the user panel and the owner/admins to the admin
  panel; config values are read at call time so panel edits take effect
  immediately.
- Callback data is namespaced (`u:` / `a:`), duplicate taps are ignored, and
  input prompts render with a Cancel button.
- Legacy menus (Account Management, Individual, Bulk, Monitor Mode, Report
  Status) now live under the admin panel's Legacy section.

### Fixed
- `Could not find the input entity for PeerChannel(...)` when reacting in private
  channels (cold entity cache, missing access hash, accounts not yet joined).
- Panel prompts were stored but never sent, so link/amount questions never
  appeared.
- Wallet race conditions: debits are atomic, parallel orders cannot overdraw,
  and failed orders refund exactly once.
- Stale configuration in long-running processes (admin ids, support username,
  UPI details, currency) after an edit.
- Crash/restart losing signed-in accounts, channels, settings and open orders.

### Testing
- 625 tests (up from the original suite), including full panel journeys, the
  end-to-end stack, limit/quota edge cases, peer-resolution regressions,
  MongoDB-backed persistence tests and a boot test of the real startup path.

## [1.0.0] - 2025-01-27

### Initial Release
- Account management system
- Message monitoring with keywords
- Bulk and individual operations
- Statistics and reporting
- Complete test suite
