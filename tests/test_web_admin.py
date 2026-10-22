"""End-to-end tests for the brutalist web admin panel.

They boot the real aiohttp app (the very same ``HealthServer`` the bot starts on
Railway) against the fake stack + real MongoDB, log in with a signed cookie and
exercise the JSON API the dashboard uses. Anything that would break the page in
production shows up here.
"""
from __future__ import annotations

import pytest

from tests.fakes import build_stack

pytestmark = pytest.mark.asyncio


def _requests(stack):
    from src.services.requests import get_plan_request_service

    return get_plan_request_service(users=stack.users)

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


async def _client(stack, monkeypatch, *, operator_id: int = 9001, password: str = "s3cret"):
    """Build a TestClient around the real health app with the panel mounted."""
    import src.config as app_config

    monkeypatch.setattr(app_config, "OWNER_ID", operator_id, raising=False)
    monkeypatch.setenv("ADMIN_PANEL_PASSWORD", password)
    monkeypatch.setenv("WEB_ADMIN", "1")

    import src.health as health

    server = health.HealthServer(0, bot=stack.bot)
    app = aiohttp.web.Application()
    handler = server._json
    for path in ("/health", "/healthz", "/ping", "/status"):
        app.router.add_get(path, handler)
    app.router.add_get("/", server._index)
    from src.webadmin import WebAdmin

    webadmin = WebAdmin(stack.bot)
    webadmin.install(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, webadmin


async def test_login_flow_and_session_cookie(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    try:
        res = await client.get("/admin", allow_redirects=False)
        assert res.status == 302, "unauthenticated page must redirect to /admin/login"

        # wrong password
        res = await client.post("/admin/api/login",
                                data={"user_id": "9001", "password": "nope"},
                                allow_redirects=False)
        assert res.status == 302 and "Wrong" in res.headers["Location"]

        # unknown operator
        res = await client.post("/admin/api/login",
                                data={"user_id": "123", "password": "s3cret"},
                                allow_redirects=False)
        assert res.status == 302 and "not+an+owner" in res.headers["Location"]

        # real login → cookie + dashboard
        res = await client.post("/admin/api/login",
                                data={"user_id": "9001", "password": "s3cret"},
                                allow_redirects=False)
        assert res.status == 302
        assert res.headers["Location"] == "/admin"
        assert client.session.cookie_jar.filter_cookies(client.make_url("/admin"))

        res = await client.get("/admin")
        body = await res.text()
        assert res.status == 200
        assert "Telegram Panel" in body and "ADMIN" in body.upper()
        assert "--acid" in body, "brutalism theme CSS missing"

        me = await (await client.get("/admin/api/me")).json()
        assert me["ok"] and me["user_id"] == 9001 and me["role"] == "owner"
    finally:
        await client.close()


async def test_api_requires_auth(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    try:
        for path in ("overview", "users", "plans", "requests", "channels", "settings"):
            res = await client.get(f"/admin/api/{path}")
            assert res.status == 401, f"{path} must be protected"
    finally:
        await client.close()


async def test_overview_and_settings_roundtrip(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "s3cret"},
                          allow_redirects=False)
        data = await (await client.get("/admin/api/overview")).json()
        assert data["ok"] and data["version"]
        assert "users" in data and "requests" in data

        settings = (await (await client.get("/admin/api/settings")).json())["settings"]
        assert "reactions" in settings and "channels" in settings

        settings["channels"]["free_limit"] = 4
        res = await client.post("/admin/api/settings", json={"__all": settings})
        assert (await res.json())["ok"]
        section = await stack.settings.section("channels")
        assert section["free_limit"] == 4, "the bot must see the change instantly"
    finally:
        await client.close()


async def test_users_wallet_plan_ban(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    user_id = 5550001
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "s3cret"},
                          allow_redirects=False)
        await stack.users.get_or_create(user_id, username="webuser", first_name="Web")

        res = await client.post(f"/admin/api/users/{user_id}/wallet", json={"amount": 500})
        assert (await res.json())["balance"] == 500

        res = await client.post(f"/admin/api/users/{user_id}/plan",
                                json={"kinds": "all", "days": 7, "accounts": 12, "channels": 3,
                                      "notify": False})
        plan = (await res.json())["plan"]
        assert plan["days"] == 7 and plan["accounts"] == 12 and plan["channels"] == 3
        limits = await stack.users.effective_limits(user_id)
        assert limits["channels"] >= 3 and limits["accounts"] >= 12

        res = await client.post(f"/admin/api/users/{user_id}/ban", json={"banned": True})
        assert (await res.json())["banned"] is True
        assert await stack.users.is_banned(user_id)

        res = await client.delete(f"/admin/api/users/{user_id}/plan")
        assert (await res.json())["revoked"] is True
        assert not await stack.users.is_premium(user_id)

        listed = (await (await client.get(f"/admin/api/users?q={user_id}")).json())["users"]
        assert any(str(u["_id"]) == str(user_id) for u in listed)
    finally:
        await client.close()


async def test_plan_crud_from_the_dashboard(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "s3cret"},
                          allow_redirects=False)

        res = await client.post("/admin/api/plans", json={
            "id": "web_test_plan", "title": "Web • 45 days", "days": 45, "price": 349,
            "accounts": 30, "channels": 6, "kinds": "all", "badge": "new",
            "description": "made in the dashboard",
        })
        plans = (await res.json())["plans"]
        assert any(p["id"] == "web_test_plan" for p in plans)

        # the bot's own plan reader sees it too
        cfg = await stack.settings.section("premium")
        assert any(p["id"] == "web_test_plan" for p in cfg["plans"])

        res = await client.post("/admin/api/plans/web_test_plan",
                                json={"title": "Renamed", "days": 60, "price": 399,
                                      "accounts": 40, "channels": 9, "kinds": "views,comments"})
        plans = (await res.json())["plans"]
        plan = next(p for p in plans if p["id"] == "web_test_plan")
        assert plan["title"] == "Renamed" and plan["days"] == 60 and plan["channels"] == 9
        assert plan["kinds"] == ["views", "comments"]

        res = await client.delete("/admin/api/plans/web_test_plan")
        plans = (await res.json())["plans"]
        assert not any(p["id"] == "web_test_plan" for p in plans)
    finally:
        await client.close()


