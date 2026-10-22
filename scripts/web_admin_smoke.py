"""Live smoke test for the web admin panel over real HTTP.

Starts the *actual* ``HealthServer`` (the one the bot boots on Railway/VPS) on a
free port with the real service stack, then — from a worker thread, so the event
loop is never blocked — logs in over HTTP and downloads the pages the operator
uses:

    /health          open, JSON status
    /admin           redirect to the login page when not signed in
    /admin/login     brutalism login form
    /admin           the dashboard itself (after login)
    /admin/api/...   the JSON the dashboard reads / writes

Run:  .venv/bin/python scripts/web_admin_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASSWORD = "smoke-test-password"
OPERATOR = 9001
failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    mark = "✔" if condition else "✘"
    print(f"  {mark} {label}")
    if not condition:
        failures.append(label)
    return bool(condition)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _opener() -> urllib.request.OpenerDirector:
    jar = CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _get(opener, base: str, path: str, follow: bool = True):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    asker = opener
    if not follow:
        asker = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(opener._cookies if hasattr(opener, "_cookies") else CookieJar()),
            NoRedirect(),
        )
    try:
        with asker.open(f"{base}{path}", timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace"), response.headers
    except urllib.error.HTTPError as exc:  # redirects / 401 come through here
        return exc.code, exc.read().decode("utf-8", "replace"), exc.headers


def _post(opener, base: str, path: str, payload: dict):
    data = urllib.parse.urlencode(payload).encode()
    request = urllib.request.Request(f"{base}{path}", data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


async def run() -> None:
    os.environ.setdefault("TEST_MONGODB_URI", "mongodb://127.0.0.1:27017")
    os.environ["ADMIN_PANEL_PASSWORD"] = PASSWORD
    os.environ["WEB_ADMIN"] = "1"

    from tests.fakes import build_stack

    stack = await build_stack()
    bot = stack.bot
    bot.store = stack.store

    import src.config as app_config

    app_config.OWNER_ID = OPERATOR
    app_config.ADMIN_IDS = []

    port = free_port()
    from src.health import HealthServer

    server = HealthServer(port, host="127.0.0.1", bot=bot)
    started = await server.start()
    check(started, f"health + admin server listening on 127.0.0.1:{port}")
    base = f"http://127.0.0.1:{port}"

    captured: dict = {}

    def http_checks() -> dict:
        opener = _opener()

        print("\n1) Public health endpoint")
        status, body, _ = _get(opener, base, "/health")
        payload = json.loads(body)
        check(status == 200 and payload.get("status") == "ok", "/health answers ok + JSON")
        check("version" in payload, "/health reports the version")

        print("\n2) The dashboard is protected")
        status, _, headers = _get(opener, base, "/admin", follow=False)
        check(status in (301, 302) and "/admin/login" in headers.get("Location", ""),
              "/admin sends visitors to the login page")
        status, body, _ = _get(opener, base, "/admin/api/overview", follow=False)
        check(status == 401, "the JSON API refuses anonymous callers")

        print("\n3) Login page (brutalism)")
        status, body, _ = _get(opener, base, "/admin/login")
        check(status == 200 and "Operator user id" in body, "login form renders")
        check("--acid" in body and "box-shadow" in body, "brutalist styling is inline (no CDN)")

        print("\n4) Wrong password is refused")
        status, body = _post(opener, base, "/admin/api/login",
                             {"user_id": str(OPERATOR), "password": "wrong"})
        check("Wrong" in body or "wrong" in body.lower(), "bad password does not log in")

        print("\n5) Real login + dashboard download")
        status, body = _post(opener, base, "/admin/api/login",
                             {"user_id": str(OPERATOR), "password": PASSWORD})
        check(status == 200 and "Telegram Panel" in body, "login lands on the dashboard")
        status, body, _ = _get(opener, base, "/admin")
        check(status == 200 and '<main id="view">' in body and "Telegram Panel" in body,
              "admin page served after login")
        check("Requests" in body and "Operators" in body,
              "dashboard tabs include Requests/Operators")
        check("function setRegion(" in body and "function liveTick(" in body,
              "soft refresh: regions are patched, the page is never re-rendered")
        check("setInterval(liveTick" in body
              and "setInterval(() => { if (S.auto) draw();" not in body,
              "the 12s timer runs the soft refresher, not a full redraw")
        check("if (isEditing()) return setLiveState();" in body,
              "auto refresh pauses while a field is focused / edited")
        check("@media (max-width:760px)" in body and "table.mob td::before" in body,
              "mobile layout: stacked tables + phone breakpoint")
        check(".table-scroll{max-height" in body,
              "recent orders/users scroll inside a fixed box")
        check("font-size:16px" in body, "16px inputs (no iOS zoom)")
        check("viewport-fit=cover" in body and "theme-color" in body,
              "mobile viewport + theme colour")

        print("\n6) JSON API used by the dashboard")
        for path in ("overview", "users", "plans", "pricing", "channels", "orders",
                     "pool", "operators", "requests", "settings"):
            status, body, _ = _get(opener, base, f"/admin/api/{path}")
            ok = False
            try:
                ok = status == 200 and json.loads(body).get("ok") is True
            except json.JSONDecodeError:
                ok = False
            check(ok, f"/admin/api/{path} answers with data")

        print("\n7) A write from the dashboard is live in the services")
        status, body, _ = _get(opener, base, "/admin/api/settings")
        settings = json.loads(body)["settings"]
        settings["channels"]["free_limit"] = 3
        request = urllib.request.Request(
            f"{base}/admin/api/settings",
            data=json.dumps({"__all": settings}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=10) as response:
            check(response.status == 200, "settings POST accepted")

        print("\n8) Force subscribe from the dashboard (owner only)")
        status, body, _ = _get(opener, base, "/admin/api/force_sub")
        payload = json.loads(body)
        check(payload.get("ok") and payload.get("channels") == [], "empty gate list loads")
        request = urllib.request.Request(
            f"{base}/admin/api/force_sub",
            data=json.dumps({"link": "-100555000111", "title": "Smoke Channel"}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=10) as response:
            added = json.loads(response.read().decode())
        check(added.get("added") == -100555000111, "channel added through the API")
        request = urllib.request.Request(
            f"{base}/admin/api/force_sub/toggle",
            data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=10) as response:
            toggled = json.loads(response.read().decode())
        check(toggled.get("enabled") is True, "gate switched on")
        return {"free_limit": settings["channels"]["free_limit"],
                "force_sub": toggled.get("enabled")}

    seen = await asyncio.to_thread(http_checks)
    live = await stack.settings.section("channels")
    check(live["free_limit"] == 3 and seen["free_limit"] == 3,
          "the bot's own settings service already sees the change")
    from src.services.forcesub import get_force_sub_service

    gate = get_force_sub_service()
    check(await gate.enabled() is True and len(await gate.channels()) == 1,
          "the bot's join gate sees the channel added in the browser")

    await server.stop()
    check(not server._runner, "the server shuts down cleanly")

    print("\n" + "=" * 60)
    if failures:
        print(f"✘ {len(failures)} check(s) failed:")
        for item in failures:
            print(f"   - {item}")
        raise SystemExit(1)
    print("✔ web admin smoke test passed — the dashboard is live and synced")


if __name__ == "__main__":
    asyncio.run(run())
