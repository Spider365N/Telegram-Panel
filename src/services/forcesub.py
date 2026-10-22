"""Force-subscribe — a join gate before the bot answers.

The operator picks one or more channels; until a user is a member of **all** of
them (and the feature is enabled) the bot shows a join screen instead of the
panel. Owners/admins are never blocked, and a check that cannot be performed
(bot kicked, channel deleted, network hiccup) is treated as *allowed* so a
misconfigured channel can never lock every user out of the bot.

Storage lives in the ``settings`` collection (section ``force_sub``) so the bot
panel, the web dashboard and the rest of the services all read the same list.

    {"enabled": false, "cache_seconds": 60, "channels": [
        {"id": -1001234567890, "username": "mychannel", "invite": null,
         "title": "Updates", "enabled": true, "added_by": 123, "added_at": "..."}
    ]}
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _internal_id(raw: int) -> int:
    """``-1001234`` → ``1234`` (the id Telethon uses inside PeerChannel)."""
    text = str(int(raw))
    return int(text[4:]) if text.startswith("-100") else abs(int(raw))


class ForceSubService:
    """Add/remove the gate channels and answer \"is this user in them?\"."""

    def __init__(self, settings=None, bot=None):
        self._settings = settings
        self._bot = bot
        self._cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}

    # -- dependencies ------------------------------------------------------
    @property
    def settings(self):
        if self._settings is None:
            from src.services.settings import get_settings_service

            self._settings = get_settings_service()
        return self._settings

    @property
    def bot(self):
        if self._bot is None:
            try:
                from src.runtime import get_bot

                self._bot = get_bot()
            except Exception:  # noqa: BLE001 - the service stays usable without a bot
                self._bot = None
        return self._bot

    @property
    def client(self):
        bot = self.bot
        return getattr(bot, "tbot", None) if bot is not None else None

    # -- configuration -----------------------------------------------------
    async def config(self) -> Dict[str, Any]:
        cfg = await self.settings.section("force_sub")
        return {
            "enabled": bool(cfg.get("enabled", False)),
            "cache_seconds": int(cfg.get("cache_seconds", 60) or 0),
            "title": str(cfg.get("title") or "📢 Join our channels to continue"),
            "message": str(
                cfg.get("message")
                or "To use this bot you must join the channel(s) below first.\n"
                   "Join, then tap <b>✅ I joined — verify</b>."
            ),
            "channels": [dict(c) for c in (cfg.get("channels") or []) if isinstance(c, dict)],
        }

    async def channels(self) -> List[Dict[str, Any]]:
        return (await self.config())["channels"]

    async def enabled(self) -> bool:
        return (await self.config())["enabled"]

    async def set_enabled(self, enabled: bool) -> bool:
        await self.settings.set_path("force_sub.enabled", bool(enabled))
        self._cache.clear()
        logger.info(f"Force subscribe {'enabled' if enabled else 'disabled'}")
        return bool(enabled)

    async def toggle(self) -> bool:
        return await self.set_enabled(not await self.enabled())

    # -- managing channels -------------------------------------------------
    async def add_channel(self, link: str, *, title: str = "", added_by: int = 0) -> Dict[str, Any]:
        """Resolve a link/username/id and store it. Raises ``ValueError`` with a
        human message when the channel cannot be reached."""
        raw = str(link or "").strip()
        if not raw:
            raise ValueError("Send a channel link, @username or -100… id")
        resolved = await self._resolve(raw)
        channels = await self.channels()
        for existing in channels:
            if int(existing.get("id") or 0) == int(resolved["id"]):
                # refresh the identifiers, never overwrite a title the owner set
                for key in ("username", "invite"):
                    if resolved.get(key):
                        existing[key] = resolved[key]
                existing["title"] = (title or existing.get("title")
                                     or resolved.get("title") or str(resolved["id"]))
                existing["enabled"] = True
                await self.settings.set_path("force_sub.channels", channels)
                self._cache.clear()
                return existing
        doc = {
            "id": int(resolved["id"]),
            "username": resolved.get("username") or "",
            "invite": resolved.get("invite") or "",
            "title": title or resolved.get("title") or str(resolved["id"]),
            "enabled": True,
            "added_by": int(added_by or 0),
            "added_at": _now_iso(),
        }
        channels.append(doc)
        await self.settings.set_path("force_sub.channels", channels)
        self._cache.clear()
        logger.info(f"Force-subscribe channel added: {doc['title']} ({doc['id']})")
        return doc

    async def remove_channel(self, channel_id: Any) -> bool:
        target = str(channel_id)
        channels = await self.channels()
        kept = []
        removed = False
        for doc in channels:
            if str(doc.get("id")) == target or str(doc.get("username") or "") == target.lstrip("@"):
                removed = True
                continue
            kept.append(doc)
        if removed:
            await self.settings.set_path("force_sub.channels", kept)
            self._cache.clear()
            logger.info(f"Force-subscribe channel removed: {channel_id}")
        return removed

    async def clear(self) -> None:
        await self.settings.set_path("force_sub.channels", [])
        self._cache.clear()

    # -- resolution --------------------------------------------------------
    async def _resolve(self, raw: str) -> Dict[str, Any]:
        client = self.client
        numeric = re.fullmatch(r"-?\d{5,}", raw)
        invite = re.search(r"(?:t(?:elegram)?\.me|telegram\.me|tg://join\?invite=)"
                           r"(?:/|joinchat/|\+)?([A-Za-z0-9_-]{6,})", raw)
        username = None
        if raw.startswith("@") and len(raw) > 1:
            username = raw[1:]
        else:
            match = re.search(r"(?:t(?:elegram)?\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,})", raw)
            if match:
                username = match.group(1)

        if numeric:
            channel_id = int(raw)
            doc: Dict[str, Any] = {"id": channel_id, "title": "", "username": "", "invite": ""}
            if client is not None:
                try:
                    entity = await self._entity_by_id(channel_id)
                    if entity is not None:
                        doc["title"] = getattr(entity, "title", "") or ""
                        doc["username"] = getattr(entity, "username", "") or ""
                except Exception as exc:  # noqa: BLE001 - id is still usable
                    logger.debug(f"force-sub: id {channel_id} title lookup failed: {exc}")
            return doc

        if invite and not username:
            if client is None:
                raise ValueError("The bot is not connected — try again in a moment")
            try:
                from telethon.tl.functions.messages import CheckChatInviteRequest
                from telethon.tl.types import ChatInviteAlready

                result = await client(CheckChatInviteRequest(invite.group(1)))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"That invite link cannot be read: {exc}") from exc
            chat = getattr(result, "chat", None)
            if not isinstance(result, ChatInviteAlready) or chat is None:
                raise ValueError(
                    "I am not a member of that private channel yet. Add the bot to the "
                    "channel first (any member rights are enough), then add the link again."
                )
            return {
                "id": int(getattr(chat, "id", 0) or 0),
                "title": getattr(chat, "title", "") or "",
                "username": getattr(chat, "username", "") or "",
                "invite": raw if "t.me" not in raw else raw,
            }

        if username:
            if client is None:
                raise ValueError("The bot is not connected — try again in a moment")
            try:
                entity = await client.get_entity(username)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"@{username} not found (or the bot cannot see it): {exc}"
                ) from exc
            return {
                "id": int(getattr(entity, "id", 0) or 0),
                "title": getattr(entity, "title", "") or "",
                "username": getattr(entity, "username", username) or username,
                "invite": "",
            }

        raise ValueError("Send @username, a t.me link or a -100… channel id")

    async def _entity_by_id(self, channel_id: int):
        client = self.client
        if client is None:
            return None
        try:
            return await client.get_entity(int(channel_id))
        except Exception:
            pass
        try:
            from telethon.tl.types import PeerChannel

            return await client.get_entity(PeerChannel(_internal_id(channel_id)))
        except Exception:
            return None

    async def _entity(self, doc: Dict[str, Any]):
        client = self.client
        if client is None:
            return None
        username = str(doc.get("username") or "").strip().lstrip("@")
        if username:
            try:
                return await client.get_entity(username)
            except Exception as exc:  # noqa: BLE001 - fall through to the id
                logger.debug(f"force-sub: @{username} lookup failed: {exc}")
        invite = str(doc.get("invite") or "")
        match = re.search(r"(?:joinchat/|\+)([A-Za-z0-9_-]{6,})", invite)
        if match:
            try:
                from telethon.tl.functions.messages import CheckChatInviteRequest
                from telethon.tl.types import ChatInviteAlready

                result = await client(CheckChatInviteRequest(match.group(1)))
                if isinstance(result, ChatInviteAlready):
                    return result.chat
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"force-sub: invite check failed: {exc}")
        return await self._entity_by_id(int(doc.get("id") or 0))

    # -- membership --------------------------------------------------------
    async def is_member(self, doc: Dict[str, Any], user_id: int) -> int:
        """1 = member, 0 = not a member, -1 = cannot tell (fail open)."""
        client = self.client
        if client is None:
            return -1
        entity = await self._entity(doc)
        if entity is None:
            logger.warning(
                f"force-sub: cannot resolve channel {doc.get('id')} — check skipped "
                f"(the bot must be a member of it)"
            )
            return -1
        try:
            from telethon.errors import (ChannelPrivateError, UserBannedInChannelError,
                                         UserNotParticipantError)

            outcome = client.get_permissions(entity, int(user_id))
            if hasattr(outcome, "__await__"):
                outcome = await outcome
            return 1 if outcome is not None else 0
        except Exception as exc:  # noqa: BLE001 - classify, never raise
            name = type(exc).__name__
            if name in ("UserNotParticipantError", "ChannelPrivateError",
                        "UserBannedInChannelError"):
                return 0
            logger.debug(f"force-sub: membership check failed for {user_id}: {exc}")
            return -1

    async def check(self, user_id: int, *, use_cache: bool = True) -> Dict[str, Any]:
        """Do we let this user through? ``{ok, missing: [...], enabled}``."""
        cfg = await self.config()
        if not cfg["enabled"] or not cfg["channels"]:
            return {"ok": True, "missing": [], "enabled": cfg["enabled"], "channels": []}
        user_id = int(user_id)
        ttl = cfg["cache_seconds"]
        cached = self._cache.get(user_id)
        if use_cache and cached and ttl and time.time() - cached[0] < ttl:
            return cached[1]
        missing: List[Dict[str, Any]] = []
        for doc in cfg["channels"]:
            if not doc.get("enabled", True):
                continue
            verdict = await self.is_member(doc, user_id)
            if verdict == 0:
                missing.append(doc)
        outcome = {"ok": not missing, "missing": missing,
                   "enabled": True, "channels": cfg["channels"]}
        self._cache[user_id] = (time.time(), outcome)
        return outcome

    def invalidate(self, user_id: Optional[int] = None) -> None:
        if user_id is None:
            self._cache.clear()
        else:
            self._cache.pop(int(user_id), None)

    # -- UI helpers --------------------------------------------------------
    @staticmethod
    def join_link(doc: Dict[str, Any]) -> str:
        username = str(doc.get("username") or "").strip().lstrip("@")
        if username:
            return f"https://t.me/{username}"
        invite = str(doc.get("invite") or "").strip()
        if invite:
            return invite if invite.startswith(("http://", "https://")) else f"https://{invite}"
        return ""


_force_sub: Optional[ForceSubService] = None


def get_force_sub_service(settings=None, bot=None) -> ForceSubService:
    global _force_sub
    if _force_sub is None or settings is not None or bot is not None:
        _force_sub = ForceSubService(settings or (_force_sub._settings if _force_sub else None),
                                     bot or (_force_sub._bot if _force_sub else None))
    return _force_sub


def reset_force_sub_service() -> None:
    global _force_sub
    _force_sub = None