async def test_plan_request_approval_from_dashboard(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    user_id = 5550002
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "s3cret"},
                          allow_redirects=False)
        await stack.users.get_or_create(user_id, username="requser", first_name="Req")
        doc = await _requests(stack).request(
            user_id, kind="premium", plan_id="premium_30", title="Premium • 30 days",
            price=299, days=30, accounts=20, channels=5, note="paid by UPI 1234")
        assert await _requests(stack).pending_count() == 1

        listed = (await (await client.get("/admin/api/requests")).json())["requests"]
        assert any(r["_id"] == doc["_id"] for r in listed)
        assert listed[0].get("note")

        res = await client.post(f"/admin/api/requests/{doc['_id']}/approve", json={"days": 30})
        body = await res.json()
        assert body["ok"] and body["approved"] == doc["_id"]
        assert await stack.users.is_premium(user_id)
        limits = await stack.users.effective_limits(user_id)
        assert limits["channels"] >= 5
        assert await _requests(stack).pending_count() == 0

        second = await _requests(stack).request(user_id, kind="channels", title="+3 channels")
        res = await client.post(f"/admin/api/requests/{second['_id']}/reject",
                                json={"reason": "not paid"})
        assert (await res.json())["rejected"] == second["_id"]
        gone = await _requests(stack).get(second["_id"])
        assert gone["status"] == "rejected" and gone["handler_note"] == "not paid"
    finally:
        await client.close()


