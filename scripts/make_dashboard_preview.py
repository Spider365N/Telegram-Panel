"""Build a self-contained preview of the dashboard at two widths.

Writes ``/home/user/dashboard-preview.html``:

* the **desktop pane** is the real dashboard (CSS + JS from ``src/webadmin.py``)
  running against in-memory sample data — tabs are clickable;
* the **phone pane** is a frozen render of the same code at 390 px, produced by
  jsdom, so the stacked-card layout can be inspected without a phone.

Every breakpoint in the CSS is rewritten to a container query, so each pane keeps
its own layout no matter how wide the window is. No server, no network.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = pathlib.Path("/home/user/dashboard-preview.html")
INNER = pathlib.Path("/tmp/dash_inner.html")

DATA = {
    "overview": {
        "version": "3.5.0", "storage": "Mongo", "uptime": 8640, "users": 42,
        "orders": 7, "active_orders": 2, "revenue": 1500, "accounts_live": 5,
        "accounts_total": 6, "channels": 3, "requests": 1, "topups": 0,
        "operators": [{"user_id": 8707101133, "role": "owner"}],
        "recent_orders": [
            {"_id": "66f1a2b3c4d5e6f708192a3b", "user_id": 6151748243, "kind": "reactions",
             "status": "completed", "price": 10, "created_at": "2026-10-17 11:04"},
            {"_id": "66f1a2b3c4d5e6f708192a3c", "user_id": 6974168540, "kind": "views",
             "status": "pending", "price": 25, "created_at": "2026-10-17 11:52"},
        ],
        "recent_users": [
            {"_id": 6151748243, "first_name": "DEMON KING !!", "balance": 500,
             "created_at": "2026-10-15"},
            {"_id": 6974168540, "first_name": "Kaka Kaka", "balance": 0,
             "created_at": "2026-10-16"},
        ],
    },
    "requests": {"requests": [
        {"_id": "66f1aa", "user_id": 6151748243, "title": "Premium", "price": 199,
         "days": 30, "channels": 5, "accounts": 15, "status": "pending",
         "created_at": "2026-10-17"},
    ]},
    "users": {"users": [
        {"_id": 6151748243, "first_name": "DEMON KING !!", "balance": 500, "banned": False,
         "plan": {"type": "premium", "kinds": ["reactions", "views"],
                  "expires_at": "2027-01-18"}},
        {"_id": 6974168540, "first_name": "Kaka Kaka", "balance": 0, "banned": False,
         "plan": {}},
        {"_id": 8707101133, "first_name": "UnEven", "balance": 341.4, "banned": True,
         "plan": {"type": "premium", "kinds": ["all"], "expires_at": "2027-01-17"}},
    ]},
    "plans": {"plans": [{"id": "premium", "title": "Premium", "days": 30, "price": 199,
                         "accounts": 15, "channels": 5, "kinds": ["all"],
                         "badge": "", "description": ""}]},
    "pricing": {"sections": {"reactions": {"free_account_limit": 8}}},
    "channels": {"channels": [
        {"_id": "-1004312382560", "title": "Test User Channel #1", "owner_id": 6974168540,
         "auto_reactions": {"enabled": True}, "auto_views": {"enabled": False},
         "locked": False},
        {"_id": "-1003876718689", "title": "Promo", "owner_id": 7938842751,
         "auto_reactions": {"enabled": True}, "auto_views": {"enabled": False},
         "locked": True},
    ]},
    "orders": {"orders": [
        {"_id": "66f1a2b3c4d5e6f708192a3b", "user_id": 6151748243, "kind": "reactions",
         "status": "pending", "price": 10, "paid_with": "wallet",
         "created_at": "2026-10-17"},
    ]},
    "pool": {"live": 6, "total": 6, "accounts": [
        {"_id": "tg_6974168540", "phone": "+919791234567", "name": "Kaka Kaka",
         "status": "active", "reactions_done": 28, "views_done": 15,
         "last_seen": "2026-10-17"},
        {"_id": "tg_8481611674", "phone": "+917209738889", "name": "Golu Golu",
         "status": "active", "reactions_done": 8, "views_done": 6,
         "last_seen": "2026-10-17"},
    ]},
    "operators": {"operators": [
        {"user_id": 8707101133, "role": "owner", "added_at": "env"},
    ]},
    "force_sub": {"enabled": True, "channels": [
        {"id": "-1001234567890", "title": "Updates", "username": "mychannel",
         "invite": None, "enabled": True},
    ]},
    "settings": {"settings": {"reactions": {}, "channels": {}}},
}


def parts() -> tuple[str, str]:
    """Return the dashboard CSS and JS exactly as the app ships them."""
    source = (ROOT / "src" / "webadmin.py").read_text(encoding="utf-8")
    css = source.split('BRUTAL_CSS = r"""', 1)[1].split('"""', 1)[0]
    js = source.split('BRUTAL_JS = r"""', 1)[1].split('"""', 1)[0]
    return css, js


def prelude() -> str:
    """A fetch() that answers from ``DATA`` instead of the server."""
    return """
  window.__DATA = %s;
  Object.defineProperty(document, "hidden", {get: () => false, configurable: true});
  const ROUTES = ["me", "users", "overview", "requests", "plans", "pricing",
                  "channels", "orders", "pool", "operators", "force_sub", "settings"];
  window.fetch = async (url) => {
    const path = String(url).replace("/admin/api/", "");
    let data = {ok: true};
    if (path.startsWith("me")) {
      data = {ok: true, user_id: 8707101133, role: "owner", version: "3.5.0"};
    } else {
      const key = ROUTES.find((r) => path.startsWith(r) && r !== "me");
      data = Object.assign({ok: true}, key ? window.__DATA[key] : {});
    }
    return {status: 200, ok: true, json: async () => data};
  };
