# 🚀 Upgrade Summary — Telegram Panel v3.5

**Status:** ✅ 899 tests green (real MongoDB) · ✅ live check ke saare **137 steps** green · ✅ web admin smoke (36 checks) green · ✅ dashboard DOM test (jsdom) green · ✅ Railway boot sim green (v3.5.0)
**Zip:** `Telegram-Panel-v3.5.0-production-ready.zip`

## 0f. v3.5.1 — hotfix: buttons code ki tarah dikh rahe the

| # | Problem | Fix | Status |
|---|---------|-----|:------:|
| 1 | Users / Channels / Accounts / Operators / Force Sub / Requests / Orders me **`<button …>` aur `<span class="tag">` text ki tarah** dikh rahe the (desktop + mobile) | Table ka `fmtCell()` har string ko escape karta tha — data ke liye sahi, par jo cells renderer khud banata hai (buttons, tags) wo bhi escape ho jate the. Ab wo `html(…)` se guzarte hain aur jaisa bana hai waisa hi render hota hai; andar ka har value ab bhi `esc()` se safe hai | ✅ |
| 2 | Mobile par ese lamba text hone se card screen ke bahar chala jata tha | Cells me `overflow-wrap:anywhere` — id/invite link bhi wrap hote hain, kuch clip nahi hota | ✅ |
| 3 | Phone par tab strip header ke neeche aadhi chhup jati thi | Nav ab **measured** header height (`--hdr`) ke neeche sticky hai, guess kiye 52px ke neeche nahi | ✅ |
| 4 | Requests ka status plain text (colour tag nahi) | Duplicate `status_tag` column hata kar asli tag renderer use hota hai | ✅ |
| 5 | Yahi bug dobara aa sakta tha | `dashboard_dom_check.py` ab **har tab** check karta hai ki koi markup text me na aaye + 2 naye static tests | ✅ |

---

## 0e. v3.5 — Round 8: dashboard soft refresh + mobile, dashboard scroll, owner-only force subscribe

| # | Aap ka point | Ab kya hai | Status |
|---|--------------|------------|:------:|
| 1 | **Auto refresh se changes erase ho jate the** | Tick ab sirf **badle hue hisse (regions)** patch karta hai — poora page dobara render nahi hota. Jo data same hai usko chhua bhi nahi jata (fingerprint), isliye scroll, focus aur type kiya hua text jaisa tha waisa rehta hai | ✅ |
| 2 | **Edit karte waqt refresh rok** | Kisi field me typing/focus ya unsaved change ho to auto refresh **pause** — header me **“✎ editing — live paused”**. Plans/Pricing/Settings jaise pure-form tabs par auto refresh bilkul band | ✅ |
| 3 | **Mobile/Android par theek dikhe** | ≤760px: sticky compact header, swipeable tab strip, 44px touch buttons, 16px inputs (iOS zoom band), tables **stacked label/value cards** ban jate hain — kuch clip nahi hota, horizontal scroll ki zarurat nahi | ✅ |
| 4 | **Recent orders / latest users se page lamba ho jata tha** | Har lambi table **fixed-height scroll box** me (sticky header ke saath) — data badhe to page nahi, box scroll hota hai | ✅ |
| 5 | **Force subscribe channel add/remove/view — owner only** | Naya **📢 Force Subscribe** screen bot ke admin panel me (sirf owner): channel add (@username / t.me link / private invite / -100 id, optional title `@ch \| Updates`), list view, delete, ON/OFF toggle, 60s cache. User ko join screen + **✅ I joined — verify**; owner/admin kabhi block nahi, aur check fail ho to **fail-open** (kabhi sabko lock out nahi karega) | ✅ |
| 6 | **Web panel se bhi wahi control** | Dashboard me **Force Sub** tab (owner-only) — wahi list, add/remove/toggle. Saath hi hardening: operators add/remove aur gate sirf owner kar sakta hai (admin khud ko owner nahi bana sakta) | ✅ |
| 7 | **Live tested + ready to deploy** | 897 tests + 137-step live check + 36-check web smoke + **jsdom DOM test** (tick ke baad typed value safe, region in-place patch) + boot sim + crash sim, sab green | ✅ |

---

## 0d. v3.4 — Round 7 (naya log): polls hata do, Buy Premium, channel slots + lock, web admin panel

