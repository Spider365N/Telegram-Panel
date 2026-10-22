# VPS par deploy guide (Hinglish)

Yeh guide Ubuntu/Debian VPS ke liye hai. Aapke 4-vCPU / 32-GB VPS ke liye Compose me bot ke liye 3.25 CPU + 8 GB RAM aur MongoDB ke liye 0.75 CPU + 4 GB RAM limit set hai. Admin panel ka port **8070** hai.

## 1) VPS par Docker check/install

Agar Docker + Compose pehle se installed hain, yeh check karke next step par jao:

```bash
docker --version
docker compose version
```

Agar install nahi hain, Ubuntu/Debian par Docker ka official convenience installer use kar sakte ho:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
```

Iske baad SSH session se logout karke dobara login karo, phir check karo:

```bash
docker --version
docker compose version
```

> `get.docker.com` Docker ka installer hai. Agar VPS provider ka managed Docker install available hai, uska official method bhi use kar sakte ho.

## 2) Prepared code VPS par bhejo + persistent config file banao

Prepared edits is conversation ke `Telegram-Panel-deploy-ready.zip` bundle me hain; upstream GitHub repo me ye local edits abhi push nahi hue. Bundle ko apne computer se VPS par bhejo (local computer terminal par):

```bash
scp Telegram-Panel-deploy-ready.zip YOUR_SSH_USER@YOUR_VPS_IP:~/
ssh YOUR_SSH_USER@YOUR_VPS_IP
```

VPS par project extract karo:

```bash
sudo apt-get update
sudo apt-get install -y unzip
unzip -q ~/Telegram-Panel-deploy-ready.zip -d ~
cd ~/Telegram-Panel
```

Agar aapne code apne GitHub fork par push kar diya hai, tab `git clone https://github.com/YOUR_GITHUB_USER/Telegram-Panel.git` use kar sakte ho. Original upstream clone karne se unmodified code milega jab tak changes push na hon.

Ab persistent config file banao:

```bash
mkdir -p config
cp config.example.json config/config.json
chmod 700 config
chmod 600 config/config.json
nano config/config.json
```

`config/config.json` me in values ko apne credentials se replace karo:

| JSON key | Value kahan se milegi |
|---|---|
| `API_ID`, `API_HASH` | https://my.telegram.org/apps |
| `BOT_TOKEN` | Telegram par `@BotFather` → `/newbot` |
| `ADMIN_ID`, `OWNER_ID` | Apna numeric user ID, jaise `@userinfobot` se |
| `ADMIN_PANEL_PASSWORD` | Apna unique password; kam se kam 16 characters |
| `ADMIN_PANEL_SECRET` | Random secret; kam se kam 32 characters |

Password/secret generate karne ke liye (commands ke output ko copy karke JSON me paste karo):

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # panel password
openssl rand -hex 32                                               # panel signing secret
```

`ADMIN_ID`/`OWNER_ID` me quotes ke andar string ke bajaye number rakh sakte ho, misal: `123456789`. `ADMIN_IDS` JSON list hai; extra admins nahi hon to `[]` rehne do. `CHANNEL_ID` optional hai—empty rehne par message-forwarding feature off rahega.

**MongoDB URI, storage path aur port ko aam taur par change mat karo.** Compose internal MongoDB service, persistent volumes, aur port 8070 set karta hai. VPS par `.env` file ki zaroorat nahi.

JSON file me trailing comma mat chhodo. `ADMIN_PANEL_PASSWORD` aur `ADMIN_PANEL_SECRET` ke sample `CHANGE_ME...` values ko replace karna zaroori hai; warna bot start nahi hoga.

## 3) App + MongoDB start karo

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f bot
```

Logs me bot connection, MongoDB storage aur admin server ka port `8070` dekhna chahiye. Logs dekhne ke baad `Ctrl+C` sirf log-follow ko band karega; container chalta rahega.

VPS ke andar se health check:

```bash
curl -fsS http://127.0.0.1:8070/health
```

## 4) Admin panel safely kholo

Panel default se **sirf VPS ke localhost par** bind hota hai, public internet par nahi. Apne laptop/PC ke terminal me yeh SSH tunnel chalao:

