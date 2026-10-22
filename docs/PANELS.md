# Panels: user panel & admin panel

The bot has two faces:

| Who | Command | Sees |
|-----|---------|------|
| Any user | `/start` | **User panel** — buy/order services, manage their channel automation |
| Owner + admins | `/start` | **Admin panel** — pricing, limits, users, orders, top-ups, settings |

Everything is driven from these panels; nothing requires editing code or the
database by hand. Prices, limits and plans are stored in MongoDB
(`settings/_id="global"`) and are editable live from the admin panel.

---

## 1. User panel

```
📈 Order Views        🔄 Auto Views
❤️ Order Reactions    🔄 Auto Reactions
👤 Account Info       👥 Members
💳 Add Money          🆘 Support
🧾 Order Management
➕ Add To Channel     ➕ Add To Group
👑 Premium            📊 Polls • 💬 Comments (premium)
```

### Services

| Button | What it does | Gating |
|--------|--------------|--------|
| 📈 **Order Views** | Buys N views on one post link. Packages + a "custom quantity" option (auto-priced from the cheapest per-1k rate). | 👑 **Premium** — the owner activates it per user |
| 🔄 **Auto Views** | Every new post in the user's channel gets views automatically. | 👑 **Premium** (`views` plan) |
| ❤️ **Order Reactions** | Reactions on a post from N different accounts, with random positive emoji and human-like delays. | ✅ **Free**: up to `reactions.free_account_limit` accounts per order (10 by default, or fewer if the pool is smaller), N free orders/day, cooldown |
| 🔄 **Auto Reactions** | Every new message in the user's channel gets reactions automatically — random 8–10 accounts, random positive emoji, 1–3s apart. Public channels need no join; private ones are joined via the bot's invite link. | ✅ **Free for everyone**, unlimited posts |
| 👥 **Members** | Accounts join a channel through an invite link. | 👑 **Premium** |
| 💬 **Comments** | Comments from N accounts on every new post. | 👑 **Premium**, with an account cap the owner sets |
| 📡 **Extra channels** | Auto reactions on more channels than the free allowance (default 2). Every channel a user adds is **locked** — disable allowed, removal only by an admin. | 👑 **Channel plans** (₹99/+1, ₹249/+3, ₹699/+10) or a custom count the owner approves |

**Plans** are per service, with days and an account cap:

```text
/grant <user_id> <reactions|views|comments|members|channels|all> [days] [accounts] [channels]
/unlimited <user_id>      # ♾ no limits, no expiry (owner only)
/revoke <user_id>         # back to the free tier
```

The same flow exists in the admin panel: 👥 **Users → open a user → 👑 Give plan**.
Locked services show the user a *Request activation* button that notifies the
owner with their id, so nobody has to hunt for a chat.
| 👤 **Account Info** | Wallet, plan, limits, pool size, personal stats | — |
| 👥 **Members** | N accounts join the user's channel/group through an invite link | Paid plans |
| 💳 **Add Money** | UPI top-up: shows UPI id → user pays → submits UTR → owner approves | — |
| 🆘 **Support** | Support username from settings | — |
| 🧾 **Order Management** | Order history, live status, progress, cancel + automatic refund | — |
| ➕ **Add To Channel / Group** | Deep link that adds the bot with the required rights; linking happens instantly | — |
| 👑 **Buy Premium** | All premium plans with their days/accounts/channels + *Request activation* to the owner + *Contact owner* | — |
| 💬 **Comments** | Premium-only by default (owner can change). Comment plans bigger than the account limit are automatically pro-rated (you are never charged for comments the limit will not allow) | Premium |

### Force subscribe (owner only)

`🛠 Admin Panel → 📢 Force Subscribe` keeps a list of channels a user must join
before the bot answers:

* **Add** — send `@username`, a `t.me` link, a private invite link (the bot must
  already be a member) or a raw `-100…` id. An optional title goes after a pipe:
  `@mychannel | Updates`.
* **List / remove / toggle** — every channel is shown with its id and link; the
  gate counts as ON only when the toggle is on *and* at least one channel exists.
* Users see a join screen with one button per channel and **✅ I joined — verify**.
  Owners/admins are never blocked; banned users still get the ban message; a
  channel whose membership cannot be checked fails open.

### Welcome message

The first `/start` shows the onboarding copy (editable in the admin panel under
**⚙️ Settings → 🔥 Welcome message**): "Welcome To Multi Reaction Bot" →
*add the bot as admin* → *grant message-manage sent permission* → *tap a button
below*, followed by the service keyboard.