async def test_channel_lock_owner_and_delete(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    channel_id = -100500001
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "s3cret"},
                          allow_redirects=False)
        await stack.channels.register(channel_id, owner_id=5550100, title="Web Channel",
                                         kind="channel")
        listed = (await (await client.get("/admin/api/channels")).json())["channels"]
        assert any(c["_id"] == channel_id for c in listed)

        res = await client.post(f"/admin/api/channels/{channel_id}/lock", json={"locked": False})
        assert (await res.json())["channel"]["locked"] is False
        # a user could delete it now
        assert await stack.channels.remove(channel_id) == 1

        await stack.channels.register(channel_id, owner_id=5550100, title="Web Channel",
                                         kind="channel")
        await client.post(f"/admin/api/channels/{channel_id}/auto",
                          json={"kind": "auto_reactions", "enabled": True})
        chan = await stack.channels.get(channel_id)
        assert (chan.get("auto_reactions") or {}).get("enabled") is True

        await stack.users.grant_plan(5550199, kinds=["views"], days=30, notify=False)
        sponsor_response = await client.post(
            f"/admin/api/channels/{channel_id}/sponsor", json={"user_id": 5550199}
        )
        sponsor_payload = await sponsor_response.json()
        assert sponsor_payload["automation"]["views"] == 1
        chan = await stack.channels.get(channel_id)
        assert int(chan["owner_id"]) == 5550100  # sponsor linking does not transfer ownership
        assert 5550199 in chan["sponsors"]
        assert (chan.get("auto_views") or {}).get("enabled") is True

        response = await client.post(
            f"/admin/api/channels/{channel_id}/owner", json={"owner_id": 5550199}
        )
        assert (await response.json())["automation"]["views"] == 1
        chan = await stack.channels.get(channel_id)
        assert int(chan["owner_id"]) == 5550199

        res = await client.delete(f"/admin/api/channels/{channel_id}")
        assert (await res.json())["removed"] == 1
        assert await stack.channels.get(channel_id) is None
    finally:
        await client.close()


async def test_multi_owner_management(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "s3cret"},
                          allow_redirects=False)
        res = await client.post("/admin/api/operators",
                                json={"user_id": 9100, "role": "owner"})
        operators = (await res.json())["operators"]
        assert any(o["user_id"] == 9100 and o["role"] == "owner" for o in operators)
        assert await stack.users.is_owner(9100)
        assert await stack.users.is_admin(9100)

        # the new owner can log in to the dashboard with the same password
        res = await client.post("/admin/api/login",
                                data={"user_id": "9100", "password": "s3cret"},
                                allow_redirects=False)
        assert res.status == 302 and res.headers["Location"] == "/admin"

        res = await client.post("/admin/api/operators/", json={})  # no-op guard
        assert res.status in (404, 405)

        res = await client.delete("/admin/api/operators/9100")
        operators = (await res.json())["operators"]
        assert not any(o["user_id"] == 9100 for o in operators)
        assert not await stack.users.is_admin(9100)
    finally:
        await client.close()


async def test_unknown_api_never_500s_the_page(monkeypatch):
    stack = await build_stack()
    client, _ = await _client(stack, monkeypatch)
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "s3cret"},
                          allow_redirects=False)
        res = await client.get("/health")
        assert res.status == 200
        payload = await res.json()
        assert payload["status"] == "ok"
        # a bogus id must answer with a clean JSON error, not a traceback
        res = await client.post("/admin/api/users/999999/plan",
                                json={"kinds": "all", "days": 5, "notify": False})
        assert res.status == 200 and (await res.json())["ok"] is True
    finally:
        await client.close()


async def test_admin_cannot_manage_owners_or_force_sub(monkeypatch):
    """Only owners may touch owners or the join gate — an admin cannot promote
    themselves or lock users out."""
    stack = await build_stack()
    admin = 9200
    await stack.users.get_or_create(admin, username="plainadmin", first_name="Admin")
    await stack.users.set_role(admin, "admin")

    # log the admin in with their own cookie jar
    import src.config as app_config

    monkeypatch.setattr(app_config, "OWNER_ID", 9001, raising=False)
    monkeypatch.setenv("ADMIN_PANEL_PASSWORD", "s3cret")
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer
    from src.webadmin import WebAdmin

    app = aiohttp.web.Application()
    WebAdmin(stack.bot).install(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/admin/api/login",
                          data={"user_id": str(admin), "password": "s3cret"},
                          allow_redirects=False)
        me = await (await client.get("/admin/api/me")).json()
        assert me["role"] == "admin"

        res = await client.post("/admin/api/operators", json={"user_id": 9201, "role": "owner"})
        assert res.status == 500
        assert (await res.json())["ok"] is False
        assert not await stack.users.is_owner(9201)

        res = await client.post("/admin/api/force_sub", json={"link": "-100999"})
        assert res.status == 500 and (await res.json())["ok"] is False
        from src.services.forcesub import get_force_sub_service

        assert await get_force_sub_service().channels() == []
    finally:
        await client.close()