```bash
ssh -N -L 8070:127.0.0.1:8070 YOUR_SSH_USER@YOUR_VPS_IP
```

`YOUR_SSH_USER` aur `YOUR_VPS_IP` ko apne VPS details se replace karo. Terminal khula rehne do, phir browser me kholo:

```text
http://127.0.0.1:8070/admin
```

Login me Telegram user ID aur JSON config wala `ADMIN_PANEL_PASSWORD` do. Pehli baar Telegram bot ko DM karke `/start` bhejna bhi zaroori ho sakta hai, taaki owner/admin user register ho.

**Port ko seedha Internet par expose mat karo.** Agar public access zaroori ho to HTTPS reverse proxy (Nginx/Caddy) + firewall/IP allow-list set karo; plain `http://VPS_IP:8070` par password bhejna safe nahi.

## 5) Restart, config change, update

Config edit karne ke baad:

```bash
nano config/config.json
docker compose restart bot
docker compose logs --tail=100 bot
```

Code update karne ke baad, archive-based install me naya bundle same VPS directory me upload karke extract/overwrite karo (yeh `config/` aur Docker named volumes ko delete nahi karta):

```bash
# Local computer se, newer bundle VPS par bhejo:
scp Telegram-Panel-deploy-ready.zip YOUR_SSH_USER@YOUR_VPS_IP:~/

# VPS par:
unzip -oq ~/Telegram-Panel-deploy-ready.zip -d ~
cd ~/Telegram-Panel
docker compose up -d --build
docker compose ps
```

Agar aap prepared code apne GitHub fork me push kar chuke ho, to `git pull` use karke uske baad `docker compose up -d --build` chala sakte ho.

## 6) Data loss se bachna

- `config/config.json` VPS ke project folder me hai; container restart/rebuild se delete nahi hota.
- MongoDB, JSON fallback/session files aur logs named Docker volumes me hain.
- `docker compose down` named volumes ko rakhta hai.
- **`docker compose down -v` mat chalana**—`-v` database aur saved bot data delete kar sakta hai.
- Project folder ko move/rename karne se pehle old containers/volumes ka backup plan bana lo.

## 7) Common errors

- **`config file is missing`**: Project directory ke andar `config/config.json` exist karta hai ya nahi check karo.
- **`Unknown runtime config key`**: JSON me key spelling/case check karo; supported uppercase keys `config.example.json` me hain.
- **Panel login reject hota hai**: `OWNER_ID`/`ADMIN_ID` sahi numeric Telegram ID hon; bot ko DM me `/start` bhejo; phir config password verify karke `docker compose restart bot` karo.
- **Port busy**: `docker compose ps` dekho. Host par `8070` occupied ho to compose ke `ports` mapping aur `HEALTH_PORT` dono ko same free port par badlo.
- **Telegram login code/2FA warnings**: Galat ya expire code, resend limit, ya wrong 2FA password app code bug nahi hota. Telegram ki cooldown ka wait karo aur latest code use karo.
- **Private channel reaction/view/comment fail**: Account ko channel resolve/join karna hoga; private channel ke liye valid invite aur zaroori invite permission honi chahiye. Telegram invalid post/discussion IDs ko reject kar sakta hai.
- **`CHANNEL_ID` warning**: Optional forwarding target set nahi hai; forwarding off rahegi. Isse bot startup fail nahi hota.
- **Auto views ON hain par views nahi badh rahe**: Logs me `no active views plan` aaye to plan recipient ka channel se link check karo. Admin Dashboard → Channels me **sponsor** se user ko link kar sakte ho bina owner transfer kiye; **owner** sirf ownership sach me badalni ho tab use karo. Automation refresh ho jayegi, aur agle naye post par views chalenge. Purane skipped posts auto-replay nahi hote. `Auto view requests on ...` log per-post accepted/failed API requests batata hai; accepted ka matlab Telegram ne request accept ki, public counter badha ye live verification alag hai.

Raw production logs me phone/account IDs, channel details, ya session filenames ho sakte hain—share karne se pehle redact karo.