| # | Aap ka point | Ab kya hai | Status |
|---|--------------|------------|:------:|
| 1 | **Poll feature hata do — har jagah se** | Polls poore bot se hat gaye: user panel, admin panel, plan segment, engagement service, order kinds, help text, config defaults, live check. Purana data safe hai, koi cheez usse padhti nahi | ✅ |
| 2 | **Auto Reactions button do baar** | Home panel me ek hi baar dikhta hai; "Buy Premium"/"My Plan" bhi sirf ek row me (duplicate buttons khatam) | ✅ |
| 3 | **Add Money → ⭐ Buy Premium** | Screen ka naam **👑 BUY PREMIUM**: order view jaisa saara plan (days • accounts • +channels • kya unlock hoga), phir **📨 Request activation** aur **💬 Contact owner**. Request MongoDB me save hoti hai, owner ko ping jata hai (plan, price, days), approve hone par wahi plan exactly grant hota hai + user ko confirmation | ✅ |
| 4 | **Auto-reaction channels: 2 free, aage plan** | `channels.free_limit` (default **2**), admin panel se badla ja sakta hai. Usse zyada channels ke liye alag **channel plan segment** (₹99/+1, ₹249/+3, ₹699/+10) aur baaki plans me bhi matching slots. User "kitne channels chahiye" batata hai → owner ko exact number ki request jati hai. 3rd channel par saaf message + *Contact owner* | ✅ |
| 5 | **Channel add hone par LOCK** | User ka add kiya channel **locked**: disable kar sakta hai, par remove/re-add sirf admin. Isse free allowance ka misuse band | ✅ |
| 6 | **Web admin panel (Brutalism, live sync)** | `/admin` usi port par jahan `/health` — Dashboard, Requests (approve/reject), Users (wallet/plan/ban/DM), Plans CRUD (+channel slots), Pricing, Channels (lock/owner/auto/delete), Orders, Account pool, **Operators (multiple owners)**, Settings + broadcast. Sab kuch bot ke **usi service objects** se chalta hai = turant sync (settings write karke live check me verify kiya) | ✅ |
| 7 | **Multiple owners** | Dashboard ▸ Operators se jitne chahe owner/admin add karo — `operators` collection se role har jagah (bot panel, permissions, god mode) turant lagu | ✅ |
| 8 | **User-friendly formatting + speed** | Asli anchor links (`tg_link` / `html_link`) — markdown brackets chat me leak nahi hote; ek hi header style; hot paths sasta (channel allowance ek hi section read karta hai); 18 naye round-7 tests | ✅ |
| 9 | **Live tested + ready to deploy** | 879 tests + 129-step live check + 25-check web HTTP smoke (login se lekar settings write tak) + boot sim + crash sim, sab green | ✅ |

---

## 0c. v3.3 — naye log (`logs.800000002.json`) ke saare issue fix

