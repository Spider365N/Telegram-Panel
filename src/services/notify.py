"""
Outbound notifications (admin alerts, user updates, broadcasts).

The notifier never raises: a blocked bot or a deleted account must not break the
order pipeline. Every send is logged.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from src import config as app_config

logger = logging.getLogger(__name__)


class Notifier:
    """Thin wrapper around a Telethon client used for bot messages."""

    def __init__(self, client=None):
        self._client = client
        self.failures: List[str] = []
        # Users the bot cannot message (never pressed /start). Remembered so the
        # log gets one clear line instead of a warning per notification attempt.
        self.unreachable: set = set()

    def bind(self, client) -> "Notifier":
        self._client = client
        return self

    @property
    def client(self):
        if self._client is None:
            try:
                from src.runtime import get_bot
                bot = get_bot()
                self._client = getattr(bot, "tbot", None)
            except Exception:
                self._client = None
        return self._client

    async def send(self, chat_id: int, text: str, buttons=None, parse_mode: Optional[str] = None) -> bool:
        client = self.client
        if client is None:
            logger.debug("Notifier has no client bound; dropping message")
            return False
        try:
            from src.utils import detect_parse_mode

            kwargs: Dict[str, Any] = {}
            if buttons:
                kwargs["buttons"] = buttons
            # Without this Telegram shows "</b>" as literal text — the operator
            # reported exactly that on the alert and startup messages.
            mode = detect_parse_mode(text, parse_mode)
            if mode:
                kwargs["parse_mode"] = mode
            await client.send_message(int(chat_id), text, **kwargs)
            return True
        except Exception as exc:
            self.failures.append(f"{chat_id}: {exc}")
            lowered = str(exc).lower()
            unreachable = (
                "cannot start conversations" in lowered
                or "invalid peer" in lowered
                or "peer_id_invalid" in lowered
                or "user is deleted" in lowered
            )
            if unreachable:
                if chat_id not in self.unreachable:
                    self.unreachable.add(chat_id)
                    logger.info(
                        f"Cannot message {chat_id} yet — that user has to open the bot "
                        f"and press /start once. Notifications for them are skipped."
                    )
                else:
                    logger.debug(f"Skipping notification to unreachable user {chat_id}")
            else:
                logger.warning(f"Could not send notification to {chat_id}: {exc}")
            return False

    async def to_admins(self, text: str, buttons=None) -> int:
        """Send to the owner + every configured admin. Returns delivery count."""
        delivered = 0
        owner = int(getattr(app_config, "OWNER_ID", 0) or 0)
        admins = {int(a) for a in getattr(app_config, "ADMIN_IDS", [])}
        for admin_id in sorted({owner, *admins} - {0}):
            if await self.send(admin_id, text, buttons):
                delivered += 1
        return delivered

    async def to_owner(self, text: str, buttons=None) -> bool:
        owner = int(getattr(app_config, "OWNER_ID", 0) or 0)
        if not owner:
            return False
        return await self.send(owner, text, buttons)

    async def to_user(self, user_id: int, text: str, buttons=None) -> bool:
        return await self.send(int(user_id), text, buttons)

    async def broadcast(self, user_ids: Iterable[int], text: str, buttons=None,
                        delay: float = 0.05) -> Dict[str, int]:
        """Send a message to many users, tolerating failures."""
        sent = failed = 0
        for user_id in user_ids:
            if await self.send(int(user_id), text, buttons):
                sent += 1
            else:
                failed += 1
            if delay:
                await asyncio.sleep(delay)
        return {"sent": sent, "failed": failed}

    def clear_failures(self) -> None:
        self.failures.clear()

    def forget_unreachable(self) -> None:
        """Allow one fresh attempt per user (used when an account starts the bot)."""
        self.unreachable.clear()


_notifier: Optional[Notifier] = None


def get_notifier(client=None) -> Notifier:
    global _notifier
    if _notifier is None or client is not None:
        _notifier = Notifier(client)
    return _notifier


def reset_notifier() -> None:
    global _notifier
    _notifier = None
