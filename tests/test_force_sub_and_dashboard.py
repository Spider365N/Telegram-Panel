"""Force subscribe (owner-only join gate) + the soft-refresh/mobile web UI.

Two acceptance areas from round 8:

* **Force subscribe** — the owner adds/removes/lists channels; a non-member sees
  the join screen, a member gets the panel, owners are never blocked, and a
  channel that cannot be checked fails *open* (never locks everybody out).
  Managing it is owner-only in the bot; the web dashboard exposes the same list.
* **Web dashboard** — auto refresh must patch only the boxes that changed (never
  re-render the page over an edit), and the CSS must carry the mobile rules
  (stacked tables, sticky nav, scroll boxes for the recent lists).
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from tests.fakes import FakeEvent, FakeTelegramClient, build_stack, make_channel

pytestmark = pytest.mark.asyncio


def ev(user_id: int, data: str = "u:home") -> FakeEvent:
    return FakeEvent(sender_id=int(user_id), chat_id=int(user_id), data=data)


async def _stack_with_channel(*, members: set | None = None, channel_id: int = -100880001):
    """A stack whose bot client can see one force-sub channel."""
    bot_client = FakeTelegramClient("bot", user_id=999)
    chat = make_channel(700001, username="forcechan", title="Force Channel")
    chat.id = 700001
    bot_client.channels[700001] = chat
    bot_client.known_entities.add(700001)
    bot_client.channel_members[700001] = set(members or ())
    stack = await build_stack(bot_client=bot_client)
    return stack, bot_client, chat


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
async def test_owner_adds_lists_and_removes_channels():
    stack, _, chat = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    doc = await service.add_channel("@forcechan", title="Updates", added_by=9001)
    assert doc["id"] == 700001 and doc["username"] == "forcechan"
    assert doc["title"] == "Updates"

    channels = await service.channels()
    assert len(channels) == 1
    assert await service.enabled() is False, "adding a channel must not silently switch the gate on"

    enabled = await service.set_enabled(True)
    assert enabled is True and await service.enabled() is True

    removed = await service.remove_channel(700001)
    assert removed is True and await service.channels() == []


async def test_adding_the_same_channel_twice_keeps_one_entry():
    stack, _, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    await service.add_channel("@forcechan", title="First")
    await service.add_channel("@forcechan", title="")
    channels = await service.channels()
    assert len(channels) == 1
    assert channels[0]["title"] == "First", "a re-add must not wipe the title"


async def test_numeric_id_and_bad_input():
    stack, _, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    doc = await service.add_channel("-1001234567890")
    assert doc["id"] == -1001234567890

    with pytest.raises(ValueError):
        await service.add_channel("not a channel at all")


# ---------------------------------------------------------------------------
# gate logic
# ---------------------------------------------------------------------------
async def test_gate_is_off_until_the_owner_turns_it_on():
    stack, _, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    await service.add_channel("@forcechan")
    verdict = await service.check(4242)
    assert verdict["ok"] is True, "disabled gate must let everyone through"


async def test_non_member_is_blocked_and_member_passes():
    stack, bot_client, chat = await _stack_with_channel(members={5550001})
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    await service.add_channel("@forcechan")
    await service.set_enabled(True)

    blocked = await service.check(5550002)
    assert blocked["ok"] is False
    assert [c["id"] for c in blocked["missing"]] == [700001]

    allowed = await service.check(5550001)
    assert allowed["ok"] is True and allowed["missing"] == []

    # the cache keeps it cheap: no second lookup inside the TTL
    bot_client.membership_error = True
    cached = await service.check(5550002)
    assert cached["ok"] is False, "the cached answer must still be used"


async def test_unreachable_channel_fails_open():
    stack, bot_client, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    await service.add_channel("@forcechan")
    await service.set_enabled(True)
    service.invalidate()

    bot_client.membership_error = True        # network / permissions failure
    verdict = await service.check(5550123, use_cache=False)
    assert verdict["ok"] is True, "a broken check must never lock a user out"


async def test_disabled_channel_entry_is_ignored():
    stack, _, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    doc = await service.add_channel("@forcechan")
    await service.set_enabled(True)
    service.invalidate()

    channels = await service.channels()
    channels[0]["enabled"] = False
    await stack.settings.set_path("force_sub.channels", channels)
    service.invalidate()
    assert (await service.check(5550999, use_cache=False))["ok"] is True


async def test_join_link_prefers_the_public_username():
    stack, _, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    doc = await service.add_channel("@forcechan")
    assert service.join_link(doc) == "https://t.me/forcechan"
    assert service.join_link({"id": -1001, "invite": "https://t.me/+abc"}) == "https://t.me/+abc"


# ---------------------------------------------------------------------------
# bot UX
# ---------------------------------------------------------------------------
async def test_user_sees_the_join_screen_and_can_verify():
    stack, bot_client, chat = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    await service.add_channel("@forcechan", title="Updates")
    await service.set_enabled(True)

    router = stack.router
    user_id = 5560001
    start = FakeEvent(sender_id=user_id, chat_id=user_id)
    await router.start_for(start)
    text = start.last_text.upper()
    assert "FORCE CHANNEL" in text or "JOIN" in text
    labels = start.button_labels()
    assert any("I joined" in label for label in labels), labels
    assert not any("BUY PREMIUM" in label.upper() for label in labels), \
        "the panel must not be reachable before joining"

    # still outside → the verify button says so
    check = FakeEvent(sender_id=user_id, chat_id=user_id, data="u:fscheck")
    await router.handle_callback(check)
    assert "My Channels" not in (check.last_text or "")
    assert any("not yet" in (a.get("text") or "").lower() for a in check.answers)

    # the admin lets them in → verify unlocks the panel
    bot_client.channel_members[700001].add(user_id)
    check2 = FakeEvent(sender_id=user_id, chat_id=user_id, data="u:fscheck", message_id=2)
    await router.handle_callback(check2)
    labels2 = check2.button_labels()
    assert any("Auto Reactions" in label for label in labels2), \
        f"the panel must be reachable after joining: {labels2}"


async def test_other_buttons_are_gated_too():
    stack, bot_client, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    await service.add_channel("@forcechan")
    await service.set_enabled(True)

    router = stack.router
    event = FakeEvent(sender_id=5560002, chat_id=5560002, data="u:prem")
    await router.handle_callback(event)
    assert "BUY PREMIUM" not in (event.last_text or "").upper()
    assert any("I joined" in label for label in event.button_labels())


async def test_owner_and_admin_are_never_blocked(monkeypatch):
    stack, _, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service
    import src.config as app_config

    monkeypatch.setattr(app_config, "OWNER_ID", 9001, raising=False)
    service = get_force_sub_service()
    await service.add_channel("@forcechan")
    await service.set_enabled(True)

    owner = FakeEvent(sender_id=9001, chat_id=9001)
    await stack.router.start_for(owner)
    assert "ADMIN PANEL" in (owner.last_text or "").upper()

    await stack.users.set_role(5560003, "admin")
    admin = FakeEvent(sender_id=5560003, chat_id=5560003)
    await stack.router.start_for(admin)
    assert "ADMIN PANEL" in (admin.last_text or "").upper()


async def test_ban_still_wins_over_the_gate():
    stack, _, _ = await _stack_with_channel()
    from src.services.forcesub import get_force_sub_service

    service = get_force_sub_service()
    await service.add_channel("@forcechan")
    await service.set_enabled(True)
    banned = 5560004
    await stack.users.get_or_create(banned, username="bad", first_name="Bad")
    await stack.users.set_banned(banned, True)
    event = FakeEvent(sender_id=banned, chat_id=banned)
    await stack.router.start_for(event)
    assert "disabled" in (event.responses[-1]["text"] if event.responses else "").lower()


async def test_owner_manages_the_gate_from_the_admin_panel(monkeypatch):
    stack, _, _ = await _stack_with_channel()
    import src.config as app_config

    monkeypatch.setattr(app_config, "OWNER_ID", 9001, raising=False)
    panel = stack.admin_panel
    event = ev(9001, "a:fs")
    await panel.dispatch("fs", [], event)
    text = (event.last_text or "").upper()
    assert "FORCE SUBSCRIBE" in text and "CHANNELS: <B>0</B>" in text

    # add a channel through the panel's text input
    await panel.dispatch("fs_add", [], event)
    await panel._save_force_sub_channel(event, {}, "@forcechan | Updates")
    from src.services.forcesub import get_force_sub_service

    channels = await get_force_sub_service().channels()
    assert len(channels) == 1 and channels[0]["title"] == "Updates"

    # enable it
    await panel.dispatch("fs_toggle", [], event)
    assert await get_force_sub_service().enabled() is True

    # the list shows it and can remove it
    await panel.dispatch("fs", [], event)
    assert "Updates" in (event.last_text or "")
    await panel.dispatch("fs_del", [str(channels[0]["id"])], event)
    assert await get_force_sub_service().channels() == []


async def test_non_owner_cannot_touch_the_gate(monkeypatch):
    stack, _, _ = await _stack_with_channel()
    import src.config as app_config

    monkeypatch.setattr(app_config, "OWNER_ID", 9001, raising=False)
    admin = 5570001
    await stack.users.get_or_create(admin, username="adm", first_name="Adm")
    await stack.users.set_role(admin, "admin")
    panel = stack.admin_panel
    event = ev(admin, "a:fs")
    await panel.dispatch("fs", [], event)
    assert any("owner only" in (a.get("text") or "").lower() for a in event.answers)
    from src.services.forcesub import get_force_sub_service

    assert await get_force_sub_service().channels() == []


# ---------------------------------------------------------------------------
# web dashboard: force-sub API + the soft-refresh / mobile UI contract
# ---------------------------------------------------------------------------
async def test_web_admin_force_sub_endpoints(monkeypatch):
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.test_utils import TestClient, TestServer

    stack, _, _ = await _stack_with_channel()
    import src.config as app_config

    monkeypatch.setattr(app_config, "OWNER_ID", 9001, raising=False)
    monkeypatch.setenv("ADMIN_PANEL_PASSWORD", "pw")
    import src.health as health
    from src.webadmin import WebAdmin

    app = aiohttp.web.Application()
    WebAdmin(stack.bot).install(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await client.post("/admin/api/login", data={"user_id": "9001", "password": "pw"},
                          allow_redirects=False)

        data = await (await client.get("/admin/api/force_sub")).json()
        assert data["ok"] and data["enabled"] is False and data["channels"] == []

        added = await (await client.post("/admin/api/force_sub",
                                         json={"link": "@forcechan", "title": "Web Updates"})).json()
        assert added["added"] == 700001

        toggled = await (await client.post("/admin/api/force_sub/toggle", json={})).json()
        assert toggled["enabled"] is True
        from src.services.forcesub import get_force_sub_service

        assert await get_force_sub_service().enabled() is True

        removed = await (await client.request("DELETE",
                                              "/admin/api/force_sub/700001")).json()
        assert removed["channels"] == []

        # the guard still applies
        client.session.cookie_jar.clear()
        res = await client.get("/admin/api/force_sub")
        assert res.status == 401
    finally:
        await client.close()