| # | Aap ka point | Kya hua tha | Ab kya hai | Status |
|---|--------------|-------------|------------|:------:|
| 1 | **OTP / 2-step password galat → pura process restart** | Ek hi galat code par client close, state wipe, dobara `/start` | Wahi client + phone rakhta hai, **3 attempts** (code aur password dono), har message me bache attempts + expiry/restart par naya code bina attempt kharch kiye | ✅ |
| 2 | **6 accounts signed in, reaction sirf 5 se** | `800000003` par **sqlite lock** (`database is locked`) → update loop crash → client chup-chaap pool se gaya | Do client ek hi `.session` file par = asli wajah. Ab same phone dobara sign-in par **wahi client reuse**, purana client pehle disconnect; pool supervisor har 60s me toota client **khud reconnect** karta hai | ✅ |
| 3 | **God mode admin/owner** | — | Admin/owner ke liye har feature unlocked, koi payment/limit nahi (panel me bhi 👑 banner, order hamesha ₹0, `paid_with="owner"`) | ✅ |
| 4 | **Error par auto-logout nahi** | connection error / sqlite lock / invalid peer par account "inactive" | Sirf **ban/block/revoked** par logout. Baaki sab (disconnect, timeout, sqlite lock, invalid peer) par account pool me rehta hai aur khud recover karta hai | ✅ |
| 5 | **Auto-join message chat me nahi** | join hone par channel me message | Ab sirf **log** me (`Auto-join finished …`), chat bilkul clean | ✅ |
| 6 | **Plan activate hone par notification** | user ko pata hi nahi chalta tha | Plan grant hote hi user ko message: plan ka naam, **kitne din** ke liye, aur kaunse services cover hue | ✅ |
| 7 | **Poll detect nahi hota** | sirf `MessageMediaPoll` check tha → `Target message is not a poll` → paid order refund | Ab **native poll, quiz, multiple choice aur inline-button poll** sab support: `resolve_poll_target()` vote ya button-press karta hai, order **complete** hota hai (refund nahi) | ✅ |
| 8 | **Auto comments** | feature hi nahi tha | Naya **Auto Comments**: har naye post par plan ke accounts se **random positive comments** (pool admin-editable, per-post account limit panel se) | ✅ |
| 9 | **Auto views kaam nahi kar raha** | owner ka plan na hone par flag hamesha ke liye **OFF** ho jata tha (jabki paying user ka plan tha) | Flag ON rehta hai; entitlement ab **owner + added_by + sponsors (paid buyer)** sabse check hota hai; plan grant hone par automation khud back on; reason ek baar log me | ✅ |
| 10 | **Plan ke baad bhi alag-alag paise maangta hai** | plan-covered order me `paid_with="free"` + per-feature charge | Ek granted plan = uske saare services free: order doc me `price 0`, `paid_with="plan"`, wallet bilkul touch nahi. Admin ke liye per-order charging alag toggle se (default off) | ✅ |
| 11 | **Live test + ready to deploy** | — | 878 tests + 112-step live check + boot sim + crash-recovery sim, sab green; zip me pura code | ✅ |
| 12 | **(v3.3.1 cleanup)** account status ka ek hi source | Purane `config['inactive_accounts']` par ab kahin likha nahi jata — sirf registry (MongoDB) mein, aur wahi ban/block wale accounts dikhte hain; session name ke saare spelling resolve hote hain | ✅ |

### Log me jo aur ganda noise tha

| Signal | Fix |
|--------|-----|
| `Server closed the connection: 0 bytes read` / `Server resent the older message` (Telethon ka normal reconnect) | log filter — sirf asli errors dikhte hain |
| `Could not send notification to <user>: An invalid Peer was used` | unreachable user ek baar yaad rakha jata hai, har message par dobara try/log nahi |
| `join failed … Could not resolve chat` | invite link refresh + account-side cache, aur pehle jaisa hard failure nahi |
| `Added-by was hidden; attributing the channel to its creator X` | ye sirf ek **guess** hai — asli adder mile to channel khud re-attribute ho jata hai |
| `Plan granted to 800000004: … 365d` par channel `800000005` ke naam | channel owner/added_by/sponsor sab entitlement candidates hain + `set_owner` handover par entitlement saath chalti hai |

---

# 🚀 Upgrade Summary — Telegram Panel v3.2

**Status:** ✅ 839 tests green (real MongoDB) · ✅ live check ke saare steps green · ✅ Railway boot sim green
**Zip:** `Telegram-Panel-v3.2-production-ready.zip`

---

## 0b. v3.2 — dusre log ke saare issue fix

| # | Kaam | Status |
|---|------|:------:|
| 1 | Notification me `</b>` / `<code>` dikh raha tha → ab har message **HTML parse mode** ke saath jata hai (Panel is online + auto-reaction alert dono) | ✅ |
| 2 | `❌ Invalid reaction provided` — Telegram sirf **apne 73 reactions** accept karta hai; `✅` allowed nahi tha. Ab list sanitize hoti hai (boot par database bhi repair), aur galti se koi refuse ho jaye to **doosra emoji try** hota hai | ✅ |
| 3 | Ek post par **2 baar reaction** (do handlers same update pakad rahe the) → ab per-channel `last_post_id` se duplicate/redelivered post skip | ✅ |
| 4 | Naya account sign-in hote hi **purane + naye sab channels me join** (free cap 10 tak), aur boot ke baad bhi ek warm-up | ✅ |
| 5 | Channel se bot remove → hamare **saare accounts us channel se leave**, record delete, owner ko message | ✅ |
| 6 | Alert ab **asli reason** batata hai (emoji refuse / flood / account revoked / approval / invite), har baar "permission" ka ilzaam nahi | ✅ |
| 7 | Log noise: `ADMIN_IDS` jaise optional vars ab INFO (Railway error jaisa nahi dikhega), `clients.json` notice bhi INFO | ✅ |
| 8 | "Insufficient balance" par ab price + balance + **💳 Add Money** button | ✅ |
| 9 | Free pool 8–10 accounts hi react karega, par **membership poori 10 tak** — pool kabhi adhoora nahi rehta | ✅ |
| 10 | 20 naye tests + live check steps 4c–4f + boot sim apni database reset karta hai (repeatable) | ✅ |