""" % json.dumps(DATA)


def inner_page(css: str, js: str) -> str:
    """The dashboard as a normal page — used for the jsdom phone render."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<style>{css}</style></head><body>"
        "<header><div class='brand'>TELEGRAM PANEL</div>"
        "<div class='meta'><span id='who'>operator 8707101133</span>"
        "<span id='live-state' class='badge live'>live</span>"
        "<span id='updated'></span>"
        "<button id='auto' class='b sm'>live</button>"
        "<button id='refresh' class='b sm'>refresh</button></div></header>"
        "<nav id='nav'></nav><main id='view'></main>"
        "<div id='toast' class='toast'></div>"
        f"<script>{prelude()}</script><script>{js}</script>"
        "</body></html>"
    )


def snapshots(inner: pathlib.Path) -> dict[str, dict[str, str]]:
    """Render a few tabs at phone width with jsdom; empty when Node is missing."""
    node = shutil.which("node")
    if not node:
        print("note: node not found - phone pane shows a placeholder")
        return {}
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="tp-snap-"))
    shutil.copy(ROOT / "scripts" / "snapshot_dashboard.js", workdir / "snap.js")

    # jsdom may live next to the repo (/tmp/domtest), in NODE_PATH or globally
    env = dict(os.environ)
    for candidate in (env.get("NODE_PATH", ""), str(ROOT), "/tmp/domtest/node_modules"):
        if candidate and (pathlib.Path(candidate) / "jsdom").is_dir():
            env["NODE_PATH"] = candidate
            break
    else:
        root = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True)
        if root.returncode == 0 and (pathlib.Path(root.stdout.strip()) / "jsdom").is_dir():
            env["NODE_PATH"] = root.stdout.strip()

    probe = subprocess.run([node, "-e", "require.resolve('jsdom')"],
                           cwd=workdir, capture_output=True, env=env)
    if probe.returncode != 0:
        print("note: jsdom not installed - phone pane shows a placeholder")
        return {}
    subprocess.run([node, "snap.js", str(inner), str(workdir / "snap.json")],
                   cwd=workdir, check=True, timeout=180, capture_output=True, env=env)
    return json.loads((workdir / "snap.json").read_text(encoding="utf-8"))


def clean(markup: str) -> str:
    """Snapshots are inert copies; their ids would fight the live dashboard."""
    return re.sub(r'\s(?:id|data-h)="[^"]*"', "", markup)


