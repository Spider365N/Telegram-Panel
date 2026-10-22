"""
Robust peer / entity resolution for Telegram clients.

Why this module exists
----------------------
Telethon can only talk to a channel it already knows about. A link like
``https://t.me/c/800000001/22`` only gives us an id, and calling
``client.get_entity(-100800000001)`` raises:

    Could not find the input entity for PeerChannel(channel_id=800000001)

...because the *access hash* for that channel was never cached in the session
(the account had never seen the channel). This module fixes that for good:

1. An ``access_hash`` cache (in-memory + MongoDB) is kept per account.
2. Access hashes are learned from every entity the client touches
   (``get_dialogs``, ``ResolveUsername``, ``GetChannels``, joins).
3. Resolution walks a ladder of strategies: cache → ``GetChannels`` →
   username resolve → dialog refresh → join (public or private invite) → retry.
4. Any channel/group the account ends up in is cached, so later operations
   (reaction, comment, view) work instantly.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import GetChannelsRequest, JoinChannelRequest
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    GetDialogsRequest,
    ImportChatInviteRequest,
)
from telethon.tl.functions.contacts import ResolveUsernameRequest
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInvite,
    ChatInviteAlready,
    ChatInvitePeek,
    InputChannel,
    InputPeerChannel,
    PeerChannel,
    User,
)

logger = logging.getLogger(__name__)

_LINK_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/"
    r"(?P<rest>[^?\s]+)",
    re.IGNORECASE,
)
_INVITE_RE = re.compile(r"^(?:\+|joinchat/)(?P<hash>[A-Za-z0-9_-]+)$")
# t.me/c/<internal_id>/<msg_id> — the internal id is what we prefix with -100
_PRIVATE_RE = re.compile(r"^c/(?P<chat>\d+)(?:/\d+)?(?:/(?P<msg>\d+))?$")


@dataclass
class LinkInfo:
    """Everything we could squeeze out of a Telegram link."""

    raw: str
    kind: str = "unknown"          # public | private | invite | unknown
    username: Optional[str] = None
    chat_id: Optional[int] = None  # -100... form
    invite_hash: Optional[str] = None
    message_id: Optional[int] = None
    topic_id: Optional[int] = None
    extra: Dict[str, Any] = field(default_factory=dict)


def parse_link(link: str) -> LinkInfo:
    """Parse a Telegram link without touching the network."""
    info = LinkInfo(raw=(link or "").strip())
    if not info.raw:
        return info

    text = info.raw.strip()
    # Bare invite links: +abc123 / joinchat/abc123
    if text.startswith("+") or text.startswith("joinchat/"):
        match = _INVITE_RE.match(text)
        if match:
            info.kind = "invite"
            info.invite_hash = match.group("hash")
            return info

    # Bare numeric id (-100800000002 or 800000002)
    if re.fullmatch(r"-?\d+", text):
        value = int(text)
        if value > 0:
            value = int(f"-100{value}")
        info.kind = "private"
        info.chat_id = value
        return info

    match = _LINK_RE.match(text)
    if not match:
        # Maybe a raw username
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", text):
            info.kind = "public"
            info.username = text
        return info

    rest = match.group("rest").strip("/")
    segments = [s for s in rest.split("/") if s]

    # Private: t.me/c/<id>/<topic?>/<msg?>
    if segments and segments[0].lower() == "c" and len(segments) >= 2 and segments[1].isdigit():
        info.kind = "private"
        info.chat_id = int(f"-100{segments[1]}")
        nums = [int(s) for s in segments[2:] if s.isdigit()]
        if len(nums) >= 2:
            info.topic_id, info.message_id = nums[0], nums[-1]
        elif nums:
            info.message_id = nums[0]
        return info

    # Invite: t.me/+hash or t.me/joinchat/hash
    if segments and (segments[0].startswith("+") or segments[0].lower() == "joinchat"):
        info.kind = "invite"
        info.invite_hash = segments[-1].lstrip("+")
        return info

    # Public: t.me/username/<topic?>/<msg?>
    if segments:
        info.kind = "public"
        info.username = segments[0]
        nums = [int(s) for s in segments[1:] if s.isdigit()]
        if len(nums) >= 2:
            info.topic_id, info.message_id = nums[0], nums[-1]
        elif nums:
            info.message_id = nums[0]
    return info


def is_private_link(link: str) -> bool:
    return parse_link(link).kind == "private"


class PeerCache:
    """access_hash cache. In-memory first, MongoDB-backed when available."""

    def __init__(self, store=None):
        self._store = store
        self._memory: Dict[str, Dict[int, int]] = {}
        self._username_memory: Dict[str, Dict[str, int]] = {}
        self._lock = asyncio.Lock()

    def _key(self, client) -> str:
        try:
            from src.utils import get_session_name
            return get_session_name(client) or "unknown"
        except Exception:
            return getattr(client, "session", None) and getattr(client.session, "filename", "unknown") or "unknown"

    async def put(self, client, entity) -> Optional[int]:
        """Remember an entity's access hash (and username mapping)."""
        if entity is None:
            return None
        try:
            if isinstance(entity, Channel):
                chat_id, access_hash, username = entity.id, entity.access_hash, entity.username
                prefix = "-100"
            elif isinstance(entity, Chat):
                chat_id, access_hash, username = entity.id, None, None
                prefix = "-"
            elif isinstance(entity, InputPeerChannel):
                chat_id, access_hash, username, prefix = entity.channel_id, entity.access_hash, None, "-100"
            elif isinstance(entity, User):
                chat_id, access_hash, username, prefix = entity.id, entity.access_hash, entity.username, ""
            else:
                return None
        except AttributeError:
            return None

        key = self._key(client)
        if access_hash:
            self._memory.setdefault(key, {})[chat_id] = access_hash
            if username:
                self._username_memory.setdefault(key, {})[username.lower()] = chat_id
            if self._store is not None:
                try:
                    await self._store.update_one(
                        "peers",
                        {"_id": f"{key}:{chat_id}"},
                        {"$set": {
                            "account": key,
                            "peer_id": chat_id,
                            "access_hash": access_hash,
                            "username": username,
                            "updated_at": time.time(),
                        }},
                        upsert=True,
                    )
                except Exception as exc:
                    logger.debug(f"peer cache write failed: {exc}")
        return access_hash

    async def forget(self, client, chat_id: Optional[int] = None,
                     username: Optional[str] = None) -> None:
        """Drop cached access hashes for a chat (the account left it).

        A stale hash makes Telethon keep trying to use a chat the account is no
        longer part of, which is how "Could not resolve chat" loops start.
        """
        key = self._key(client)
        if chat_id is not None:
            self._memory.get(key, {}).pop(int(chat_id), None)
            for cached_username, mapped in list(self._username_memory.get(key, {}).items()):
                if int(mapped) == int(chat_id):
                    self._username_memory[key].pop(cached_username, None)
            if self._store is not None:
                try:
                    await self._store.delete_one("peers", {"_id": f"{key}:{int(chat_id)}"})
                except Exception as exc:
                    logger.debug(f"peer cache delete failed: {exc}")
        if username:
            username = str(username).lower().lstrip("@")
            mapped = self._username_memory.get(key, {}).pop(username, None)
            if mapped:
                self._memory.get(key, {}).pop(int(mapped), None)
                if self._store is not None:
                    try:
                        await self._store.delete_one("peers", {"_id": f"{key}:{int(mapped)}"})
                    except Exception as exc:
                        logger.debug(f"peer cache delete failed: {exc}")

    async def get(self, client, chat_id: int) -> Optional[int]:
        key = self._key(client)
        cached = self._memory.get(key, {}).get(chat_id)
        if cached:
            return cached
        if self._store is None:
            return None
        try:
            doc = await self._store.find_one("peers", {"_id": f"{key}:{chat_id}"})
        except Exception:
            return None
        if doc and doc.get("access_hash"):
            self._memory.setdefault(key, {})[chat_id] = int(doc["access_hash"])
            if doc.get("username"):
                self._username_memory.setdefault(key, {})[doc["username"].lower()] = chat_id
            return int(doc["access_hash"])
        return None

    async def get_id_for_username(self, client, username: str) -> Optional[int]:
        key = self._key(client)
        found = self._username_memory.get(key, {}).get(username.lower())
        if found:
            return found
        if self._store is None:
            return None
        try:
            doc = await self._store.find_one("peers", {"account": key, "username": username.lstrip("@")})
        except Exception:
            return None
        return int(doc["peer_id"]) if doc else None

    async def preload(self, client, limit: int = 200) -> int:
        """Warm the cache from MongoDB for this account."""
        if self._store is None:
            return 0
        try:
            docs = await self._store.find("peers", {"account": self._key(client)}, limit=limit)
        except Exception:
            return 0
        bucket = self._memory.setdefault(self._key(client), {})
        for doc in docs:
            try:
                bucket[int(doc["peer_id"])] = int(doc["access_hash"])
                if doc.get("username"):
                    self._username_memory.setdefault(self._key(client), {})[
                        doc["username"].lower()
                    ] = int(doc["peer_id"])
            except (KeyError, TypeError, ValueError):
                continue
        return len(docs)