---

# 🚀 (v3.1) Pehle kya fix hua tha


**Status:** ✅ 818 tests green (real MongoDB) · ✅ live check ke saare steps green · ✅ Railway boot sim green
**Zip:** `Telegram-Panel-v3.1-production-ready.zip`

---

## 0. v3.1 — "accounts channel mein join kyun nahi ho rahe the"

| # | Kaam | Status |
|---|------|:------:|
| 1 | **Asli bug mila**: Telegram `CheckChatInvite` ka *not-a-member* jawab (`ChatInvite`) mein `.chat` field hi nahi hota; purana code usi ko "kuch karne ki zarurat nahi" samajh kar `ImportChatInviteRequest` **bhejta hi nahi tha** → `join()` None → har reaction `Could not resolve chat …` | ✅ |
| 2 | Ab plain `ChatInvite` par **hamesha import** hota hai, uske baad entity dobara resolve | ✅ |
| 3 | **Invite link khud theek hota hai**: link na ho ya mar chuka ho to bot se naya banwa kar channel par save karta hai (orders + auto reactions dono) | ✅ |
| 4 | Batch fail ho to **naye link ke saath ek retry** (extra credit kharch nahi hota) | ✅ |
| 5 | **Permissions ka masla kabhi tha hi nahi** — aapke Admin Rights ke saare toggle ON theek hain | ✅ |
| 6 | Telegram adder na batae to channel **creator (ya single human admin)** ke naam par assign — `owner=None` gaya | ✅ |
| 7 | Restore kiye accounts **apna naam** rakhte hain (log mein "Unknown" gaya) | ✅ |
| 8 | Free order screen pool khaali hone par sach bolti hai ("pool is empty" + sign-in ka tareeka) | ✅ |
| 9 | Non-link message par dobara prompt ("Unrecognised Telegram link: Test Message" gaya) | ✅ |
| 10 | Free **order** ka poora rasta end-to-end test (9 naye tests) + live check step 4b | ✅ |

---

# 🚀 (v3.0) Pehle kya bana tha — poora record

**Status:** ✅ 793 tests green (real MongoDB) · ✅ live check green · ✅ crash test green
**Zip:** `Telegram-Panel-v3.0-production-ready.zip`

---

## 1. Is round mein kya kya hua (short mein)

| # | Kaam | Status |
|---|------|:------:|
| 1 | **Free auto reactions** — har user ko free, bot add hote hi ON | ✅ |
| 2 | Har naye post (text/photo/video/file) par **8–10 random accounts** se reaction | ✅ |
| 3 | **Random positive emoji** + **1–3 second** ka random gap (pehle 5–25s tha) | ✅ |
| 4 | **Public channel** → join ki zarurat nahi, seedha react | ✅ |
| 5 | **Private channel** → bot ka invite link banake accounts auto-join | ✅ |
| 6 | Invite link pehli baar fail ho to **agle post par retry** (production wala bug) | ✅ |
| 7 | Bot add hote hi accounts **channel ke andar warm** — pehle hi post par reaction | ✅ |
| 8 | Reaction se **view count nahi badhta** — views alag paid service hai | ✅ |
| 9 | Sirf reactions free; **views / comments / polls / members premium** | ✅ |
| 10 | Plan **type** ke hisaab se unlock (reactions / views / comments / polls / members / all) | ✅ |
| 11 | Plan ke **din** aur **account limit** admin set kare | ✅ |
| 12 | **♾ Unlimited** grant — bina limit, bina expiry (sirf owner) | ✅ |
| 13 | `/grant`, `/unlimited`, `/revoke` commands (auto-registered, owner-only) | ✅ |
| 14 | Free user ko locked screen + **"Request activation"** button (owner ko seedha alert) | ✅ |
| 15 | **Multi-user isolation** — koi dusre ka channel na dekh sake na chala sake | ✅ |
| 16 | Owner sab dekhta hai; admin apne + unassigned channels (dusre ke nahi) | ✅ |
| 17 | Panels ek hi message **edit** karte hain — chat clean, koi spam nahi | ✅ |
| 18 | Message formatting fix (`</b>accounts` wala glue) + poora screen test | ✅ |
| 19 | Logs **stdout** par (Railway ab INFO ko error nahi dikhayega) | ✅ |
| 20 | Admin **Users page crash** fix (`'coroutine' object is not subscriptable`) | ✅ |
| 21 | Basic group join crash fix (`Cannot cast InputPeerChat…`) | ✅ |
| 22 | Public channel resolution pehle attempt me (username-first) | ✅ |

