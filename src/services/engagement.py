"""
Engagement operations performed with the signed-in accounts.

These are the "worker" functions behind every panel feature:

* reactions on a post (manual orders *and* auto-reactions on new posts)
* views on a post (manual orders *and* auto-views on new posts)
* members joining a channel/group
* comments on a post

All of them are defensive by design: one dead account must never abort the
whole batch, FloodWait is respected, and every account's entity access-hash is
learned & cached so private-channel links keep working after a restart.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from telethon.errors import FloodWaitError, ReactionInvalidError
from telethon.tl.functions.messages import GetMessagesViewsRequest
from telethon.tl.types import ReactionEmoji

from src.peers import get_peer_resolver, parse_link
from src.services.accounts import AccountRegistry, get_account_registry
from src.services.settings import (
    DEFAULT_POSITIVE_EMOJIS,
    get_settings_service,
    sanitize_reaction_emojis,
)
from src.utils import is_fatal_session_error, is_session_revoked_error

logger = logging.getLogger(__name__)

# Errors that mean "this client is not connected right now" — the account is
# fine, so we reconnect it and run the operation again instead of reporting a
# failure (the production log was full of "Cannot send requests while
# disconnected" which used to fail a whole batch).
_DISCONNECT_MARKERS = (
    "cannot send requests while disconnected",
    "not connected",
    "connection error",
    "connectionerror",
    "disconnected",
    "database is locked",
    "server closed the connection",
    "connection reset",
    "timed out",
    "timeout",
    "temporarily unavailable",
    "eof occurred",
)


def looks_like_disconnect_error(error) -> bool:
    """Is this a connection problem (retry) rather than a dead account?"""
    text = str(error or "").lower()
    return any(marker in text for marker in _DISCONNECT_MARKERS)


# Emojis Telegram itself refused (learned at runtime). Telegram only accepts its
# own reaction set, so a stored list may contain something like "✅" that every
# account would fail on — those are remembered and never picked again.
REJECTED_REACTION_EMOJIS: set = set()
_ALERTED_REACTION_EMOJIS: set = set()


def pick_reaction_emojis(pool: Optional[Sequence[str]] = None) -> List[str]:
    """The usable reaction pool (configured list minus what Telegram rejected)."""
    kept, _ = sanitize_reaction_emojis(list(pool or DEFAULT_POSITIVE_EMOJIS))
    usable = [e for e in kept if e not in REJECTED_REACTION_EMOJIS]
    return usable or list(DEFAULT_POSITIVE_EMOJIS)


async def send_reaction(client, entity, message_id: int, emojis: Sequence[str],
                        *, attempts: int = 3) -> str:
    """Send a reaction, transparently working around emojis Telegram refuses.

    Returns the emoji that actually went through and raises only when every
    candidate was rejected — with a message that explains the real reason.
    """
    pool = list(dict.fromkeys(str(e) for e in emojis if isinstance(e, str) and e.strip()))
    if not pool:
        pool = list(DEFAULT_POSITIVE_EMOJIS)
    candidates = [e for e in pool if e not in REJECTED_REACTION_EMOJIS] or pool
    sampled = random.sample(candidates, min(len(candidates), max(1, attempts)))
    refused: List[str] = []
    for emoji in sampled:
        try:
            await client(SendReactionRequestCompat(
                peer=entity, msg_id=int(message_id),
                reaction=[ReactionEmoji(emoticon=emoji)],
            ))
            return emoji
        except ReactionInvalidError:
            refused.append(emoji)
            REJECTED_REACTION_EMOJIS.add(emoji)
            logger.warning(
                f"Telegram rejected the reaction emoji {emoji!r} — trying another one "
                f"and dropping it from the pool"
            )
            await _alert_rejected_emoji(emoji)
    raise ValueError(
        "Every reaction emoji was refused by Telegram "
        f"({', '.join(refused) or 'none tried'}) — this channel may only allow "
        "specific reactions; change the emoji list in the admin panel"
    )


async def _alert_rejected_emoji(emoji: str) -> None:
    """Tell the admins once that a configured emoji is not usable."""
    if emoji in _ALERTED_REACTION_EMOJIS:
        return
    _ALERTED_REACTION_EMOJIS.add(emoji)
    try:
        from src.services.notify import get_notifier

        await get_notifier().to_admins(
            "⚠️ <b>Reaction emoji skipped</b>\n\n"
            f"Telegram refused the emoji <code>{emoji}</code> — it only accepts its "
            "own reaction set, so this one can never be used.\n\n"
            "I picked another emoji for this post and stopped using it. "
            "Open <b>Admin panel → ❤️ Reactions → Emojis</b> to review the list."
        )
    except Exception as exc:  # never break a reaction batch because of a notice
        logger.debug(f"rejected-emoji notice failed: {exc}")


def _is_member_entity(entity) -> bool:
    """True when the account can already use this chat (member / public chat)."""
    if entity is None:
        return False
    if getattr(entity, "left", False):
        return False
    for attr in ("broadcast", "megagroup"):        # channels / supergroups
        if hasattr(entity, attr):
            return True
    try:
        from telethon.tl.types import Chat, User
        return isinstance(entity, (Chat, User))
    except Exception:  # pragma: no cover - defensive
        return True


def looks_like_invite_problem(errors) -> bool:
    """Do these batch errors point at a broken invite link / missing membership?

    Used to decide that a whole batch is worth one retry with a freshly exported
    invite link instead of reporting a plain failure.
    """
    blob = " ".join(str(e).lower() for e in (errors or []))
    return any(marker in blob for marker in (
        "invite", "no longer valid", "could not resolve chat", "join",
        "not a member", "hash",
    ))

# Telegram-side guard rails
MAX_FLOOD_SLEEP = 90          # don't block a batch for longer than this
PER_ACCOUNT_TIMEOUT = 45      # seconds for a single account's operation


@dataclass
class OperationResult:
    """Outcome of a batch operation."""

    ok: int = 0
    failed: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def attempted(self) -> int:
        return self.ok + self.failed

    def add_error(self, message: str) -> None:
        if len(self.errors) < 8:
            self.errors.append(message)

    def summary(self) -> str:
        parts = [f"✅ {self.ok} success"]
        if self.failed:
            parts.append(f"❌ {self.failed} failed")
        if self.skipped:
            parts.append(f"⏭ {self.skipped} skipped")
        return " • ".join(parts)


ProgressFn = Callable[[str], Awaitable[None]]


class EngagementService:
    """Runs engagement operations across the account pool."""

    def __init__(self, registry: Optional[AccountRegistry] = None, settings=None, resolver=None):
        self._registry = registry
        self._settings = settings
        self._resolver = resolver

    # -- dependencies (lazy so tests can inject) ---------------------------
    @property
    def registry(self) -> AccountRegistry:
        if self._registry is None:
            self._registry = get_account_registry()
        return self._registry

    @property
    def settings(self):
        if self._settings is None:
            self._settings = get_settings_service()
        return self._settings

    @property
    def resolver(self):
        if self._resolver is None:
            self._resolver = get_peer_resolver()
        return self._resolver

    # ------------------------------------------------------------------
    # generic batch runner
    # ------------------------------------------------------------------
    async def _run_batch(
        self,
        clients: Sequence[Tuple[str, Any]],
        worker: Callable[[str, Any], Awaitable[Optional[bool]]],
        *,
        delay_min: float = 0.0,
        delay_max: float = 0.0,
        progress: Optional[ProgressFn] = None,
        progress_every: int = 5,
        label: str = "operation",
    ) -> OperationResult:
        """Run *worker* for every client, isolating failures."""
        result = OperationResult()
        total = len(clients)
        for index, (name, client) in enumerate(clients, start=1):
            if delay_min or delay_max:
                lo, hi = sorted((max(0.0, delay_min), max(0.0, delay_max)))
                await asyncio.sleep(random.uniform(lo, hi))
            try:
                outcome = await asyncio.wait_for(worker(name, client), timeout=PER_ACCOUNT_TIMEOUT)
                if outcome is False:
                    result.skipped += 1
                else:
                    result.ok += 1
            except asyncio.TimeoutError:
                result.failed += 1
                result.add_error(f"{name}: timed out")
                logger.warning(f"{label}: {name} timed out")
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 0) or 0)
                if 0 < wait <= MAX_FLOOD_SLEEP:
                    logger.warning(f"{label}: flood wait {wait}s, waiting")
                    await asyncio.sleep(wait)
                    try:
                        outcome = await asyncio.wait_for(worker(name, client), timeout=PER_ACCOUNT_TIMEOUT)
                        if outcome is False:
                            result.skipped += 1
                        else:
                            result.ok += 1
                        continue
                    except Exception as retry_exc:  # noqa: BLE001
                        result.failed += 1
                        result.add_error(f"{name}: {type(retry_exc).__name__}")
                        continue
                result.skipped += 1
                result.add_error(f"{name}: flood wait {wait}s")
            except Exception as exc:  # noqa: BLE001
                if is_session_revoked_error(exc) or is_fatal_session_error(exc):
                    result.skipped += 1
                    result.add_error(f"{name}: session revoked")
                    await self.registry.remove_by_session(name, "revoked during operation")
                    try:
                        await self.registry._notify_revoked(name, exc)
                    except Exception:  # noqa: BLE001 - notice only
                        pass
                elif looks_like_disconnect_error(exc):
                    # Self-healing: reconnect this account and try once more.
                    logger.info(f"{label}: {name} lost its connection ({str(exc)[:60]}) — reconnecting")
                    try:
                        await client.connect()
                        outcome = await asyncio.wait_for(worker(name, client), timeout=PER_ACCOUNT_TIMEOUT)
                        if outcome is False:
                            result.skipped += 1
                        else:
                            result.ok += 1
                        logger.info(f"{label}: {name} recovered after reconnecting")
                    except Exception as retry_exc:  # noqa: BLE001
                        result.failed += 1
                        result.add_error(f"{name}: {type(retry_exc).__name__}: {str(retry_exc)[:60]}")
                        logger.warning(f"{label} failed for {name} after reconnect: {retry_exc}")
                else:
                    result.failed += 1
                    result.add_error(f"{name}: {type(exc).__name__}: {str(exc)[:80]}")
                    logger.warning(f"{label} failed for {name}: {exc}")
            if progress and (index % progress_every == 0 or index == total):
                try:
                    await progress(f"{label}: {index}/{total} accounts processed")
                except Exception as exc:  # progress must never break the batch
                    logger.debug(f"progress callback failed: {exc}")
        return result

    async def invite_hash_for(self, chat_id, *, force: bool = False) -> Optional[str]:
        """Look up the invite link the bot created for a private channel.

        Without this, accounts that are not members of a private channel cannot
        join, so reactions/views/comments from them would fail.

        Self-healing: when no link is stored yet (or ``force=True`` says the
        stored one died) the running bot is asked for a fresh invite link, which
        is then saved on the channel. That is what makes manual orders work on a
        channel the bot has only just been added to — before a single post ran.
        """
        if chat_id is None:
            return None
        doc = await self._channel_doc_for(chat_id)
        link = (doc or {}).get("invite_link")
        if force or not link:
            exported = await self._export_invite_link(doc)
            if exported:
                link = exported
        if not link:
            return None
        info = parse_link(link)
        return info.invite_hash or info.username or None

    async def _channel_doc_for(self, chat_id) -> Optional[Dict[str, Any]]:
        """The stored channel document behind a link/id (any id spelling)."""
        try:
            from src.peers import PeerResolver
            from src.services.channels import get_channel_service
            service = get_channel_service()
            internal = PeerResolver.to_internal_channel_id(chat_id)
            # Accept both the -100… form and the raw internal id.
            for candidate in (int(chat_id), int(f"-100{internal}"), internal):
                doc = await service.get(candidate)
                if doc:
                    return doc
        except Exception:
            return None
        return None

    async def _export_invite_link(self, doc: Optional[Dict[str, Any]]) -> Optional[str]:
        """Ask the live bot to create an invite link for this private channel."""
        if not doc:
            return None
        if doc.get("username"):
            return None            # public: accounts can react without joining
        try:
            from src.runtime import get_bot
            exporter = getattr(get_bot(), "_export_invite", None)
            if exporter is None:
                return None
            link = await exporter(doc["_id"])
        except Exception as exc:
            logger.debug(f"invite export failed for {doc.get('_id')}: {exc}")
            return None
        if not link:
            return None
        try:
            from src.services.channels import get_channel_service
            await get_channel_service().register(doc["_id"], invite_link=link)
        except Exception as exc:
            logger.debug(f"could not store invite link for {doc.get('_id')}: {exc}")
        logger.info(f"Invite link ready for {doc['_id']}")
        return link

    async def refresh_invite(self, link: str) -> Optional[str]:
        """Replace a dead invite link for the channel behind *link*.

        Used when a whole batch failed: the stored link was revoked/expired, so
        a fresh one is exported before the batch is retried once.
        """
        info = parse_link(link or "")
        if info.invite_hash:
            return info.invite_hash          # the caller already has a link
        if info.chat_id is None:
            return None
        return await self.invite_hash_for(info.chat_id, force=True)

    async def join_target(self, link: str) -> Dict[str, Any]:
        """Resolve the join target (invite hash) for a post link when possible."""
        info = parse_link(link)
        invite_hash = info.invite_hash
        if not invite_hash and info.chat_id is not None:
            invite_hash = await self.invite_hash_for(info.chat_id)
        return {"invite_hash": invite_hash, "info": info}

    # ------------------------------------------------------------------
    # reactions
    # ------------------------------------------------------------------
    async def react_to_link(
        self,
        link: str,
        account_count: int,
        *,
        emojis: Optional[Sequence[str]] = None,
        delay: Optional[Tuple[float, float]] = None,
        join_if_needed: bool = True,
        exclude: Sequence[str] = (),
        progress: Optional[ProgressFn] = None,
        resolved_cache: Optional[Dict[str, Any]] = None,
    ) -> OperationResult:
        """Apply reactions from *account_count* different accounts."""
        reaction_cfg = await self.settings.section("reactions")
        emojis = pick_reaction_emojis(emojis or reaction_cfg.get("emojis"))
        delay_min, delay_max = delay or (
            float(reaction_cfg.get("delay_min", 3.0)), float(reaction_cfg.get("delay_max", 12.0))
        )
        clients = await self.registry.pick(account_count, exclude=exclude)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])

        info = parse_link(link)
        invite_hash = info.invite_hash or await self.invite_hash_for(info.chat_id)

        async def worker(name: str, client) -> bool:
            entity, message_id = await self.resolver.resolve_message_link(
                client, link, join=join_if_needed, invite_hash=invite_hash,
            )
            if not message_id:
                raise ValueError("Link has no message id")
            emoji = await send_reaction(client, entity, int(message_id), emojis)
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="reactions")
            logger.info(f"Reaction {emoji} applied to {message_id} by {name}")
            return True

        result = await self._run_batch(
            clients, worker, delay_min=delay_min, delay_max=delay_max,
            progress=progress, label="reactions",
        )
        result.details.update({"emoji_pool": emojis, "accounts_used": len(clients)})
        return result

    async def auto_react(
        self,
        *,
        channel_id: Any,
        message_id: int,
        account_count: int,
        delay: Optional[Tuple[float, float]] = None,
        join_if_needed: bool = True,
        emojis: Optional[Sequence[str]] = None,
        invite_link: Optional[str] = None,
        username: Optional[str] = None,
        exclude: Sequence[str] = (),
    ) -> OperationResult:
        """Auto-react to a freshly published post.

        * **public** channel (``username`` known) — nothing to join, the account
          just resolves ``@username`` and reacts.
        * **private** channel — the account must be a member; it joins through the
          invite link the bot exported (``invite_link``) and then reacts.

        Reactions never touch the view counter — views are a separate, paid
        service with its own accounts.
        """
        reaction_cfg = await self.settings.section("reactions")
        auto_cfg = reaction_cfg.get("auto", {}) or {}
        emojis = pick_reaction_emojis(emojis or reaction_cfg.get("emojis"))
        delay_min, delay_max = delay or (
            float(auto_cfg.get("delay_min", 1.0)), float(auto_cfg.get("delay_max", 3.0))
        )
        clients = await self.registry.pick(account_count, exclude=exclude)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])
        invite_hash = None
        if invite_link:
            info = parse_link(invite_link)
            invite_hash = info.invite_hash or info.username or None

        async def worker(name: str, client) -> bool:
            entity = await self.resolver.resolve(
                client, channel_id, username=username, join=join_if_needed,
                invite_hash=invite_hash,
            )
            await send_reaction(client, entity, int(message_id), emojis)
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="reactions")
            return True

        result = await self._run_batch(
            clients, worker, delay_min=delay_min, delay_max=delay_max, label="auto-reactions"
        )
        result.details["accounts_used"] = len(clients)
        result.details["emoji_pool"] = emojis
        return result

    async def leave_channel(
        self,
        *,
        channel_id: Any = None,
        username: Optional[str] = None,
        invite_link: Optional[str] = None,
        reason: str = "the bot was removed",
    ) -> OperationResult:
        """Make every pool account leave a channel again.

        Used when the channel owner removes the bot: the accounts that joined to
        react (or that a paid plan put there) must not stay inside somebody
        else's channel. Accounts that were never members are skipped, failures
        never abort the batch.
        """
        clients = await self.registry.pick(1000, mark=False)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])
        invite_hash = None
        if invite_link:
            info = parse_link(invite_link)
            invite_hash = info.invite_hash or info.username or None

        async def worker(name: str, client) -> bool:
            from telethon.tl.functions.channels import LeaveChannelRequest

            entity = None
            try:
                entity = await self.resolver.resolve(
                    client, channel_id, username=username,
                    invite_hash=invite_hash, join=False,
                )
            except Exception as exc:  # noqa: BLE001 - not a member / cannot see it
                logger.debug(f"{name}: nothing to leave ({exc})")
                return False
            if entity is None:
                return False
            from telethon.tl.types import Channel

            if isinstance(entity, Channel) and getattr(entity, "left", False):
                return False
            peer = await client.get_input_entity(entity)
            await client(LeaveChannelRequest(peer))
            await self.cache_forget(client, channel_id, username)
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="leave")
            logger.info(f"{name} left {channel_id or username}")
            return True

        result = await self._run_batch(clients, worker, label="leave")
        result.details["reason"] = reason
        return result

    async def cache_forget(self, client, channel_id: Any = None,
                           username: Optional[str] = None) -> None:
        """Drop a chat from the access-hash cache (after leaving it)."""
        try:
            from src.peers import PeerResolver
            internal = None
            if channel_id is not None:
                internal = PeerResolver.to_internal_channel_id(channel_id)
            await self.resolver.cache.forget(client, internal, username)
        except Exception as exc:  # noqa: BLE001 - cache hygiene only
            logger.debug(f"peer cache forget failed: {exc}")

    async def ensure_members(
        self,
        *,
        channel_id: Any,
        account_count: int,
        invite_link: Optional[str] = None,
        username: Optional[str] = None,
        delay: Optional[Tuple[float, float]] = None,
    ) -> OperationResult:
        """Put pool accounts *inside* a channel before the first post.

        Only needed for private channels (``username`` empty): a public channel
        can be reacted to without joining, so this returns early.
        """
        if username:
            return OperationResult(details={"skipped": "public channel — no join needed"})
        if not invite_link:
            return OperationResult(errors=["No invite link available to join with"])
        invite_hash = parse_link(invite_link).invite_hash
        if not invite_hash:
            return OperationResult(errors=["Invite link has no hash to import"])
        if delay is None:
            # same human-like pacing as the reactions themselves
            cfg = (await self.settings.section("reactions")).get("auto", {}) or {}
            delay = (float(cfg.get("delay_min", 1.0)), float(cfg.get("delay_max", 3.0)))
        clients = await self.registry.pick(account_count)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])

        async def worker(name: str, client) -> bool:
            # Already inside? Then there is nothing to do — and it must not be
            # reported as a fresh join (the owner sees those numbers).
            try:
                existing = await self.resolver.resolve(client, channel_id)
            except Exception:
                existing = None
            if _is_member_entity(existing):
                logger.debug(f"{name} is already a member of {channel_id}")
                return False
            try:
                entity = await self.resolver.resolve(
                    client, channel_id, join=True, invite_hash=invite_hash,
                )
            except Exception as exc:  # noqa: BLE001 - reported through result.errors
                raise ValueError(str(exc)) from exc
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="join")
            logger.info(f"{name} joined {channel_id} through the invite link")
            return _is_member_entity(entity) or entity is not None

        result = await self._run_batch(clients, worker, delay_min=delay[0],
                                       delay_max=delay[1], label="join")
        result.details["accounts_used"] = len(clients)
        return result

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    async def send_views(
        self,
        link: str,
        quantity: int,
        *,
        account_count: Optional[int] = None,
        delay: Optional[Tuple[float, float]] = None,
        join_if_needed: bool = True,
        progress: Optional[ProgressFn] = None,
    ) -> OperationResult:
        """Push *quantity* views onto a post.

        Telegram counts one view per account per post, so the number of accounts
        is the real ceiling; extra accounts are simply reused (each account can
        view several posts, not the same post twice).
        """
        views_cfg = await self.settings.section("views")
        delay_min, delay_max = delay or (
            float(views_cfg.get("auto", {}).get("delay_min", 2.0)),
            float(views_cfg.get("auto", {}).get("delay_max", 8.0)),
        )
        quantity = max(0, int(quantity))
        available = await self.registry.available_count()
        wanted = min(account_count or available or quantity, available) if available else 0
        if quantity and available:
            # each account can contribute one view per post
            wanted = max(1, min(quantity, available))
        clients = await self.registry.pick(wanted) if wanted else []
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])
        view_info = parse_link(link)
        view_invite = view_info.invite_hash or await self.invite_hash_for(view_info.chat_id)

        async def worker(name: str, client) -> bool:
            entity, message_id = await self.resolver.resolve_message_link(
                client, link, join=join_if_needed, invite_hash=view_invite,
            )
            if not message_id:
                raise ValueError("Link has no message id")
            # 1) fetching the message registers a view
            try:
                await client.get_messages(entity, ids=[int(message_id)])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"get_messages for views failed ({name}): {exc}")
            # 2) explicit increment request (this is what official clients send)
            await client(GetMessagesViewsRequest(
                peer=entity, id=[int(message_id)], increment=True
            ))
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="views")
            return True

        result = await self._run_batch(
            clients, worker, delay_min=delay_min, delay_max=delay_max,
            progress=progress, label="views", progress_every=10,
        )
        # Views beyond the account pool cannot be delivered honestly — report it.
        sent = result.ok
        result.details.update({
            "requested": quantity,
            "accounts_used": len(clients),
            "delivered": sent,
        })
        if quantity > sent:
            result.details["shortfall"] = quantity - sent
        return result

    async def auto_views(
        self,
        *,
        channel_id: Any,
        message_id: int,
        views_per_post: Optional[int] = None,
        invite_link: Optional[str] = None,
    ) -> OperationResult:
        """Auto-views for a newly published post."""
        views_cfg = await self.settings.section("views")
        auto_cfg = views_cfg.get("auto", {}) or {}
        per_post = int(views_per_post or auto_cfg.get("views_per_post", 100))
        available = await self.registry.available_count()
        clients = await self.registry.pick(min(per_post, available) if available else 0)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])
        invite_hash = None
        if invite_link:
            info = parse_link(invite_link)
            invite_hash = info.invite_hash or info.username or None

        async def worker(name: str, client) -> bool:
            entity = await self.resolver.resolve(client, channel_id, join=True,
                                                 invite_hash=invite_hash)
            try:
                await client.get_messages(entity, ids=[int(message_id)])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"get_messages failed: {exc}")
            await client(GetMessagesViewsRequest(peer=entity, id=[int(message_id)], increment=True))
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="views")
            return True

        result = await self._run_batch(clients, worker, delay_min=1.0, delay_max=6.0,
                                       label="auto-views", progress_every=10)
        result.details["accounts_used"] = len(clients)
        return result

    # ------------------------------------------------------------------
    # members
    # ------------------------------------------------------------------
    async def add_members(
        self,
        channel: str,
        count: int,
        *,
        invite_link: Optional[str] = None,
        progress: Optional[ProgressFn] = None,
    ) -> OperationResult:
        """Have *count* accounts join a channel/group."""
        target = invite_link or channel
        info = parse_link(target)
        cfg = await self.settings.section("members")
        delay_min = float(cfg.get("delay_min", 2.0))
        delay_max = float(cfg.get("delay_max", 8.0))
        available = await self.registry.available_count()
        clients = await self.registry.pick(min(int(count or 0), available) if available else 0)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])

        async def worker(name: str, client) -> bool:
            if info.kind == "invite":
                entity = await self.resolver.join(client, invite_hash=info.invite_hash)
            else:
                entity = await self.resolver.join(
                    client, username=info.username, chat_id=info.chat_id
                )
            if entity is None:
                raise ValueError("join failed")
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="joins")
            return True

        result = await self._run_batch(
            clients, worker, delay_min=delay_min, delay_max=delay_max,
            progress=progress, label="members",
        )
        result.details["accounts_used"] = len(clients)
        return result

    # ------------------------------------------------------------------
    # comments
    # ------------------------------------------------------------------
    async def post_comments(
        self,
        link: str,
        text: str,
        *,
        account_count: int,
        per_account_texts: Optional[Sequence[str]] = None,
        progress: Optional[ProgressFn] = None,
    ) -> OperationResult:
        """Post comments on a channel post using N accounts.

        The number of accounts is hard-capped by the admin-configured
        ``comments.max_accounts`` limit, so no caller (panel, CLI or a future
        API) can spend more accounts than the owner allows.
        """
        cfg = await self.settings.section("comments")
        cap = int(cfg.get("max_accounts", 0) or 0)
        if cap > 0:
            account_count = min(int(account_count), cap)
        comment_info = parse_link(link)
        comment_invite = comment_info.invite_hash or await self.invite_hash_for(comment_info.chat_id)
        clients = await self.registry.pick(account_count)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])
        variants = [t for t in (per_account_texts or []) if t and t.strip()]

        async def worker(name: str, client) -> bool:
            entity, message_id = await self.resolver.resolve_message_link(
                client, link, join=True, invite_hash=comment_invite,
            )
            if not message_id:
                raise ValueError("Link has no message id")
            body = random.choice(variants) if variants else text
            try:
                await client.send_message(entity, body, comment_to=int(message_id))
            except TypeError:
                # Older Telethon: reply in the discussion group instead
                await client.send_message(entity, body, reply_to=int(message_id))
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="comments")
            return True

        result = await self._run_batch(
            clients, worker,
            delay_min=float(cfg.get("delay_min", 4.0)),
            delay_max=float(cfg.get("delay_max", 15.0)),
            progress=progress, label="comments",
        )
        result.details["accounts_used"] = len(clients)
        return result

    async def auto_comments(
        self,
        *,
        channel_id: Any,
        message_id: int,
        texts: Sequence[str],
        account_count: int,
        username: Optional[str] = None,
        invite_link: Optional[str] = None,
        delay: Optional[Tuple[float, float]] = None,
    ) -> OperationResult:
        """Post random positive comments on a freshly published channel post.

        Works like :meth:`auto_react`: the accounts are resolved from the channel
        id/@username (joining through the invite link when the channel is
        private), then each one writes one of the supplied texts. Texts are
        assigned randomly, so the comment section does not look copy-pasted.
        """
        cfg = await self.settings.section("comments")
        cap = int(cfg.get("max_accounts", 0) or 0)
        if cap > 0:
            account_count = min(int(account_count), cap)
        pool = [t.strip() for t in (texts or []) if str(t).strip()]
        if not pool:
            return OperationResult(errors=["No comment texts configured"])
        clients = await self.registry.pick(account_count)
        if not clients:
            return OperationResult(errors=["No signed-in accounts available"])
        auto_cfg = (cfg.get("auto") or {})
        if delay is None:
            delay = (float(auto_cfg.get("delay_min", 4.0)), float(auto_cfg.get("delay_max", 12.0)))
        invite_hash = None
        if invite_link:
            info = parse_link(invite_link)
            invite_hash = info.invite_hash or info.username or None

        bodies = [random.choice(pool) for _ in clients]

        async def worker(name: str, client) -> bool:
            entity = await self.resolver.resolve(
                client, channel_id, username=username, join=True, invite_hash=invite_hash,
            )
            body = bodies.pop() if bodies else random.choice(pool)
            try:
                await client.send_message(entity, body, comment_to=int(message_id))
            except TypeError:
                # Older Telethon: reply in the discussion group instead
                await client.send_message(entity, body, reply_to=int(message_id))
            doc_id = await self.registry.doc_id_for(name)
            if doc_id:
                await self.registry.store.touch(doc_id, kind="comments")
            return True

        result = await self._run_batch(
            clients, worker, delay_min=delay[0], delay_max=delay[1], label="auto-comments",
        )
        result.details["accounts_used"] = len(clients)
        return result


def SendReactionRequestCompat(peer, msg_id: int, reaction):
    """``messages.SendReactionRequest`` across Telethon versions."""
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        return SendReactionRequest(peer=peer, msg_id=msg_id, reaction=reaction)
    except ImportError:  # pragma: no cover - very old Telethon
        from telethon.tl.functions.messages import SendReactionRequest as Req  # type: ignore
        return Req(peer=peer, msg_id=msg_id, reaction=reaction)


_engagement: Optional[EngagementService] = None


def get_engagement_service(registry=None, settings=None) -> EngagementService:
    global _engagement
    if _engagement is None or registry is not None or settings is not None:
        _engagement = EngagementService(registry, settings)
    return _engagement


def reset_engagement_service() -> None:
    global _engagement
    _engagement = None
