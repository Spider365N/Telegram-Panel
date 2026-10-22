"""
Panel polish: message formatting, edit-in-place navigation, and the promise
that the searchable text never contains a broken HTML entity or a missing
space (``<b>10</b>accounts``), which the operator reported.

Every user-panel and admin-panel screen is rendered here against the fakes and
checked for:

* a real payload (not empty, not "None"),
* balanced ``<b>`` / ``<i>`` / ``<code>`` tags,
* no ``</b>word`` / ``word<b>`` glitches,
* Telegram's 4096 character limit,
* clickable buttons on every screen except pure confirmations.

Navigation then has to edit the same message instead of posting a new one.
"""
from __future__ import annotations

import re

import pytest

from tests.fakes import FakeEvent, build_stack, make_channel

OWNER = 123456789
USER = 4242

TAG_RE = re.compile(r"</?(b|i|u|s|code|pre|a)(?:\s[^>]*)?>", re.IGNORECASE)
# ``</b>accounts`` / ``to<b>10</b>`` — the class of typo the operator reported.
GLUE_RE = re.compile(r"</(?:b|i|code|u|s|pre)>[A-Za-z0-9]|[A-Za-z0-9]<(?:b|i|code|u|s|pre)>",
                     re.IGNORECASE)


async def panel_stack():
    from tests.fakes import FakeTelegramClient

    channel = make_channel(555001)
    clients = {"phone1": FakeTelegramClient("phone1", user_id=5001,
                                            channels={555001: channel})}
    stack = await build_stack(clients)
    await stack.registry.sync_from_live_clients()
    await stack.channels.register(-100555001, USER, title="My Test Channel")
    await stack.users.credit(USER, 500, "topup")
    return stack


def assert_screen_text(text: str, where: str) -> None:
    assert text and text.strip(), f"{where}: empty screen"
    assert "None" not in text, f"{where}: rendered a Python None"
    assert len(text) < 4096, f"{where}: {len(text)} chars — Telegram would refuse it"
    for tag in TAG_RE.findall(text):
        assert text.lower().count(f"<{tag.lower()}>") == text.lower().count(f"</{tag.lower()}>"), \
            f"{where}: unbalanced <{tag}>"
    assert not re.search(r"</?b>\s*None|None\s*</?b>", text), f"{where}: None in bold"
    assert not GLUE_RE.search(text), (
        f"{where}: <b>…</b> is glued to a word (missing space): "
        f"{GLUE_RE.search(text).group(0)!r}"
    )


USER_SCREENS = [
    ("u:home", "home"),
    ("u:or", "order reactions"),
    ("u:ar", "auto reactions"),
    ("u:ar_ch:-100555001", "auto reactions channel"),
    ("u:ov", "order views"),
    ("u:av", "auto views"),
    ("u:av_ch:-100555001", "auto views channel"),
    ("u:mb", "members"),
    ("u:ai", "account info"),
    ("u:wallet", "wallet"),
    ("u:om", "orders"),
    ("u:ch", "my channels"),
    ("u:addch", "add to channel"),
    ("u:addgr", "add to group"),
    ("u:prem", "premium"),
    ("u:sp", "support"),
    ("u:cm", "comments"),
    ("u:reqplan:views", "plan request"),
    ("u:topup", "topup prompt"),
]