---

## 2. Free tier — exactly kaise chalta hai

1. User bot ko apne channel me **Add To Channel** button se add karta hai (all permissions).
2. Bot channel ko us user ke naam par register karta hai, `auto_reactions` ON,
   aur **private channel** ho to pool accounts ko invite link se andar bhej deta hai.
3. Har naye message par:
   * account count = **random 8–10** (pool chhota ho to jitne hain, plan cap se zyada nahi),
   * har account ek **random positive emoji** (👍❤️🔥🎉🥰👏😁🤩💯🙏⚡✅🏆👌),
   * accounts ke beech **random 1–3 second** gap,
   * public channel par **koi join nahi**, private par **invite link se join**.
4. Kuch bhi apply na ho to aapko **reason** ke saath message aata hai (pool khali /
   Invite Users permission nahi) — chup-chaap "0 ok" nahi.

## 3. Premium — sirf reactions free hain

| Service | Free? | Kaise milega |
|---|---|---|
| Auto Reactions (8–10) | ✅ Free | default |
| Auto Reactions (zyada accounts / custom emoji) | 👑 Premium | owner plan deta hai |
| Auto Views + Order Views | 👑 Premium | owner plan deta hai |
| Comments | 👑 Premium | owner plan deta hai (account limit bhi owner set kare) |
| Poll Votes | 👑 Premium | owner plan deta hai |
| Members | 👑 Premium | owner plan deta hai |
| ♾ Unlimited (kuch bhi limit nahi) | 👑 Owner-only | `/unlimited <user_id>` |

---

## 4. v2.0 round — kya bana / fix hua (reference)

| # | Kaam | Status |
|---|------|:------:|
| 1 | Production crash fix — `Could not find the input entity for PeerChannel(channel_id=800000006)` | ✅ |
| 2 | Signed-in accounts **MongoDB** mein save (crash/restart par kuch nahi jaata) | ✅ |
| 3 | **User panel** (aapke image-1 jaisa) + **Admin panel** | ✅ |
| 4 | **Add To Channel / Add To Group** buttons (image-2 + permissions flow) | ✅ |
| 5 | Saare commands **auto-register** (☰ menu khud update) | ✅ |
| 6 | Sab kuch admin panel se **configurable** (price, quantity, limits, toggles, emoji) | ✅ |
| 7 | **Free reactions** — 10 accounts default, ya jo available hain | ✅ |
| 8 | Bot channel mein add hote hi **auto reactions** (random positive emoji + random delay) | ✅ |
| 9 | **Private channels** — invite link se accounts auto-join | ✅ |
| 10 | **Paid plans**: reactions, views, members, poll (premium), comment (premium) | ✅ |
| 11 | **Order Views** free ya paid (admin decide kare) + custom quantity pricing | ✅ |
| 12 | **Auto Views** on/off + custom premium plans | ✅ |
| 13 | **Send PV — sirf owner** ko dikhta hai (server-side bhi block) | ✅ |
| 14 | Docker + docker-compose (bot + MongoDB) — deploy-ready | ✅ |

---

## 2. User panel (aapke mockup ke hisaab se)

```
📈 Order Views        🔄 Auto Views
❤️ Order Reactions    🔄 Auto Reactions
👤 Account Info       👥 Members
💳 Add Money          🆘 Support
🧾 Order Management
➕ Add To Channel     ➕ Add To Group
👑 Premium            📊 Polls • 💬 Comments (premium)
```

* **Add To Channel / Add To Group** → deep link `?startchannel` / `?startgroup`
  with `post_messages+edit_messages+delete_messages+invite_users` rights — jaisa
  aapke image-2 mein tha, plus "How to use" onboarding text (admin panel se
  editable: **Settings → 🔥 Welcome message**).
