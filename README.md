# Telegram Panel 3.5.1

Telegram bot + web admin dashboard. The prepared VPS deployment uses Docker Compose, a persistent MongoDB volume, a private JSON config file, and a localhost-only admin port (**8070**).

> **Important:** This project stores Telegram account sessions and includes automated channel-engagement features. Use only accounts/channels you own or are authorized to manage, follow Telegram's terms, and understand that automated activity can trigger platform limits or account restrictions. Never publish bot tokens, API hashes, session files, or raw production logs.

## VPS quick start (Ubuntu/Debian)

These edits are in the prepared project bundle. **The upstream GitHub repository has not been modified by this workspace.** If deploying this exact version, use the `Telegram-Panel-deploy-ready.zip` bundle attached in this conversation, or first push the prepared code to your own fork.

Download the bundle to your computer, then copy it to the VPS (run the first command on your computer):

```bash
scp Telegram-Panel-deploy-ready.zip YOUR_SSH_USER@YOUR_VPS_IP:~/
ssh YOUR_SSH_USER@YOUR_VPS_IP
```

On the VPS, extract and enter the project:

```bash
sudo apt-get update
sudo apt-get install -y unzip
unzip -q ~/Telegram-Panel-deploy-ready.zip -d ~
cd ~/Telegram-Panel
```

Install Docker Engine + Compose if they are not already installed. See [`docs/DEPLOY_HINGLISH.md`](docs/DEPLOY_HINGLISH.md) for the Docker install commands. If you install Docker as a non-root user, add that user to the `docker` group and reconnect your SSH session before continuing.

Create the persistent, private config file:

```bash
mkdir -p config
cp config.example.json config/config.json
chmod 700 config
chmod 600 config/config.json
nano config/config.json
```

Fill `API_ID`, `API_HASH`, `BOT_TOKEN`, and your numeric `ADMIN_ID`/`OWNER_ID`. Set:

- `ADMIN_PANEL_PASSWORD` to a unique password of at least 16 characters.
- `ADMIN_PANEL_SECRET` to a random secret of at least 32 characters.

Generate values with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` (password) and `openssl rand -hex 32` (secret). Keep the values private. The sample placeholders intentionally prevent startup until replaced.

Start the bot and bundled MongoDB:

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

Check health on the VPS:

```bash
curl -fsS http://127.0.0.1:8070/health
```

The admin port is deliberately bound to the VPS loopback interface. From your own computer, open an SSH tunnel:

```bash
ssh -N -L 8070:127.0.0.1:8070 YOUR_SSH_USER@YOUR_VPS_IP
```

Then open **http://127.0.0.1:8070/admin** in your browser and log in with your Telegram user ID and `ADMIN_PANEL_PASSWORD`. Keep the SSH tunnel terminal open while using the panel.

For the full copy/paste walkthrough in Hinglish, see [`docs/DEPLOY_HINGLISH.md`](docs/DEPLOY_HINGLISH.md). Railway is also supported via environment variables; see [`docs/DEPLOY.md`](docs/DEPLOY.md).

## Configuration and persistence

- VPS secrets/settings are read from `config/config.json`; `.env` is not required by the Compose deployment.
- The config directory is mounted read-only. The entrypoint copies it into an ephemeral runtime file and starts Python as an unprivileged user.
- `mongo_data` persists accounts, users, orders, balances, settings, and session strings.
- `bot_data` persists legacy JSON state and Telethon session files; `bot_logs` holds rotating application logs.
- `docker compose down` keeps named volumes. **Do not use `docker compose down -v`** unless you intentionally want to erase stored data.
- Edit `config/config.json`, then run `docker compose restart bot` to apply a config change.
- This archive does not contain a Git working tree. To update an archive-based deployment, upload/extract the newer bundle over the same project directory, keep `config/`, and run `docker compose up -d --build`. If you publish the changes to your own fork, you can use `git pull` instead.

To expose the panel publicly, put HTTPS and an IP allow-list in front of it first; do not remove the loopback binding and expose plain HTTP to the Internet.

## Auto-views diagnosis and recovery

The newest supplied log shows the bot starting and restoring its account pool. It records an auto-view post being skipped because **no active views plan was found for a user linked to that channel**. A plan was granted later, but the original code did not log successful per-post view outcomes, so that export cannot confirm whether later posts got views.

This bundle fixes the observable path: it stores the channel adder separately from the owner, applies plans to the owner/adder/explicit sponsors only, resolves public channels by username, retries a stale private invite once, logs per-post Telegram request outcomes, alerts on a zero-result batch, and preserves post credits when delivery fails. The logs count requests accepted by Telegram, not a verified increase in the public view counter. It also makes duplicate post claims atomic under concurrent Telegram updates.

For a channel already in the database whose plan recipient is not linked:

1. Open **Admin Dashboard → Channels** through the SSH tunnel described above.
2. Use **sponsor** to link the plan holder without transferring channel ownership. Use **owner** only when ownership should actually move.
3. The dashboard refreshes auto-view automation immediately. The next new post is the first automatic retry; older skipped posts are not replayed or charged retroactively.

If the bot is added again, the updated registration path also fills a missing adder link. A plan granted to an unrelated user will not be attached to another user's channel; that is intentional tenant isolation.

The older export spans **17–21 October 2026**; the additional log is from **22 October 2026**. Other messages include Telegram-side login-code delivery limits, optional `CHANNEL_ID` forwarding being unset, private-channel access/invite permissions, invalid/deleted post references, and notifications to deleted users. These do not by themselves indicate a process crash loop. Never share raw logs: they can contain account, channel, and user identifiers.

The previous deployment listened on **8080**. This Compose setup uses **8070** for `/admin` and `/health`.

## Quality checks

Run the offline suite locally:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The automated suite uses Telegram fakes for its bot flows. A live Telegram login/engagement test requires your own credentials and is intentionally not run as part of this workspace review.