OUTER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telegram Panel v3.5.0 - dashboard preview</title>
<style>
__CSS__
  html,body{background:#e9e7dd}
  body{font-family:"Courier New",monospace}
  .top{padding:16px 20px;border-bottom:6px solid #0b0b0b;background:#f6f5ef;
       display:flex;gap:12px;flex-wrap:wrap;align-items:center}
  .top h1{margin:0;font-size:17px;letter-spacing:1.5px;text-transform:uppercase}
  .top .note{font-size:12px;background:#d6ff00;border:2px solid #0b0b0b;padding:3px 8px}
  .wrap{display:flex;gap:22px;padding:20px;align-items:flex-start;flex-wrap:wrap}
  .pane{background:#f6f5ef;border:5px solid #0b0b0b;box-shadow:10px 10px 0 #0b0b0b}
  .pane > h2{margin:0;padding:8px 12px;background:#0b0b0b;color:#f6f5ef;
             font-size:12px;letter-spacing:2px;text-transform:uppercase}
  .frame{container-type:inline-size;background:#f6f5ef}
  .desk .frame{width:1120px}
  .phone .frame{width:390px}
  .pane .cap{padding:6px 10px;font-size:11px;background:#d6ff00;border-top:3px solid #0b0b0b}
  .switcher{padding:0 20px 26px;display:flex;gap:8px;flex-wrap:wrap}
  .switcher button{font-family:inherit}
</style></head>
<body>
  <div class="top">
    <h1>&#9672;&#9672; Telegram Panel v3.5.0 &mdash; dashboard preview</h1>
    <span class="note">same code, same data</span>
    <span class="note">desktop pane is live &middot; phone pane is a real render</span>
  </div>
  <div class="wrap">
    <div class="pane desk"><h2>desktop &middot; 1120 px</h2>
      <div class="frame" id="deskFrame">
        <header><div class="brand">&#9672;&#9672; TELEGRAM PANEL</div>
          <div class="meta"><span id="who">operator 8707101133 &middot; owner</span>
            <span id="live-state" class="badge live">&#9679; live</span>
            <span id="updated"></span>
            <button id="auto" class="b sm">live</button>
            <button id="refresh" class="b sm">&#8635;</button></div></header>
        <nav id="nav"></nav><main id="view"></main>
        <div id="toast" class="toast"></div>
      </div>
      <div class="cap">click the tabs &mdash; this pane runs the real dashboard JavaScript</div>
    </div>

    <div class="pane phone" id="phonePane"><h2>phone &middot; 390 px</h2>
      <div class="frame">
        <header><div class="brand">&#9672;&#9672; TELEGRAM PANEL</div>
          <div class="meta"><span class="badge live">&#9679; live</span>
            <button class="b sm">&#10074;&#10074; live</button><button class="b sm">&#8635;</button>
            <button class="b sm">exit</button></div></header>
        <nav id="pnav"></nav><main id="pview"></main>
      </div>
      <div class="cap">rendered by the same code &mdash; pick a tab below</div>
    </div>
  </div>
  <div class="switcher" id="switcher"></div>

  <script>var SNAP = __SNAP__;</script>
  <script>
    (function () {
      function showPhone(tab) {
        var s = SNAP[tab];
        if (!s) return;
        document.getElementById("pnav").innerHTML = s.nav;
        document.getElementById("pview").innerHTML = s.view;
        Array.prototype.forEach.call(
          document.querySelectorAll("#switcher button"),
          function (b) { b.style.background = b.textContent === tab ? "#d6ff00" : ""; });
      }
      var bar = document.getElementById("switcher");
      Object.keys(SNAP).forEach(function (tab) {
        var b = document.createElement("button");
        b.className = "b sm";
        b.textContent = tab;
        b.onclick = function () { showPhone(tab); };
        bar.appendChild(b);
      });
      showPhone(SNAP.users ? "users" : Object.keys(SNAP)[0]);
    })();
  </script>
  <script>__PRELUDE__</script>
  <script>__JS__</script>
</body></html>
"""


def main() -> None:
    css, js = parts()
    inner = inner_page(css, js)
    INNER.write_text(inner, encoding="utf-8")

    snap = {tab: {"nav": clean(v["nav"]), "view": clean(v["view"])}
            for tab, v in snapshots(INNER).items()}
    if not snap:
        snap = {"overview": {"nav": "", "view":
                "<div class='card'><h2>phone preview</h2><p>Install Node + jsdom "
                "and run this script again.</p></div>"}}

    # every breakpoint becomes a container query so each pane keeps its layout
    responsive = re.sub(r"@media \(max-width:(\d+)px\)",
                        r"@container (max-width:\1px)", css)

    outer = (OUTER.replace("__CSS__", responsive)
                  .replace("__SNAP__", json.dumps(snap))
                  .replace("__PRELUDE__", prelude())
                  .replace("__JS__", js))
    OUT.write_text(outer, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(snap)} phone tabs)")


if __name__ == "__main__":
    main()