* **Free reactions**: koi bhi user 10 accounts tak (ya pool mein jitne hain) free
  reactions le sakta hai — random positive emoji, human-like random delay,
  daily quota + cooldown (sab admin panel se set).
* **Auto reactions**: channel add karte hi chalu — har naye post par automatic
  random reactions. **Private channel** ho to invite link se accounts join
  karke react karte hain (yahi production crash ka fix hai).

---

## 3. Admin panel (owner + admins)

```
💰 Pricing   🎁 Free Limits   😀 Emojis   👥 Users   🧾 Orders
💳 Top-ups   📡 Channels      👤 Accounts 📣 Broadcast  ⚙️ Settings
🧩 Legacy menu (Monitor / Individual / Bulk / Reports)   ✉️ Send PV (owner only)
```

* **Pricing** — har service ke liye plan add / edit / delete (title, quantity, price).
* **Free Limits** — free accounts/order, orders per day, cooldown, auto accounts
  per post, auto views per post, free/premium account caps, comment account limit,
  poll/comment premium-only toggles, auto-reaction master switch.
* **Emojis** — positive emoji set add/delete/reset (default 14), random ya sequential.
* **Users** — wallet credit/debit, premium grant/revoke, admin promote, ban/unban.
* **Orders** — detail, cancel + auto refund, retry; **Top-ups** — approve / reject / amount edit.
* **Send PV** — button sirf owner ko dikhta hai, callback bhi owner ke alawa reject.

---

## 4. MongoDB — kuch bhi nahi khoyega

`MONGODB_URI` set karte hi ye sab database mein jaata hai:
users, wallets, transactions, orders, payments, channels, settings,
**signed-in accounts (session strings)**, aur entity access-hash cache.

Crash proof bhi test kiya gaya:

```
$ python scripts/live_check.py crash-write  --mongo mongodb://127.0.0.1:27017
  ℹ wrote user 777001, channel -100800000001 and 6 accounts
  ℹ killing the process hard (exit code 9) — data must survive

$ python scripts/live_check.py crash-verify --mongo mongodb://127.0.0.1:27017
  ✔ wallet balance survived
  ✔ channel registration survived
  ✔ 6 account session strings survived
  ✔ everything survived the hard crash
```

---

## 5. Test evidence (sab actually chalaya gaya)

| Check | Command | Result |
|-------|---------|--------|
| Full suite | `python -m pytest tests/ -q` | **652 passed, 6 skipped** |
| Full suite + real MongoDB | `TEST_MONGODB_URI=… pytest tests/ -q` | **658 passed** |
| MongoDB live tests | `pytest tests/test_mongodb_live.py -q` | **6 passed** (real mongod 7.0.14) |
| Live panel check (no credentials needed) | `python scripts/live_check.py demo --mongo …` | **all checks passed** |
| Crash round-trip | `crash-write` → `crash-verify` | **everything survived** |
| Bot boot (real startup path) | `tests/test_bot_boot.py` | **4 passed** |
| Deploy config (Railway + VPS) | `tests/test_deploy_config.py` | **27 passed** |
| Real process + `MONGO_URL` + `$PORT` | `curl /health` | **HTTP 200 `{"status":"ok","storage":"MongoStore"}`** |
| Cold start with wrong credentials | `python main.py` | clear CRITICAL hint, no retry loop |

Live check mein jo actually verify hota hai: user panel ke saare buttons, owner ka
admin panel, owner-only Send PV, free auto reactions + auto views, free reaction
order (crash-log wala case), paid views order + wallet debit, top-up → admin
approve, admin price change ka turant asar, account session persistence aur
crash ke baad restore.

---

## 6. Deploy — VPS aur Railway dono par (v2.1 mein add hua)

**Railway fix:** pehla Dockerfile `VOLUME` use kar raha tha, jo Railway reject
karta hai (*"docker VOLUME at Line 24 is not supported"*). Ab:

- Dockerfile mein **na VOLUME, na HEALTHCHECK** instruction — isliye Railway ise
  accept karta hai (parsed + tested).
- `railway.json` added → Dockerfile builder + `/health` healthcheck.
- Railway ka MongoDB plugin `MONGO_URL` / `MONGOHOST`+`MONGOUSER`+`MONGOPASSWORD`
  deta hai — bot ye sab **khud padh leta hai** (manual wiring nahi chahiye).
