"""
Panel router: dispatch for the user panel and the admin panel.

Everything the panels do flows through here:

* callback data ``u:<action>[:args]``  → user panel
* callback data ``a:<action>[:args]``  → admin panel (access checked)
* plain text while an input state is active (link, amount, UTR, price …)

The router also remembers the panel message per chat so navigation edits the
same message instead of flooding the conversation (matching the edit-in-place
behaviour the panel already had).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from telethon import Button

from src.panels.common import (
    ADMIN_PREFIX,
    USER_PREFIX,
    CallbackCache,
    Screen,
)
from src.services.users import get_user_service

logger = logging.getLogger(__name__)

INPUT_TTL = 900  # seconds an input state stays valid


class PanelRouter:
    def __init__(self, tbot=None, users=None):
        self.tbot = tbot
        self._users_service = users
        self._inputs: Dict[int, Dict[str, Any]] = {}
        self._messages: Dict[int, int] = {}
        self._cache = CallbackCache()
        self._user_panel = None
        self._admin_panel = None

    # ------------------------------------------------------------------
    @property
    def users(self):
        if self._users_service is None:
            self._users_service = get_user_service()
        return self._users_service

    @property
    def user_panel(self):
        if self._user_panel is None:
            from src.panels.user_panel import UserPanel
            self._user_panel = UserPanel(self)
        return self._user_panel

    @property
    def admin_panel(self):
        if self._admin_panel is None:
            from src.panels.admin_panel import AdminPanel
            self._admin_panel = AdminPanel(self)
        return self._admin_panel

    # ------------------------------------------------------------------
    # rendering
    # ------------------------------------------------------------------
    async def render(self, event, screen: Screen, *, edit: bool = True) -> None:
        """Show a Screen, editing the current message when possible."""
        if event is None:
            return
        kwargs: Dict[str, Any] = {"buttons": screen.buttons or None, "parse_mode": screen.parse_mode}
        chat_id = getattr(event, "chat_id", None)
        try:
            if hasattr(event, "answer"):
                try:
                    await event.answer()
                except Exception as exc:
                    logger.debug(f"callback answer failed: {exc}")
            if edit:
                try:
                    message = await event.edit(screen.text, **kwargs)
                    if message is not None and chat_id is not None:
                        self._messages[chat_id] = getattr(message, "id", None) or self._messages.get(chat_id)
                    return
                except Exception as exc:
                    logger.debug(f"edit failed ({exc}); sending new message")
            message = await event.respond(screen.text, **kwargs)
            if message is not None and chat_id is not None:
                self._messages[chat_id] = getattr(message, "id", None)
        except Exception as exc:
            logger.error(f"Panel render failed: {exc}", exc_info=True)
            try:
                await event.respond(screen.text, buttons=screen.buttons or None)
            except Exception:
                pass

    async def answer(self, event, text: str = "", alert: bool = False) -> None:
        try:
            if hasattr(event, "answer"):
                await event.answer(text or None, alert=alert)
            elif text:
                await event.respond(text)
        except Exception as exc:
            logger.debug(f"answer failed: {exc}")

    async def toast(self, event, text: str) -> None:
        await self.answer(event, text, alert=False)

    # ------------------------------------------------------------------
    # input states
    # ------------------------------------------------------------------
    async def set_input(self, chat_id: int, action: str, data: Dict[str, Any]) -> None:
        self._inputs[int(chat_id)] = {"action": action, "data": data or {}, "ts": __import__("time").time()}

    async def get_input(self, chat_id: int) -> Dict[str, Any]:
        state = self._inputs.get(int(chat_id))
        if not state:
            return {}
        import time
        if time.time() - state.get("ts", 0) > INPUT_TTL:
            self._inputs.pop(int(chat_id), None)
            return {}
        return state

    async def clear_input(self, chat_id: int) -> None:
        self._inputs.pop(int(chat_id), None)

    def has_input(self, chat_id: int) -> bool:
        return int(chat_id) in self._inputs

    # ------------------------------------------------------------------
    # /start and menu entry points
    # ------------------------------------------------------------------
    async def start_for(self, event) -> None:
        """Route /start: admins land in the admin panel, users in the user panel."""
        await self.clear_input(getattr(event, "chat_id", 0) or 0)
        user_id = int(getattr(event, "sender_id", 0) or 0)
        await self.users.get_or_create(
            user_id,
            getattr(getattr(event, "sender", None), "username", None),
            getattr(getattr(event, "sender", None), "first_name", None),
        )
        if await self.users.is_banned(user_id):
            await event.respond("🚫 Your access to this bot has been disabled. Contact support.")
            return
        if await self.users.is_admin(user_id):
            await self.admin_panel.show_main(event)
        else:
            if not await self.user_panel.force_sub_gate(event):
                return
            await self.user_panel.show_home(event, welcome=True)

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------
    @staticmethod
    def is_panel_callback(data: str) -> bool:
        return bool(data) and (data.startswith(USER_PREFIX) or data.startswith(ADMIN_PREFIX))

    async def handle_callback(self, event, data: Optional[str] = None) -> bool:
        data = data if data is not None else (getattr(event, "data", b"") or b"").decode("utf-8", "ignore")
        if not self.is_panel_callback(data):
            return False
        user_id = int(getattr(event, "sender_id", 0) or 0)
        if self._cache.seen(f"{user_id}:{data}:{getattr(event, 'message_id', '')}"):
            logger.debug(f"Duplicate callback ignored: {data}")
            return True
        parts = data.split(":")
        prefix, action, args = parts[0], parts[1] if len(parts) > 1 else "", parts[2:]
        try:
            if await self.users.is_banned(user_id) and not await self.users.is_admin(user_id):
                await self.answer(event, "🚫 Access disabled", alert=True)
                return True
            if prefix == "a":
                if not await self.users.is_admin(user_id):
                    logger.warning(f"Non-admin {user_id} tried admin callback {data}")
                    await self.answer(event, "⛔ Admin only", alert=True)
                    return True
                await self.admin_panel.dispatch(action, args, event)
                return True
            # the join gate runs before every user-panel action (cached, cheap);
            # ``fscheck`` must always work or the user could never get in.
            if action != "fscheck":
                if not await self.user_panel.force_sub_gate(event):
                    return True
            await self.user_panel.dispatch(action, args, event)
            return True
        except Exception as exc:
            logger.error(f"Panel callback {data} failed: {exc}", exc_info=True)
            try:
                await self.answer(event, "⚠️ Something went wrong. Please try again.", alert=True)
            except Exception:
                pass
            return True

    # ------------------------------------------------------------------
    # text input
    # ------------------------------------------------------------------
    async def handle_message(self, event) -> bool:
        """Consume a text message belonging to an active panel input."""
        chat_id = getattr(event, "chat_id", None)
        if chat_id is None:
            return False
        text = (getattr(event, "raw_text", None) or getattr(event, "text", "") or "").strip()
        if text.startswith("/"):
            await self.clear_input(chat_id)  # a command aborts the flow
            return False
        state = await self.get_input(chat_id)
        if not state:
            return False

        action = state.get("action", "")
        data = state.get("data") or {}
        panel_name = data.get("panel", "user")
        await self.clear_input(chat_id)
        try:
            if panel_name == "admin":
                if not await self.users.is_admin(int(getattr(event, "sender_id", 0) or 0)):
                    return False
                await self.admin_panel.handle_input(action, text, event, data)
            else:
                await self.user_panel.handle_input(action, text, event, data)
            return True
        except ValueError as exc:
            await event.respond(f"⚠️ {exc}")
            return True
        except Exception as exc:
            logger.error(f"Panel input {action} failed: {exc}", exc_info=True)
            await event.respond("⚠️ Could not process that. Please try again or send /start.")
            return True

    # ------------------------------------------------------------------
    async def request_input(self, event, *, panel: str, action: str, prompt: str,
                            data: Optional[Dict[str, Any]] = None,
                            buttons: Optional[List[List[Any]]] = None,
                            cancel_to: Optional[str] = None) -> None:
        """Prompt for text input with a Cancel button."""
        chat_id = getattr(event, "chat_id", None)
        await self.set_input(chat_id, action, {"panel": panel, **(data or {})})
        rows = list(buttons or [])
        cancel_action = cancel_to or (USER_PREFIX if panel == "user" else ADMIN_PREFIX) + "cancel"
        rows.append([Button.inline("❌ Cancel", cancel_action, style="danger")])
        await self.render(event, Screen(prompt, rows))

    async def cancel_input(self, event) -> None:
        await self.clear_input(getattr(event, "chat_id", 0) or 0)
        await self.answer(event, "Cancelled")


_router: Optional[PanelRouter] = None


def get_panel_router(tbot=None) -> PanelRouter:
    global _router
    if _router is None or tbot is not None:
        _router = PanelRouter(tbot)
    return _router


def reset_panel_router() -> None:
    global _router
    _router = None