class PeerResolver:
    """Resolve links / ids / usernames into entities Telethon can actually use."""

    def __init__(self, store=None):
        self.cache = PeerCache(store)
        self._dialog_refreshed: Dict[str, float] = {}
        self._in_flight: Dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _entity_from_peer(peer) -> Optional[Any]:
        """Convert a stored Peer* object back into a real entity if possible."""
        return None  # only used for logging in some Telethon versions

    @classmethod
    def _input_from_entity(cls, entity):
        try:
            if isinstance(entity, Channel):
                return InputChannel(entity.id, entity.access_hash or 0)
            if isinstance(entity, Chat):
                return tl_utils.get_input_peer(entity)
            if isinstance(entity, User):
                return tl_utils.get_input_peer(entity)
        except Exception:
            return None
        return None

    @staticmethod
    def to_internal_channel_id(chat_id) -> int:
        """``-100800000001`` → ``800000001`` (Telegram's raw channel id)."""
        text = str(int(chat_id))
        if text.startswith("-100") and len(text) > 4:
            return int(text[4:])
        return abs(int(chat_id))

    async def _get_channels(self, client, chat_id: int, access_hash: int = 0):
        """``channels.GetChannels`` for a single id.

        Works for channels the account is a member of even with hash 0 (the
        server then returns the channel together with the real access hash,
        which we cache for later calls).
        """
        channel_id = self.to_internal_channel_id(chat_id)
        try:
            result = await client(GetChannelsRequest([InputChannel(channel_id, access_hash or 0)]))
        except Exception as exc:
            logger.debug(f"GetChannels failed for {chat_id}: {exc}")
            return None
        for chat in getattr(result, "chats", []) or []:
            if isinstance(chat, (Channel, Chat)):
                await self.cache.put(client, chat)
                return chat
        return None

    async def _resolve_username(self, client, username: str):
        username = username.lstrip("@")
        try:
            result = await client(ResolveUsernameRequest(username=username))
        except Exception as exc:
            logger.debug(f"ResolveUsername failed for {username}: {exc}")
            return None
        for chat in getattr(result, "chats", []) or []:
            await self.cache.put(client, chat)
            return chat
        for user in getattr(result, "users", []) or []:
            await self.cache.put(client, user)
            return user
        return None

    async def refresh_dialogs(self, client, force: bool = False, ttl: int = 300) -> int:
        """Pull the dialog list once (per TTL) to populate Telethon's cache."""
        key = PeerCache()._key(client)
        last = self._dialog_refreshed.get(key, 0)
        if not force and (time.time() - last) < ttl:
            return 0
        count = 0
        try:
            dialogs = await client.get_dialogs(limit=200)
            for dialog in dialogs or []:
                entity = getattr(dialog, "entity", None)
                if entity is not None:
                    await self.cache.put(client, entity)
                    count += 1
            self._dialog_refreshed[key] = time.time()
            logger.info(f"Dialog cache refreshed for {key}: {count} entities")
        except Exception as exc:
            logger.debug(f"refresh_dialogs failed: {exc}")
        return count

    async def join(self, client, *, chat_id: Optional[int] = None,
                   username: Optional[str] = None, invite_hash: Optional[str] = None):
        """Join a public channel by username/id or a private one via invite.

        Only *channels* can be joined with ``JoinChannelRequest``. A basic group
        (``Chat``, ids that do not carry the ``-100`` prefix) has no such method —
        accounts get in through an invite link or because an admin added them —
        so that case is reported instead of raising the old
        "Cannot cast InputPeerChat to any kind of InputChannel".
        """
        label = username or chat_id or invite_hash
        try:
            if invite_hash:
                return await self.join_by_invite(client, invite_hash, chat_id=chat_id)

            entity = None
            if username:
                entity = await self._resolve_username(client, username.lstrip("@"))
            if entity is None and chat_id is not None:
                entity = await self._get_channels(client, chat_id)

            if entity is None and chat_id is not None:
                # Last resort: let Telethon resolve it (works for cached peers).
                try:
                    entity = await client.get_entity(chat_id)
                except Exception:
                    entity = None

            if isinstance(entity, Chat):
                logger.info(
                    f"{label}: basic group — accounts must be added by an admin "
                    f"or an invite link; nothing to join automatically"
                )
                await self.cache.put(client, entity)
                return entity
            if entity is None:
                return None
            await self.cache.put(client, entity)
            await client(JoinChannelRequest(entity))
            logger.info(f"Joined channel via {label}")
            return entity
        except FloodWaitError:
            raise
        except Exception as exc:
            logger.warning(f"Join failed ({label}): {type(exc).__name__}: {exc}")
            return None

    async def join_by_invite(self, client, invite_hash: str,
                             chat_id: Optional[int] = None):
        """Import a private invite link — the part that used to be broken.

        Telegram answers ``messages.CheckChatInvite`` in three shapes and only two
        of them carry a ``chat`` field:

        * ``ChatInviteAlready`` — the account is already a member (``.chat`` set)
        * ``ChatInvitePeek``    — an expired link the account once peeked at
        * ``ChatInvite``        — **not a member yet** — this is the one we care
          about, and it has **no** ``.chat`` field at all. The old code read
          ``result.chat``, got ``None`` and returned without ever sending
          ``ImportChatInviteRequest``, so accounts could never join a private
          channel (the production "Could not resolve chat" loop).

        Errors are turned into actionable messages instead of being swallowed.
        """
        from telethon import errors as tl_errors

        result = None
        try:
            result = await client(CheckChatInviteRequest(invite_hash))
        except FloodWaitError:
            raise
        except Exception as exc:
            # invalid/expired hash, banned link, flood … — never hide this
            logger.warning(
                f"Invite check failed for …{str(invite_hash)[-8:]}: "
                f"{type(exc).__name__}: {exc}"
            )

        if isinstance(result, ChatInviteAlready):
            chat = getattr(result, "chat", None)
            if chat is not None:
                await self.cache.put(client, chat)
                logger.info("Invite: the account is already a member")
            return chat

        if isinstance(result, ChatInvitePeek):
            logger.info("Invite: link expired for this account, joining again")
            result = None            # fall through and import

        try:
            imported = await client(ImportChatInviteRequest(invite_hash))
        except FloodWaitError:
            raise
        except tl_errors.UserAlreadyParticipantError:
            # race: it joined between the check and the import
            logger.info("Invite: already a participant")
            return await self._resolve_after_join(client, chat_id)
        except tl_errors.InviteRequestSentError:
            raise ValueError(
                "the channel approves new members manually (join requests), so "
                "accounts stay outside until an admin accepts them"
            ) from None
        except (tl_errors.InviteHashExpiredError, tl_errors.InviteHashInvalidError) as exc:
            raise ValueError(
                f"the invite link is no longer valid for this account "
                f"({type(exc).__name__})"
            ) from None
        except Exception as exc:
            logger.warning(
                f"Invite import failed (…{str(invite_hash)[-8:]}): "
                f"{type(exc).__name__}: {exc}"
            )
            return None

        chats = [c for c in (getattr(imported, "chats", None) or [])
                 if isinstance(c, (Channel, Chat))]
        chat = chats[0] if chats else None
        if chat is None:
            chat = await self._resolve_after_join(client, chat_id)
        if chat is not None:
            await self.cache.put(client, chat)
            logger.info(f"Joined through the invite link: {getattr(chat, 'title', chat.id)}")
        return chat

    async def _resolve_after_join(self, client, chat_id: Optional[int]):
        """After joining, get the entity back (Updates may not carry it)."""
        if chat_id is None:
            return None
        entity = await self._get_channels(client, chat_id)
        if entity is not None:
            return entity
        try:
            return await client.get_entity(chat_id)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # main entry points
    # ------------------------------------------------------------------
    async def resolve(
        self,
        client,
        peer,
        *,
        username: Optional[str] = None,
        chat_id: Optional[int] = None,
        invite_hash: Optional[str] = None,
        join: bool = False,
    ):
        """Resolve a peer to a Telethon entity, learning/caching access hashes.

        ``peer`` may be an entity (returned as-is), a username/string, a
        ``PeerChannel``/``InputChannel`` or a numeric id.
        """
        if peer is not None and not isinstance(peer, (str, int, PeerChannel, InputChannel)) \
                and username is None and chat_id is None:
            return peer  # already resolved

        if isinstance(peer, str):
            if username is None and not peer.strip().lstrip("-").isdigit():
                username = peer
            elif chat_id is None and peer.strip().lstrip("-").isdigit():
                chat_id = int(peer)
        elif isinstance(peer, int) and chat_id is None:
            chat_id = peer

        if isinstance(peer, (PeerChannel, InputChannel)):
            if chat_id is None:
                chat_id = -(int(getattr(peer, "channel_id", 0)) + 10 ** 12)

        # 1) username path — Telethon handles usernames itself and caches them.
        #    A public channel/group needs no membership to be resolved, so this
        #    is tried first even when we also know the numeric id.
        if username:
            entity = await self._resolve_username(client, username)
            if entity is not None:
                return entity
            if chat_id is None and not join:
                raise ValueError(
                    f"Cannot resolve @{username} for this account "
                    f"(it may be private, deleted, or the account is not a member)"
                )

        if chat_id is None:
            if peer is None:
                if username:
                    entity = await self._resolve_username(client, username)
                    if entity is not None:
                        return entity
                raise ValueError("Cannot resolve a peer with no identifier")
            # Fall back to Telethon's own resolution (1:1 chats, cached peers)
            return await client.get_entity(peer)

        # 2) already known to Telethon?
        try:
            return await client.get_entity(chat_id)
        except Exception:
            pass

        # 3) our access-hash cache + GetChannels
        access_hash = await self.cache.get(client, chat_id)
        entity = await self._get_channels(client, chat_id, access_hash or 0)
        if entity is not None:
            return entity

        # 4) dialog refresh (account is a member but cache was cold)
        await self.refresh_dialogs(client)
        try:
            return await client.get_entity(chat_id)
        except Exception:
            pass

        # 5) username lookup when we also know the public name
        if username:
            entity = await self._resolve_username(client, username)
            if entity is not None:
                return entity

        # 6) join and retry
        if join:
            joined = await self.join(client, chat_id=chat_id, username=username,
                                     invite_hash=invite_hash)
            if joined is not None:
                return joined
            try:
                return await client.get_entity(chat_id)
            except Exception:
                pass

        hint = (
            f"private channel — it needs an invite link (bot must have "
            f"'Invite Users' permission) so the account can join first"
            if not username else
            f"public channel — check that @{username} exists and the account is not banned"
        )
        raise ValueError(
            f"Could not resolve chat {chat_id} for this account ({hint})."
        )

    async def resolve_message_link(self, client, link: str, *, join: bool = False,
                                   invite_hash: Optional[str] = None) -> Tuple[Any, int]:
        """Return ``(entity, message_id)`` for any Telegram message link.

        ``invite_hash`` overrides the hash found in the link itself — the panel
        uses this to pass the invite link the bot created for a private channel
        so accounts can join before reacting.
        """
        info = parse_link(link)
        if info.kind == "unknown":
            raise ValueError(f"Unrecognised Telegram link: {link}")
        effective_hash = invite_hash or info.invite_hash
        entity = await self.resolve(
            client, None, username=info.username, chat_id=info.chat_id,
            invite_hash=effective_hash, join=join,
        )
        return entity, int(info.message_id or 0)

    async def ensure_member(self, client, link: str, *, invite_hash: Optional[str] = None) -> Any:
        """Make sure the account is inside the chat behind *link*, join if needed."""
        return await self.resolve_message_link(client, link, join=True)


_resolver: Optional[PeerResolver] = None


def get_peer_resolver(store=None) -> PeerResolver:
    global _resolver
    if _resolver is None or store is not None:
        if store is None:
            try:
                from src.db.store import get_store_sync
                store = get_store_sync()
            except Exception:
                store = None
        _resolver = PeerResolver(store)
    return _resolver


def reset_peer_resolver() -> None:
    global _resolver
    _resolver = None
