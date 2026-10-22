"""
Signed-in Telegram accounts, persisted in MongoDB.

Why
---
The old panel kept accounts in ``clients.json`` + ``*.session`` files on disk.
A crash, a container rebuild or a wiped volume meant every signed-in account was
gone and had to be re-authenticated by hand.

This module stores, for every account:

* the **session string** (a full port of the Telethon session) — so the account
  can be rebuilt on any host without the original ``.session`` file,
* the phone number, Telegram user id, name/username, who signed it in,
* health metadata: status, last error, last use, groups count.

``AccountRegistry`` is the bridge between those documents and the live
``TelegramClient`` objects the panel actually uses to perform operations.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from telethon import TelegramClient
from telethon.sessions import StringSession

from src.config import API_ID, API_HASH
from src.db.store import COLL_ACCOUNTS, get_store_sync

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"
STATUS_REVOKED = "revoked"
STATUS_ERROR = "error"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_session_string(client) -> Optional[str]:
    """Serialise any Telethon session (SQLite or String) into a string."""
    try:
        session = getattr(client, "session", None)
        if session is None:
            return None
        if isinstance(session, StringSession):
            return session.save()
        return StringSession.save(session)
    except Exception as exc:
        logger.warning(f"Could not export session string: {exc}")
        return None


def build_client_from_session(session_string: str, session_name: Optional[str] = None,
                              **kwargs) -> Optional[TelegramClient]:
    """Rebuild a connected-ready client from a stored session string.

    ``StringSession`` has no file name, so Telethon's own ``get_session_name``
    used to answer "Unknown" for every restored account — which also meant every
    restored account shared a single access-hash cache bucket. The name is
    therefore attached to the client explicitly.
    """
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH, **kwargs)
        if session_name:
            try:
                client._panel_session_name = str(session_name)
            except Exception:  # pragma: no cover - defensive
                pass
        return client
    except Exception as exc:
        logger.error(f"Could not build client from stored session: {exc}")
        return None


class AccountStore:
    """CRUD for the ``accounts`` collection."""

    def __init__(self, store=None):
        self._store = store

    @property
    def store(self):
        return self._store or get_store_sync()

    @staticmethod
    def account_id(phone: Optional[str], tg_user_id: Optional[int] = None) -> str:
        if tg_user_id:
            return f"tg_{tg_user_id}"
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        return f"phone_{digits}"

    async def save_signed_in(
        self,
        client,
        phone: Optional[str] = None,
        owner_id: Optional[int] = None,
        *,
        session_string: Optional[str] = None,
        me=None,
    ) -> Dict[str, Any]:
        """Persist (or refresh) an account after a successful login."""
        if me is None:
            try:
                me = await client.get_me()
            except Exception as exc:
                logger.warning(f"get_me failed while saving account: {exc}")
                me = None

        session_string = session_string or export_session_string(client)
        tg_user_id = getattr(me, "id", None)
        username = getattr(me, "username", None)
        first_name = getattr(me, "first_name", None) or ""
        last_name = getattr(me, "last_name", None) or ""
        display_name = (f"{first_name} {last_name}".strip()) or username or phone or "account"

        doc_id = self.account_id(phone or getattr(me, "phone", None), tg_user_id)
        existing = await self.store.find_one(COLL_ACCOUNTS, {"_id": doc_id})
        payload = {
            "phone": phone or getattr(me, "phone", None),
            "tg_user_id": tg_user_id,
            "username": username,
            "name": display_name,
            "session_string": session_string,
            "status": STATUS_ACTIVE,
            "last_error": None,
            "updated_at": _now_iso(),
            "last_seen": time.time(),
        }
        if existing is None:
            payload.update({
                "owner_id": owner_id,
                "created_at": _now_iso(),
                "source": "panel",
                "reactions_done": 0,
                "views_done": 0,
                "joins_done": 0,
                "groups": [],
            })
            await self.store.insert_one(COLL_ACCOUNTS, {"_id": doc_id, **payload})
        else:
            await self.store.update_one(COLL_ACCOUNTS, {"_id": doc_id}, {"$set": payload})
            if owner_id and not existing.get("owner_id"):
                await self.store.update_one(
                    COLL_ACCOUNTS, {"_id": doc_id}, {"$set": {"owner_id": owner_id}}
                )
        logger.info(f"Account persisted to database: {doc_id} ({display_name})")
        return {"_id": doc_id, **payload}

    async def get(self, doc_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_ACCOUNTS, {"_id": doc_id})

    async def find_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_ACCOUNTS, {"phone": phone})

    async def list_accounts(
        self,
        status: Optional[str] = None,
        owner_id: Optional[int] = None,
        limit: int = 0,
    ) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {}
        if status:
            query["status"] = status
        if owner_id is not None:
            query["owner_id"] = int(owner_id)
        return await self.store.find(COLL_ACCOUNTS, query, sort=[("created_at", -1)], limit=limit)

    async def all_accounts(self) -> List[Dict[str, Any]]:
        return await self.store.find(COLL_ACCOUNTS, {})

    async def count(self, status: Optional[str] = None) -> int:
        return await self.store.count(COLL_ACCOUNTS, {"status": status} if status else {})

    async def set_status(self, doc_id: str, status: str, error: Optional[str] = None) -> None:
        await self.store.update_one(
            COLL_ACCOUNTS,
            {"_id": doc_id},
            {"$set": {"status": status, "last_error": error, "updated_at": _now_iso()}},
        )

    async def touch(self, doc_id: str, *, kind: Optional[str] = None, amount: int = 1) -> None:
        update: Dict[str, Any] = {"$set": {"last_seen": time.time()}}
        if kind:
            update["$inc"] = {f"{kind}_done": amount}
        await self.store.update_one(COLL_ACCOUNTS, {"_id": doc_id}, update)

    async def remove(self, doc_id: str) -> int:
        return await self.store.delete_one(COLL_ACCOUNTS, {"_id": doc_id})

    async def stats(self) -> Dict[str, Any]:
        total = await self.count()
        active = await self.count(STATUS_ACTIVE)
        revoked = await self.count(STATUS_REVOKED)
        return {"total": total, "active": active, "revoked": revoked,
                "inactive": max(0, total - active - revoked)}


class AccountRegistry:
    """Live clients <-> database records.

    The panel keeps live ``TelegramClient`` objects in ``tbot.active_clients``
    (keyed by session name). This registry links them to their database rows and
    provides the selection helpers the engagement services need.
    """

    def __init__(self, store: Optional[AccountStore] = None):
        self.store = store or AccountStore()
        self._tbot = None
        self._doc_by_session: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._restore_attempted = False

    # -- wiring ------------------------------------------------------------
    def bind(self, tbot) -> "AccountRegistry":
        self._tbot = tbot
        return self

    @property
    def tbot(self):
        if self._tbot is None:
            from src.runtime import get_bot
            self._tbot = get_bot()
        return self._tbot

    def _active_clients(self) -> Dict[str, Any]:
        tbot = self.tbot
        return getattr(tbot, "active_clients", {}) if tbot is not None else {}

    # -- registration ------------------------------------------------------
    async def register(self, session_name: str, client, owner_id: Optional[int] = None,
                       phone: Optional[str] = None) -> Optional[str]:
        """Store the live client's session in the database and map it."""
        doc = await self.store.save_signed_in(client, phone=phone or session_name,
                                              owner_id=owner_id)
        if doc:
            self._doc_by_session[session_name] = doc["_id"]
        return doc.get("_id") if doc else None

    async def sync_from_live_clients(self, owner_default: Optional[int] = None) -> int:
        """Make sure every live client has a database row (idempotent)."""
        count = 0
        for session_name, client in list(self._active_clients().items()):
            if session_name in self._doc_by_session:
                continue
            try:
                doc = await self.store.save_signed_in(client, phone=session_name,
                                                      owner_id=owner_default)
                if doc:
                    self._doc_by_session[session_name] = doc["_id"]
                    count += 1
            except Exception as exc:
                logger.warning(f"Could not persist account {session_name}: {exc}")
        if count:
            logger.info(f"Persisted {count} live account(s) to the database")
        return count

    async def doc_id_for(self, session_name: str) -> Optional[str]:
        """Registry document id for a session/phone name.

        The session name is sanitized (digits only, e.g. ``919000000002``) while
        the stored phone keeps its ``+`` prefix and may carry ``.session`` — every
        spelling has to resolve, otherwise usage counters and status updates were
        silently skipped for real accounts.
        """
        if session_name in self._doc_by_session:
            return self._doc_by_session[session_name]
        name = str(session_name or "")
        candidates = [name, name.replace(".session", "")]
        digits = "".join(ch for ch in name if ch.isdigit())
        if digits:
            candidates += [digits, f"+{digits}"]
        for candidate in dict.fromkeys(c for c in candidates if c):
            doc = await self.store.find_by_phone(candidate)
            if doc:
                self._doc_by_session[session_name] = doc["_id"]
                return doc["_id"]
        return None

    async def restore_missing_clients(self, owner_default: Optional[int] = None) -> int:
        """Rebuild accounts that exist in MongoDB but not in memory.

        This is the crash-recovery path: session strings make it possible to
        bring every account back without touching the ``.session`` files.
        """
        if self._restore_attempted:
            return 0
        self._restore_attempted = True
        tbot = self.tbot
        if tbot is None:
            return 0
        restored = 0
        docs = await self.store.list_accounts()
        active_names = set(self._active_clients().keys())
        for doc in docs:
            if doc.get("status") == STATUS_REVOKED:
                continue
            session_string = doc.get("session_string")
            if not session_string:
                continue
            phone = doc.get("phone") or doc["_id"]
            session_name = str(phone).lstrip("+").replace(" ", "")
            if session_name in active_names:
                self._doc_by_session.setdefault(session_name, doc["_id"])
                continue
            client = build_client_from_session(session_string, session_name=session_name)
            if client is None:
                continue
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    await self.store.set_status(doc["_id"], STATUS_REVOKED,
                                                "not authorized after restore")
                    continue
                lock = getattr(tbot, "active_clients_lock", None)
                if lock is not None:
                    async with lock:
                        tbot.active_clients[session_name] = client
                else:
                    tbot.active_clients[session_name] = client
                self._doc_by_session[session_name] = doc["_id"]
                await self.store.touch(doc["_id"])
                restored += 1
                logger.info(f"Restored account from database: {session_name}")
            except Exception as exc:
                from src.utils import is_fatal_session_error

                if is_fatal_session_error(exc):
                    logger.warning(
                        f"Account {session_name} is banned/blocked or its session was "
                        f"revoked — marking it revoked: {exc}"
                    )
                    await self.store.set_status(doc["_id"], STATUS_REVOKED, str(exc)[:200])
                else:
                    # A temporary problem (network, sqlite lock, flood) must never
                    # turn into "inactive": the account stays in the pool and the
                    # supervisor retries it on the next cycle.
                    logger.warning(
                        f"Could not restore account {session_name} right now ({exc}) — "
                        f"keeping it in the pool and retrying automatically"
                    )
                    await self.store.set_status(doc["_id"], STATUS_ACTIVE, str(exc)[:200])
                try:
                    await client.disconnect()
                except Exception:
                    pass
        if restored:
            logger.info(f"Recovered {restored} account(s) from the database")
        return restored

    # -- self-healing pool -------------------------------------------------
    async def revive_disconnected(self) -> Dict[str, int]:
        """Reconnect everything that dropped out; log out only on ban/block.

        This is the answer to the production log's "5 reactions from 6 accounts":
        one account lost its connection (sqlite lock after a session file was
        opened twice) and silently disappeared from ``active_clients``. Here it
        is put back instead — connections are re-established, database-only
        accounts are rebuilt from their session string, and an account is only
        ever dropped when Telegram says it is banned/blocked/revoked.
        """
        from src.utils import is_fatal_session_error

        stats = {"checked": 0, "reconnected": 0, "restored": 0, "revoked": 0, "failed": 0}
        active = self._active_clients()
        if not active and self.tbot is None:
            return stats

        for session_name, client in list(active.items()):
            stats["checked"] += 1
            try:
                if client is not None and client.is_connected():
                    continue
                await client.connect()
                if not await client.is_user_authorized():
                    # Not necessarily revoked — Telegram can answer this before the
                    # first request lands; only a fatal (ban/block/revoked) error
                    # may drop an account, so this one stays in the pool.
                    stats["failed"] += 1
                    logger.warning(
                        f"Account {session_name} is not authorized yet after reconnect — "
                        f"keeping it in the pool and retrying"
                    )
                    continue
                stats["reconnected"] += 1
                logger.info(f"Reconnected account {session_name} automatically")
                doc_id = await self.doc_id_for(session_name)
                if doc_id:
                    await self.store.set_status(doc_id, STATUS_ACTIVE, None)
            except Exception as exc:  # noqa: BLE001 - one bad account never stops the loop
                stats["failed"] += 1
                if is_fatal_session_error(exc):
                    stats["revoked"] += 1
                    logger.warning(f"Account {session_name} banned/blocked/revoked — dropping it: {exc}")
                    await self.remove_by_session(session_name, f"fatal: {str(exc)[:120]}")
                    lock = getattr(self.tbot, "active_clients_lock", None)
                    if lock is not None:
                        async with lock:
                            active.pop(session_name, None)
                    else:  # pragma: no cover - defensive
                        active.pop(session_name, None)
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    await self._notify_revoked(session_name, exc)
                else:
                    logger.info(
                        f"Account {session_name} could not reconnect yet ({exc}) — will "
                        f"retry on the next supervisor pass"
                    )

        # Accounts that only exist in the database (crash / lost client).
        try:
            self._restore_attempted = False
            stats["restored"] = await self.restore_missing_clients()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"restore pass failed: {exc}")

        if stats["reconnected"] or stats["restored"] or stats["revoked"]:
            logger.info(
                f"Pool supervisor: {stats['reconnected']} reconnected, "
                f"{stats['restored']} restored, {stats['revoked']} revoked, "
                f"{stats['failed']} still down"
            )
        return stats

    async def _notify_revoked(self, session_name: str, error: Exception) -> None:
        """Tell the operator an account needs a fresh sign-in (ban/block only)."""
        try:
            from src.services.notify import get_notifier
            await get_notifier().to_admins(
                "🚫 <b>Account logged out</b>\n\n"
                f"📱 <code>{session_name}</code> was banned, blocked or its session "
                f"was revoked by Telegram, so it was removed from the pool.\n"
                f"📝 {str(error)[:140]}\n\n"
                "Add it again from <b>Account Management</b> if you still need it."
            )
        except Exception as exc:  # noqa: BLE001 - notice only
            logger.debug(f"revoked-account notice failed: {exc}")

    # -- selection ---------------------------------------------------------
    def live_clients(self) -> List[Tuple[str, Any]]:
        out = []
        for name, client in list(self._active_clients().items()):
            if client is None:
                continue
            try:
                if hasattr(client, "is_connected") and not client.is_connected():
                    continue
            except Exception:
                continue
            out.append((name, client))
        return out

    async def healthy_clients(self) -> List[Tuple[str, Any]]:
        """Live clients that are not marked revoked in the database."""
        revoked = {doc["_id"] for doc in await self.store.list_accounts(status=STATUS_REVOKED)}
        healthy = []
        for name, client in self.live_clients():
            doc_id = await self.doc_id_for(name)
            if doc_id and doc_id in revoked:
                continue
            healthy.append((name, client))
        return healthy

    async def pick(
        self,
        count: int,
        *,
        exclude: Sequence[str] = (),
        strategy: str = "random",
        mark: bool = True,
    ) -> List[Tuple[str, Any]]:
        """Pick up to *count* accounts for an operation.

        ``strategy`` is ``random`` (default, spreads load and looks natural),
        ``first`` (oldest first) or ``rotate`` (least recently used).
        """
        pool = await self.healthy_clients()
        if len(pool) < max(1, int(count)):
            # Fewer accounts connected than asked for: this is exactly how the
            # production log ended up with "5 ok of 5 accounts" while 6 were
            # signed in. Heal the pool first, then take the real number.
            try:
                stats = await self.revive_disconnected()
                if stats.get("reconnected") or stats.get("restored"):
                    pool = await self.healthy_clients()
                    logger.info(
                        f"Account pool healed before the operation: "
                        f"{len(pool)} account(s) live now"
                    )
            except Exception as exc:  # noqa: BLE001 - never block an operation
                logger.debug(f"pool heal before pick failed: {exc}")
        excluded = {str(e) for e in exclude}
        pool = [(n, c) for n, c in pool if n not in excluded and str(n) not in excluded]
        if not pool:
            return []
        count = max(0, int(count))
        if count >= len(pool):
            chosen = pool[:]
        elif strategy == "first":
            chosen = pool[:count]
        elif strategy == "rotate":
            async def last_used(item):
                doc_id = await self.doc_id_for(item[0])
                doc = await self.store.get(doc_id) if doc_id else None
                return (doc or {}).get("last_seen") or 0
            ordered = sorted([(await last_used(i), i) for i in pool], key=lambda x: x[0])
            chosen = [i for _, i in ordered[:count]]
        else:
            chosen = random.sample(pool, count)
        if mark:
            for name, _ in chosen:
                doc_id = await self.doc_id_for(name)
                if doc_id:
                    await self.store.touch(doc_id)
        return chosen

    async def pick_count(self, count: int, **kwargs) -> List[Tuple[str, Any]]:
        return await self.pick(count, **kwargs)

    async def available_count(self) -> int:
        return len(await self.healthy_clients())

    # -- identity helpers --------------------------------------------------
    @staticmethod
    def session_name(client) -> str:
        try:
            from src.utils import get_session_name
            return get_session_name(client) or "unknown"
        except Exception:
            return getattr(getattr(client, "session", None), "filename", "unknown") or "unknown"

    async def remove_by_session(self, session_name: str, revoke_reason: str = "removed") -> None:
        doc_id = await self.doc_id_for(session_name)
        if doc_id:
            await self.store.set_status(doc_id, STATUS_REVOKED, revoke_reason)
        self._doc_by_session.pop(session_name, None)

    async def stats(self) -> Dict[str, Any]:
        base = await self.store.stats()
        base["live"] = len(self.live_clients())
        return base


_registry: Optional[AccountRegistry] = None


def get_account_registry(store: Optional[AccountStore] = None) -> AccountRegistry:
    global _registry
    if _registry is None or store is not None:
        _registry = AccountRegistry(store)
    return _registry


def reset_account_registry() -> None:
    global _registry
    _registry = None