- Railway `$PORT` inject karta hai → bot usi port par **`/health` status page**
  serve karta hai (Railway isi se healthy mark karta hai). VPS par PORT nahi
  hota, to kuch bhi extra start nahi hota.
- `/data` writable mile to JSON fallback wahin rakhta hai (Railway Volume),
  warna `./data`.
- `nixpacks.toml` bhi added (agar koi Nixpacks builder use kare).

**Railway steps (short):**

1. New Project → Deploy from GitHub repo (ya `railway up`).
2. `+ New → Database → MongoDB` (plugin) — variables automatically share ho jaate hain.
3. Variables: `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMIN_ID` (+ `OWNER_ID`, `SUPPORT_USERNAME`, `CURRENCY` optional).
4. Deploy → logs mein `Bot is now running...` → healthcheck `/health` green ✅
5. Chahein to Railway **Volume `/data`** mount kar lein (optional, MongoDB mode mein zaroorat nahi).

Poori guide: **`docs/DEPLOY.md`** (VPS, Railway, Render/Fly/Koyeb + troubleshooting table).

---

## 7. Deploy kaise karein

### Option A — Docker (recommended)

```bash
cd Telegram-Panel
cp env.example .env      # API_ID, API_HASH, BOT_TOKEN, ADMIN_ID bhar dijiye
docker compose up -d --build
docker compose logs -f bot
```

`MONGODB_URI` automatically bundled Mongo se connect ho jaata hai; volumes
database + logs ko rebuild ke baad bhi bachaye rakhte hain.

### Option B — direct

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env.example .env      # edit karein
python main.py
```

Zaroori env: `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMIN_ID`, aur (highly
recommended) `MONGODB_URI=mongodb://127.0.0.1:27017`.
Optional: `OWNER_ID`, `ADMIN_IDS`, `SUPPORT_USERNAME`, `CURRENCY`,
`PAYMENT_UPI_ID`, `PAYMENT_UPI_NAME`, `STORAGE_BACKEND`.

Deploy ke baad ek baar:

```bash
python scripts/live_check.py demo --mongo "mongodb://127.0.0.1:27017"   # end-to-end green hona chahiye
```

---

## 8. Naye/important files

| File | Kya hai |
|------|---------|
| `src/health.py` | `/health` status endpoint (Railway/Render healthcheck ke liye) |
| `railway.json`, `nixpacks.toml`, `docs/DEPLOY.md` | Railway deploy config + guide |
| `src/panels/` | user panel, admin panel, router, shared UI helpers |
| `src/services/` | settings, users/wallets, accounts, orders, payments, engagement, auto-engagement, notifications, executor |
| `src/db/store.py` | MongoDB store + JSON/memory fallback |
| `src/peers.py` | link parsing + entity/access-hash resolution (crash fix) |
| `src/commands.py` | command menu auto-registration |
| `scripts/live_check.py` | credential-free live check + crash test |
| `docs/PANELS.md` | har button/pricing/limit ki detail |
| `Dockerfile`, `docker-compose.yml`, `env.example` | deploy setup |
| `tests/fakes.py` + 8 naye test files | fake Telegram transport + full journeys |

---

## 9. Notes / next steps (honest disclosure)

1. Live verification **fake Telegram transport** par hui hai (aapke real
   `api_id`/`bot_token` mere paas nahi the) — panels, orders, reactions, Mongo
   persistence, crash recovery sab real code paths se chale. Aapke token se ek
   baar `python main.py` chala kar `/start` dabaa lena — uske baad sab live hai.
2. Docker image sandbox mein **build nahi hui** (yahan Docker daemon nahi hai) —
   Dockerfile ko parse-test kiya gaya hai (dockerfile-parse) aur compose file ko
   YAML-validated; aapke VPS/Railway par pehla `docker compose up --build` /
   `railway up` isko build karega.
3. Bot ke saath **real account sessions** add karte waqt Telegram ka flood-limit
   dhyan mein rakhna (rate limiting + delays already built-in hain).
4. Aapko kuch aur chahiye — jaise payment gateway (Razorpay/Crypto), referral
   system, ya panel ki Hindi/English language toggle — bata dijiye, next round
   mein add kar dunga.
