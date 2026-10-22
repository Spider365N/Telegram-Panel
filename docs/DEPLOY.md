# 🚀 Deploy Guide — VPS & Railway (both supported)

Same code, same Dockerfile, two targets. Everything persistent lives in
**MongoDB**, so an ephemeral filesystem (Railway) and a VPS with volumes are
both safe.

| | VPS (docker compose) | Railway |
|---|---|---|
| Database | bundled MongoDB 7 container | MongoDB plugin (or Atlas) |
| Data survives redeploy | ✅ named volumes | ✅ MongoDB (volume optional) |
| Health check | `/health` on port 8080 | `/health` on `$PORT` (auto) |
| Web admin panel | `/admin` on port 8080 | `/admin` on `$PORT` (auto) |
| Config file | `.env` | Railway dashboard variables |

---

## 0. Get your credentials first (both targets)

| Variable | Where |
|---|---|
| `API_ID`, `API_HASH` | https://my.telegram.org/apps |
| `BOT_TOKEN` | @BotFather → `/newbot` |
| `ADMIN_ID` (and `OWNER_ID`) | @userinfobot — your numeric Telegram id |

> `OWNER_ID` is the only account that sees the **Send PV** button. `ADMIN_ID`
> already gets the admin panel; set `ADMIN_IDS` for extra admins.

---

## 🖥️ Option A — VPS with Docker Compose (recommended)

**Requirements:** any Linux VPS with Docker + Compose, 1 GB RAM is enough.

```bash
# 1. Get the code
git clone <your-repo-url> Telegram-Panel
cd Telegram-Panel

# 2. Configure
cp env.example .env
nano .env      # API_ID, API_HASH, BOT_TOKEN, ADMIN_ID (+ SUPPORT_USERNAME, CURRENCY)

# 3. Start bot + MongoDB
docker compose up -d --build

# 4. Watch it boot
docker compose logs -f bot
#   … "Bot started successfully and is ready to receive commands!"

# 5. Health check
curl -s localhost:8080/health
#   {"status":"ok","storage":"MongoStore","accounts":{"live":0,…},"users":…}
```

What the compose file does for you:

* starts **MongoDB 7** and points the bot at it (`MONGODB_URI=mongodb://mongo:27017`)
* keeps `mongo_data`, `bot_data` (JSON fallback + session file) and `bot_logs`
  in named volumes → `docker compose down && up` loses nothing
* waits for MongoDB to be healthy before starting the bot
* publishes the status endpoint on port **8080**

**No Docker?** Plain install:

```bash
sudo apt install -y python3.11-venv   # or python3-venv
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# MongoDB (or use Atlas and skip this)
sudo apt install -y mongodb-org || docker run -d -p 27017:27017 -v mongo:/data/db mongo:7.0

cp env.example .env && nano .env      # MONGODB_URI=mongodb://127.0.0.1:27017
python main.py
```

Keep it alive with a systemd unit or `tmux`/`screen`:

```ini
# /etc/systemd/system/telegram-panel.service
[Unit]
Description=Telegram Panel
After=network.target

[Service]
WorkingDirectory=/opt/Telegram-Panel
ExecStart=/opt/Telegram-Panel/venv/bin/python main.py
Restart=always
RestartSec=10
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now telegram-panel && sudo journalctl -fu telegram-panel
```

---

## 🚄 Option B — Railway (step by step)

The repo ships `Dockerfile` + `railway.json`, both written to satisfy Railway's
builder:

* **no** `VOLUME` instruction (Railway: *"docker VOLUME at Line N is not
  supported, use Railway Volumes"*) — attach a Railway Volume instead
* **no** `HEALTHCHECK` instruction — a real `/health` route is served on `$PORT`
* MongoDB is used for storage, so an ephemeral container filesystem is fine

### 1. Create the project

**Dashboard:** `New Project` → `Deploy from GitHub repo` → pick this repo.

**CLI:**

```bash
npm i -g @railway/cli
railway login
railway init          # create a new project
railway up            # builds the Dockerfile and deploys
```

### 2. Add MongoDB

`+ New` → `Database` → **Add MongoDB** (same project).

Railway then shares variables with your service:

```
MONGO_URL=mongodb://mongo:***@mongodb.railway.internal:27017
MONGOHOST, MONGOPORT, MONGOUSER, MONGOPASSWORD, MONGO_DB
```

The bot reads **any** of these — no copying of connection strings needed:

* `MONGODB_URI` → `MONGO_URI` → `MONGODB_URL` → `MONGO_URL`
* or the parts `MONGOHOST` + `MONGOPORT` + `MONGOUSER` + `MONGOPASSWORD`
  (built into a URI with `authSource=admin`)

> Prefer Atlas? Set just `MONGODB_URI=mongodb+srv://user:pass@cluster.mongodb.net`
> and skip the plugin. Whitelist `0.0.0.0/0` (or Railway's egress IPs) in Atlas.

### 3. Set the bot variables

Service → **Variables** → *Raw editor*:

```env
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef
BOT_TOKEN=123456789:AA...your-token...
ADMIN_ID=123456789
OWNER_ID=123456789
SUPPORT_USERNAME=YourSupportHandle
CURRENCY=₹
STORAGE_BACKEND=auto
```

Do **not** set `PORT` (Railway injects it) and do **not** upload a `.env` file.

### 4. Deploy & verify

Deploy → **Deploy logs** should end with:

```
Bot started successfully and is ready to receive commands!
```

Then:

```bash
railway domain                      # optional: gives you a public URL
curl -s https://<your-domain>/health
# {"status":"ok","storage":"MongoStore","accounts":{"live":6,"total":6}, …}
```

Railway's healthcheck hits `/health` automatically; the value can also be seen
in **Settings → Deploy → Healthcheck Path** (already set to `/health` by
`railway.json`).

### 5. Optional: Railway Volume

Mount a **Volume at `/data`** (Service → Settings → Volumes). The app detects a
writable `/data` and puts the JSON fallback + session file there, so even the
no-MongoDB case survives redeploys. In MongoDB mode nothing depends on it.

### 6. Daily use

* redeploy after a code change: `railway up` (or push to the connected branch)
* tail logs: `railway logs`
* all state (accounts, wallets, orders, channels, settings) is in MongoDB, so a
  redeploy is invisible to your users.

---

## ☁️ Option C — Render / Fly.io / Koyeb

Use the same `Dockerfile` as a **background worker**:

| | Value |
|---|---|
| Build | Dockerfile in repo root |
| Start command | `python main.py` |
| Env | same variables as Railway; `MONGODB_URI` from Atlas, or `MONGO_URL` |
| Disk (optional) | mount at `/data` when not using MongoDB |

Fly.io example:

```bash
fly launch --no-deploy            # detects the Dockerfile
fly secrets set API_ID=... API_HASH=... BOT_TOKEN=... ADMIN_ID=... \
                MONGODB_URI="mongodb+srv://..."
fly volumes create panel_data --size 1   # only if you skip MongoDB
fly deploy && fly logs
```

`PORT` is injected by these platforms too, so `/health` just works.

---

## 🖤 The web admin panel

The same HTTP port that answers `/health` also serves the **brutalist admin
dashboard** at `/admin`, so there is nothing extra to deploy:

```bash
# VPS / docker compose
open http://<server-ip>:8080/admin

# Railway / Render / Fly
open https://<your-domain>/admin
```

Log in with **your Telegram user id** (the value in `OWNER_ID`/`ADMIN_ID`, or any
owner/admin you added in the *Operators* tab) and the password from
`ADMIN_PANEL_PASSWORD`. Without that variable the bot generates one and prints it
in the log on first start — change it in *Settings ▸ `web.password`* at any time.

What it does: live dashboard, **plan-request queue** (approve/reject what users
asked for in *👑 Buy Premium*), users (wallet, plan grant/revoke, ban, DM), plans
(create/edit/delete incl. channel slots), every pricing/settings section, channels
(lock, owner, auto toggles, delete), orders (cancel/retry), the account pool, and
**multiple owners** management. It writes to the same MongoDB the bot uses, so
changes are visible in the bot instantly.

**On a phone** the dashboard switches to a mobile layout (swipeable tabs, big
touch targets, tables as cards, scrolling lists) — no app needed, just open
`/admin` in Chrome or Safari.

**Live refresh** patches only the changed boxes and pauses while you type, so an
edit is never lost. Long tables (recent orders/users, orders, users, channels)
scroll inside their own fixed-height box.

Set `WEB_ADMIN=0` to serve the health endpoints only.

---

## 🔍 Verifying a fresh deploy (2 minutes)

```bash
# 1. Process health + storage + account pool
curl -s <host>:<port>/health | python -m json.tool

# 2. Full panel behaviour against your real database (no Telegram credentials
#    needed — runs the real panels/orders/automation against a fake transport)
python scripts/live_check.py demo --mongo "mongodb+srv://..."
#    → "all checks passed — the panel behaves as expected"

# 3. Crash safety (write, hard-kill, verify from a fresh process)
python scripts/live_check.py crash-write  --mongo "<uri>"
python scripts/live_check.py crash-verify --mongo "<uri>"
#    → "everything survived the hard crash"
```

Then in Telegram: `/start` → user panel appears → add the bot to a channel
(*Add To Channel*) → post something → reactions should appear within seconds.

---

## 🧯 Troubleshooting

| Symptom | Fix |
|---|---|
| `dockerfile invalid: docker VOLUME at Line N is not supported` | you are deploying an older zip — this repo's Dockerfile has no `VOLUME`; re-download the latest package |
| `Environment configuration error … ADMIN_ID` | older builds required `ADMIN_ID`. Either add it in the dashboard **or** redeploy the current build — `OWNER_ID` / `ADMIN_IDS` alone is now enough |
| Deploy logs show `Environment variable 'X' not set` for optional vars | harmless — only the Telegram credentials plus one owner id are required |
| Values arrive with quotes (`"₹"`, `"8707101133"`) | handled: quotes, padding and stray newlines are stripped automatically since v2.1 |
| `storage: "JsonStore"` in `/health` while using a cloud host | MongoDB is not reachable, so the bot deliberately kept running on the local store. Check the log line above it for the reason (wrong password, Atlas IP allow-list, missing `dnspython`). With Atlas, whitelist `0.0.0.0/0` for a quick test |
| `MongoDB unreachable … falling back to the JSON store` | wrong credentials/host, or Atlas IP allow-list. Fix the URI; the bot keeps running meanwhile |
| `API_ID / API_HASH were rejected by Telegram` | wrong `API_ID`/`API_HASH` — the bot stops retrying on purpose |
| `BOT_TOKEN was rejected by Telegram` | reissue the token with @BotFather and update the variable |
| Railway deploy healthy but bot silent | make sure exactly one replica is running, and that `ADMIN_ID` is your own numeric id |
| `Could not read settings from store: name 'get_store_sync' is not defined` | fixed in this build — update and redeploy |
| `Channel registered: … owner=None` | Telegram did not report who added the bot. Update to the latest build; the actor is now detected from `added_by`/the action message, and the panel shows an **Assign to me** button for anything still unowned |
| Notifications show `</b>` or `<code>` as text | fixed in **v3.2** — every notification now asks Telegram to parse HTML |
| `Invalid reaction provided (only emoji are allowed)` | the emoji is not in Telegram's own reaction set. v3.2 filters such emojis out, repairs the stored list on boot and retries with another emoji; check **Admin panel → ❤️ Reactions → Emojis** |
| The same post got reactions twice | fixed in **v3.2** — a repeated/redelivered update is skipped per channel |
| A newly added account is not used in older channels | it joins them automatically after sign-in (and once after each boot), up to the free cap of 10 |
| The bot is removed but our accounts stay in the channel | fixed in **v3.2** — every account leaves, the channel record is dropped and you get a notification |
| `Could not resolve chat -100…` on **every** reaction | fixed in **v3.1** — Telegram's *not-a-member* invite answer (`ChatInvite`) has no `.chat`, so the old code never sent the import and accounts stayed outside a private channel. Accounts now always import the bot's invite link; a revoked link is replaced and the batch retried once |
| `Auto reactions … 0 ok, 0 failed` | the account pool is empty — sign in accounts from **Admin panel → 👤 Accounts → ➕ Add account**, then post again. The bot now warns you about this instead of staying silent |
| Bot added, but reactions still missing | check the bot has **post messages + invite users** in that channel (use the panel's *Add To Channel* button), and that at least one account is in the pool |
| `Could not create an invite link for …` | the bot needs the *Invite Users* permission in that channel; it retries automatically on the next post (and on a manual order too) |
| Reactions work on a public channel but not on a private one | private channels require the *Invite Users* permission so the pool accounts can join. Re-add the bot with all permissions (or make the channel public) |
| A user asks for views/comments but the buttons are locked | that is the premium gate — activate it per service: admin panel → 👥 Users → open the user → 👑 Give plan, or `/grant <user_id> views 30` |
| `0 reactions on every post` | the account pool is empty (panel → 👤 Accounts → add) or the accounts are not in the channel yet (private channel: check the invite permission) |
| `Another instance is using the same session file` | two deployments/containers share a session — stop the duplicate or set a different `BOT_SESSION_NAME` |
| Bot cannot react in a channel | re-add the bot with **post_messages + invite_users** (the panel's *Add To Channel* button does it for you) |