### Isolation

A user only ever sees and manages the channels **they** added the bot to. The
owner sees everything; admins see their own channels plus the ones Telegram
could not attribute to anybody. Every channel callback re-checks ownership, so a
hand-crafted button cannot touch somebody else's channel.

### Free tier behaviour (all values admin-editable)

* `reactions.free_account_limit` — accounts used per **free** order (default 10)
* `reactions.free_min_accounts` — if the pool is smaller than the limit, use what exists
* `reactions.free_daily_orders`, `reactions.free_cooldown_seconds` — anti-abuse
* `views.free_quantity`, `views.free_daily_orders` — free view trial
* `reactions.auto.enabled` + `accounts_per_post` — automatic reactions on new posts

### Private channels

When the bot is added, it exports an invite link (`ExportChatInviteRequest`) and
stores it with the channel. Accounts that are not members yet join through that
link before reacting/commenting — so private channels work exactly like public
ones. This is the fix for the `Could not find the input entity for
PeerChannel(...)` crash reported from production.

---

## 2. Admin panel

```
💰 Pricing            🎁 Free Limits
👥 Users              🧾 Orders
💳 Top-ups            📡 Channels
👤 Accounts           😀 Emojis
📣 Broadcast          ⚙️ Settings
🧩 Legacy menu        🔄 Refresh
✉️ Send PV (owner only)
```

### 💰 Pricing
Per service (reactions, views, members, comments, channels, premium): list of plans
with **title / quantity / price**, live editing, delete, and a 3-step
"add plan" wizard. Custom quantities ordered by users are priced from the
cheapest per-unit rate across plans.

### 🎁 Free Limits
Accounts per free order, free orders/day, cooldown, auto accounts per post,
auto views per post, free view trial, account caps for free/premium users,
comment account limit, the free-channel count, per-service premium-only toggles, and a master switch
for the whole auto-reaction engine.

### 😀 Emojis
The list of **positive reactions** used for free orders, paid orders and auto
reactions. Add emojis, remove them one by one, reset to the default 14, or switch
between random and sequential picking.

### 👥 Users
Browse users, then per user: credit/debit the wallet, grant/revoke premium
(preset plans or custom days), promote/demote to admin, ban/unban. Premium grants
and wallet credits notify the user automatically.

### 🧾 Orders
All orders with status, user, amount and quantity. Order detail shows the payload
and result; admin can cancel (+ automatic refund) or retry a failed order.

### 💳 Top-ups
Pending UPI top-ups with UTR, one-tap approve/reject and an "edit amount" path
for partial payments. Approval credits the wallet atomically.

### 📡 Channels / 👤 Accounts
Who added the bot, what automation is enabled, posts seen, reactions/views sent.
Accounts shows the pool (live / inactive / revoked) with session strings in the
database, plus buttons to restore accounts from the DB or sync live clients.

### ⚙️ Settings
UPI id/name, support username, announcement banner, minimum top-up, max active
orders per user, refund-on-failure toggle, and storage/MongoDB status.

### ✉️ Send PV — owner-only
The private-message workflow is restricted to `OWNER_ID`: the button is not even
rendered for other admins, and the callback is rejected server-side as well.

---

## 3. Order lifecycle

```
pending → processing → completed
                     ↘ partial   (some accounts failed)
                     ↘ failed → refunded (automatic)
                     ↘ cancelled → refunded
```

* Wallet is debited **after** quoting the current price from settings.
* Any failure path refunds automatically (`orders.refund_on_failure`).
* Partial deliveries are reported honestly, including "N views could not be
  delivered because the account pool is smaller than the request".
* Every state change notifies the user.

---

## 4. Notifications

| Event | Who gets told |
|-------|---------------|
| Bot starts | Owner + admins |
| Bot added to a channel | The user who added it (with a "My Channels" button) |
| Order finished / partial / failed | The order's owner |
| Top-up submitted | All admins (with Approve/Reject buttons) |
| Top-up approved/rejected | The user |
| Wallet credited by support | The user |
| Premium granted | The user |
| Auto plan exhausted | The channel owner |

---

## 5. Command menu (auto-registered)

The bot pushes its command list to Telegram on every start, so the ☰ menu is
always correct — no BotFather editing:

```
User scope:  /start /panel /menu /views /reactions /autoviews /autoreactions
             /members /orders /wallet /premium /account /addchannel /addgroup
             /support /help
Admins:      + /admin /pricing /limits /users /aorders /topups /channels
             /accounts /broadcast /settings /stats
Owner:       + /sendpv
```
