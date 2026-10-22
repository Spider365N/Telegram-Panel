# Deployment guide: VPS (Docker Compose) and Railway

The recommended production target is a Linux VPS with Docker Compose. The Compose file starts the bot and a private MongoDB 7 service, persists application state in named volumes, loads secrets from a host-side JSON file, and binds the admin/health server to **127.0.0.1:8070**. A 4-vCPU / 32-GB VPS has ample capacity for the included limits (bot: 3.25 CPU / 8 GB; MongoDB: 0.75 CPU / 4 GB).

For a step-by-step Hinglish guide with copy/paste commands, see [`DEPLOY_HINGLISH.md`](DEPLOY_HINGLISH.md).

## Docker Compose on a VPS (recommended)

The prepared edits in this delivery are local to the provided bundle and have not been pushed to the upstream GitHub repository. Upload/extract `Telegram-Panel-deploy-ready.zip` (or clone your own fork after pushing these changes), then run the following from the project root:

```bash
cd ~/Telegram-Panel
mkdir -p config
cp config.example.json config/config.json
chmod 700 config
chmod 600 config/config.json
nano config/config.json
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

Set `API_ID`, `API_HASH`, `BOT_TOKEN`, `ADMIN_ID`/`OWNER_ID`, a unique panel password of at least 16 characters, and a random panel secret of at least 32 characters. The sample values are deliberately invalid and must be replaced. Keep the JSON file private; Compose mounts it read-only. `MONGODB_URI`, `DATA_DIR`, `CLIENTS_JSON_PATH`, and `HEALTH_PORT` are supplied by Compose so they match the internal MongoDB service, persistent data volume, and port 8070.

Verify on the VPS:

```bash
curl -fsS http://127.0.0.1:8070/health
```

Open the dashboard via an SSH tunnel rather than exposing plain HTTP publicly:

```bash
ssh -N -L 8070:127.0.0.1:8070 YOUR_SSH_USER@YOUR_VPS_IP
```

Visit `http://127.0.0.1:8070/admin`. The port mapping in `docker-compose.yml` is intentionally localhost-only. If public access is required, use HTTPS plus a firewall/IP allow-list in a reverse proxy.

State is stored in named volumes (`mongo_data`, `bot_data`, `bot_logs`). `docker compose down` preserves them; **do not use `docker compose down -v`** unless you deliberately want to erase them. For an archive-based install, upload/extract the newer bundle over the project directory and run `docker compose up -d --build`; `git pull` is available only if you push the prepared source to your own fork. To apply a JSON config edit, run `docker compose restart bot`.

## Railway (environment-variable deployment)

Railway can build the repository's Dockerfile directly. Do not set `CONFIG_SOURCE_FILE` there unless you also mount a config file: that variable is used by Compose to require and copy the VPS JSON configuration. Without it, the entrypoint uses Railway's environment variables and the existing platform-port behavior.

1. Create a Railway project from this GitHub repository and add a MongoDB service (or use MongoDB Atlas).
2. Add `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID` or `ADMIN_ID`, and a secure `ADMIN_PANEL_PASSWORD` / `ADMIN_PANEL_SECRET` in the bot service's Variables. Add `MONGO_URL` or `MONGODB_URI` for persistence.
3. Do not hard-code `PORT`; Railway injects it. The app serves `/health` and `/admin` on the platform port.
4. Deploy and inspect the service logs. If you need Telethon session files to survive redeploys as well, attach a persistent volume at `/data` and set `DATA_DIR=/data`.

Railway environment values, tokens, and Telegram session data are sensitive. Use private variables and never paste them into public issue reports.

## Auto-views troubleshooting

If the bot says auto views are enabled but no view request runs, inspect the latest bot log for `auto views skipped` or `Auto view requests on ...`. A skip for “no active views plan” means the plan is either missing/expired or its recipient is not linked to that channel. In **Admin Dashboard → Channels**, choose **sponsor** to link the plan holder without changing the channel owner; use **owner** only for a real ownership transfer. The updated dashboard refreshes automation immediately, and views begin on the next new post. Old skipped posts are not replayed automatically.

Successful and failed view-request batches now have per-post outcome logs. “Accepted” means Telegram's API request returned successfully; it does not independently verify that the public view counter increased. Failed batches do not consume finite post credits. Public channels are resolved by username; private channels use the stored invite and get one fresh-link retry if Telegram reports an invite problem.

## Operational/security notes

- The VPS JSON file is the single human-edited configuration source; there is no `.env` file in the recommended Compose workflow.
- MongoDB is not published to the host. It is reachable only on the private Compose network.
- Bot and Mongo logs are size-limited by Docker's `json-file` rotation; the application's own log files rotate at 10 MiB with five backups.
- Telethon `.session` files and all runtime state live on persistent storage. Back up volumes before destructive maintenance.
- Do not share raw production logs or session files. Logs can contain account, channel, and user identifiers.
- Automated Telegram activity may trigger rate limits or account restrictions. Use only accounts and channels you are authorized to manage, and follow Telegram's terms.