@pytest.mark.asyncio
class TestEveryScreenRenders:
    @pytest.mark.parametrize("data,label", USER_SCREENS)
    async def test_user_screen(self, data, label):
        stack = await panel_stack()
        event = FakeEvent(sender_id=USER, chat_id=USER, data=data)
        await stack.router.handle_callback(event)
        assert_screen_text(event.last_text, label)
        assert event.last_buttons, f"{label}: no buttons to navigate with"

    async def test_owner_screen(self):
        stack = await panel_stack()
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, data="a:home")
        await stack.router.handle_callback(event)
        assert_screen_text(event.last_text, "admin home")
        assert "Send PV" in " | ".join(event.button_labels())

    @pytest.mark.parametrize("data,label", [
        ("a:pricing", "pricing"),
        ("a:limits", "limits"),
        ("a:users", "users"),
        ("a:channels", "channels"),
        ("a:accounts", "accounts"),
        ("a:emojis", "emojis"),
        ("a:settings", "settings"),
        ("a:topups", "topups"),
        ("a:orders", "orders"),
    ])
    async def test_admin_screen(self, data, label):
        stack = await panel_stack()
        event = FakeEvent(sender_id=OWNER, chat_id=OWNER, data=data)
        await stack.router.handle_callback(event)
        assert_screen_text(event.last_text, f"admin {label}")
        assert event.last_buttons, f"admin {label}: no buttons"

    async def test_plan_grant_screens(self):
        stack = await panel_stack()
        for data in ("a:user:4242", "a:user_prem:4242", "a:user_prem:4242:views",
                     "a:user_prem:4242:unlimited"):
            event = FakeEvent(sender_id=OWNER, chat_id=OWNER, data=data)
            await stack.router.handle_callback(event)
            assert_screen_text(event.last_text, data)
            assert event.last_buttons, f"{data}: no buttons"

    async def test_premium_lock_screens_for_a_free_user(self):
        stack = await panel_stack()
        for data in ("u:ov", "u:av", "u:cm", "u:mb"):
            event = FakeEvent(sender_id=USER, chat_id=USER, data=data)
            await stack.router.handle_callback(event)
            assert "premium" in event.last_text.lower()
            assert_screen_text(event.last_text, f"lock:{data}")


@pytest.mark.asyncio
class TestEditInPlace:
    async def test_navigation_edits_the_same_message(self):
        stack = await panel_stack()
        # Each tap renders into the *same* message; none of them posts a new one
        # (the duplicate-tap guard ignores the exact same callback twice, so the
        # route below uses a different action each step).
        for data in ("u:or", "u:ch", "u:ar", "u:ar_ch:-100555001", "u:home",
                     "u:wallet", "u:ai"):
            event = FakeEvent(sender_id=USER, chat_id=USER, data=data, message_id=77)
            await stack.router.handle_callback(event)
            assert event.edits, f"{data} did not edit the panel message"
            assert not event.responses, f"{data} posted a new message"

    async def test_text_prompts_edit_too(self):
        stack = await panel_stack()
        await stack.router.handle_callback(
            FakeEvent(sender_id=USER, chat_id=USER, data="u:topup"))
        event = FakeEvent(sender_id=USER, chat_id=USER, text="500")
        await stack.router.handle_message(event)
        assert event.edits and "UTR" in event.edits[-1]["text"]

    async def test_back_button_returns_to_the_same_message(self):
        stack = await panel_stack()
        first = FakeEvent(sender_id=USER, chat_id=USER, data="u:ch")
        await stack.router.handle_callback(first)
        back = FakeEvent(sender_id=USER, chat_id=USER, data="u:home")
        await stack.router.handle_callback(back)
        assert back.edits and "TELEGRAM PANEL" in back.edits[-1]["text"]


@pytest.mark.asyncio
class TestCopyQuality:
    async def test_free_tier_is_advertised_as_free(self):
        stack = await panel_stack()
        event = FakeEvent(sender_id=USER, chat_id=USER, data="u:home")
        await stack.router.handle_callback(event)
        assert "Free" in event.last_text

    async def test_premium_services_are_marked_in_the_menu(self):
        stack = await panel_stack()
        event = FakeEvent(sender_id=USER, chat_id=USER, data="u:home")
        await stack.router.handle_callback(event)
        labels = " | ".join(event.button_labels())
        for premium_label in ("Auto Views", "Order Views", "Comments", "Members"):
            assert premium_label in labels
        assert "👑" in labels

    async def test_source_has_no_glued_html(self):
        """Static guard so a future edit cannot ship ``</b>word`` again."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "src"
        bad = []
        for path in list(root.rglob("*.py")):
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                # the literal escape ``\n`` inside a source string is not a glue
                probe = line.replace("\\n", " ").replace("''", " ")
                if GLUE_RE.search(probe) and "GLUE_RE" not in line:
                    bad.append(f"{path.name}:{number}: {line.strip()[:80]}")
        assert not bad, bad
