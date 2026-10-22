"""
Persistence layer.

Every piece of durable state in the panel (signed-in accounts / sessions, panel
users, orders, wallet balances, dynamic settings and the peer access-hash cache)
goes through this module.

Backends
--------
* ``MongoStore``  – MongoDB (primary, survives crashes/restarts on any host)
* ``JsonStore``   – local, crash-safe JSON files (zero-dependency fallback)
* ``MemoryStore`` – in-process only (tests / dry runs)

All three implement the same tiny async API, so the rest of the code base never
has to care which one is active. The store is selected automatically:
``STORAGE_BACKEND=auto`` (default) uses MongoDB whenever ``MONGODB_URI`` is set
and reachable, otherwise it degrades gracefully to the JSON store.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Collection names (single source of truth)
# ---------------------------------------------------------------------------
COLL_USERS = "users"
COLL_ACCOUNTS = "accounts"          # signed-in Telegram accounts (sessions)
COLL_ORDERS = "orders"
COLL_SETTINGS = "settings"
COLL_TRANSACTIONS = "transactions"
COLL_CHANNELS = "channels"          # channels/groups where the bot is installed
COLL_PEERS = "peers"                # access-hash cache for entity resolution
COLL_LOGS = "event_logs"
COLL_COUNTERS = "counters"

ALL_COLLECTIONS = (
    COLL_USERS, COLL_ACCOUNTS, COLL_ORDERS, COLL_SETTINGS, COLL_TRANSACTIONS,
    COLL_CHANNELS, COLL_PEERS, COLL_LOGS, COLL_COUNTERS,
)


class BaseStore:
    """Common interface + query helpers shared by all backends."""

    name = "base"

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:  # pragma: no cover - trivial
        return None

    async def close(self) -> None:  # pragma: no cover - trivial
        return None

    # -- primitives (implemented by subclasses) ----------------------------
    async def insert_one(self, collection: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    async def find_one(self, collection: str, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    async def find(
        self,
        collection: str,
        query: Optional[Dict[str, Any]] = None,
        *,
        sort: Optional[List[tuple]] = None,
        limit: int = 0,
        projection: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    async def update_one(
        self,
        collection: str,
        query: Dict[str, Any],
        update: Dict[str, Any],
        *,
        upsert: bool = False,
    ) -> int:
        raise NotImplementedError

    async def delete_one(self, collection: str, query: Dict[str, Any]) -> int:
        raise NotImplementedError

    async def count(self, collection: str, query: Optional[Dict[str, Any]] = None) -> int:
        raise NotImplementedError

    # -- convenience -------------------------------------------------------
    async def upsert_one(self, collection: str, query: Dict[str, Any], doc: Dict[str, Any]) -> Dict[str, Any]:
        """Insert *doc* (merged with query) if missing, else update it."""
        existing = await self.find_one(collection, query)
        if existing is None:
            payload = {**query, **doc}
            await self.insert_one(collection, payload)
            return payload
        await self.update_one(collection, query, {"$set": doc}, upsert=True)
        return {**existing, **doc}

    async def get_or_create(self, collection: str, doc_id: Any, defaults: Dict[str, Any]) -> Dict[str, Any]:
        found = await self.find_one(collection, {"_id": doc_id})
        if found:
            return found
        payload = {"_id": doc_id, **defaults}
        await self.insert_one(collection, payload)
        return payload

    async def log_event(self, kind: str, **fields: Any) -> None:
        try:
            await self.insert_one(COLL_LOGS, {
                "kind": kind, "ts": time.time(), "created_at": _utcnow_iso(), **fields,
            })
        except Exception as exc:  # never let logging break a flow
            logger.debug(f"log_event failed: {exc}")


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Query matching (shared by the memory/JSON backends)
# ---------------------------------------------------------------------------
def _match_condition(value: Any, condition: Any) -> bool:
    if isinstance(condition, dict):
        for op, operand in condition.items():
            if op == "$eq":
                if value != operand:
                    return False
            elif op == "$ne":
                if value == operand:
                    return False
            elif op == "$gt":
                if value is None or not value > operand:
                    return False
            elif op == "$gte":
                if value is None or not value >= operand:
                    return False
            elif op == "$lt":
                if value is None or not value < operand:
                    return False
            elif op == "$lte":
                if value is None or not value <= operand:
                    return False
            elif op == "$in":
                if value not in (operand or []):
                    return False
            elif op == "$nin":
                if value in (operand or []):
                    return False
            elif op == "$exists":
                exists = value is not None
                if bool(operand) != exists:
                    return False
            elif op == "$regex":
                import re
                flags = re.IGNORECASE if condition.get("$options", "").find("i") >= 0 else 0
                if value is None or not re.search(operand, str(value), flags):
                    return False
            else:
                logger.warning(f"Unsupported query operator {op!r}; ignoring")
        return True
    return value == condition


def _matches(doc: Dict[str, Any], query: Optional[Dict[str, Any]]) -> bool:
    if not query:
        return True
    for key, condition in query.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in condition):
                return False
            continue
        if key == "$and":
            if not all(_matches(doc, sub) for sub in condition):
                return False
            continue
        value = doc
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                value = None
                break
        if not _match_condition(value, condition):
            return False
    return True


def _apply_update(doc: Dict[str, Any], update: Dict[str, Any]) -> bool:
    """Apply a Mongo-style update dict in place. Returns True if changed."""
    changed = False
    for op, fields in update.items():
        if op in ("$set", "$setOnInsert"):
            for path, val in fields.items():
                _set_path(doc, path, val)
                changed = True
        elif op == "$inc":
            for path, val in fields.items():
                _set_path(doc, path, (_get_path(doc, path) or 0) + val)
                changed = True
        elif op == "$push":
            for path, val in fields.items():
                arr = _get_path(doc, path)
                if not isinstance(arr, list):
                    arr = []
                    _set_path(doc, path, arr)
                if isinstance(val, dict) and "$each" in val:
                    arr.extend(val["$each"])
                    if val.get("$slice"):
                        del arr[:-int(val["$slice"])]
                else:
                    arr.append(val)
                changed = True
        elif op == "$addToSet":
            # Match Mongo's atomic set insertion for the JSON/memory backends.
            # This is used for channel sponsors, where a read/append/write could
            # otherwise lose one user's association when two orders complete at
            # the same time.
            for path, val in fields.items():
                arr = _get_path(doc, path)
                if not isinstance(arr, list):
                    arr = []
                    _set_path(doc, path, arr)
                values = val.get("$each", []) if isinstance(val, dict) and "$each" in val else [val]
                for item in values:
                    if item not in arr:
                        arr.append(item)
                        changed = True
        elif op == "$pull":
            for path, val in fields.items():
                arr = _get_path(doc, path)
                if isinstance(arr, list):
                    doc_arr = [x for x in arr if not (x == val if not isinstance(val, dict) else _matches(x, val))]
                    _set_path(doc, path, doc_arr)
                    changed = True
        elif op == "$unset":
            for path in fields:
                _set_path(doc, path, None)
                changed = True
        elif op == "$max":
            for path, val in fields.items():
                cur = _get_path(doc, path)
                if cur is None or val > cur:
                    _set_path(doc, path, val)
                    changed = True
        elif op.startswith("$"):
            logger.warning(f"Unsupported update operator {op!r}; ignoring")
        else:  # plain replacement of top-level fields
            doc[op] = fields
            changed = True
    return changed


def _get_path(doc: Dict[str, Any], path: str) -> Any:
    value: Any = doc
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _set_path(doc: Dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor = doc
    for part in parts[:-1]:
        nxt = cursor.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[part] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _deep_copy(doc: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(doc, default=str))


def _sort_docs(docs: List[Dict[str, Any]], sort: Optional[List[tuple]]) -> List[Dict[str, Any]]:
    if not sort:
        return docs
    if isinstance(sort, tuple) and len(sort) == 2 and isinstance(sort[1], int):
        sort = [sort]          # tolerate sort=("field", -1)
    ordered = list(docs)
    # Apply sort keys from last to first so the first key wins (stable sort).
    for key, direction in reversed(list(sort)):
        ordered.sort(key=lambda d: (_get_path(d, key) is None, _get_path(d, key)), reverse=direction < 0)
    return ordered


# ---------------------------------------------------------------------------
# In-memory + JSON backed store
# ---------------------------------------------------------------------------
class JsonStore(BaseStore):
    """Dict-backed store with optional crash-safe JSON persistence.

    Writes go through a temp file + ``os.replace`` so a crash mid-write can
    never truncate the data file (same guarantee the config manager uses).
    """

    name = "json"

    def __init__(self, data_dir: Optional[str] = None, persist: bool = True):
        from src.config import DATA_DIR as CFG_DATA_DIR
        self.data_dir = data_dir or CFG_DATA_DIR
        self.persist = persist
        self._data: Dict[str, Dict[str, Dict[str, Any]]] = {c: {} for c in ALL_COLLECTIONS}
        self._lock = threading.RLock()
        self._dirty = False
        if self.persist:
            os.makedirs(self.data_dir, exist_ok=True)
            self._load()

    # -- file helpers ------------------------------------------------------
    def _path(self, collection: str) -> str:
        return os.path.join(self.data_dir, f"{collection}.json")

    def _load(self) -> None:
        for collection in ALL_COLLECTIONS:
            path = self._path(collection)
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self._data[collection] = {str(k): v for k, v in raw.items()}
            except (json.JSONDecodeError, OSError) as exc:
                logger.error(f"Could not load {path}: {exc} (starting empty)")

    def _flush(self, collection: str) -> None:
        if not self.persist:
            return
        path = self._path(collection)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data[collection], fh, indent=2, ensure_ascii=False, default=str)
                os.replace(tmp, path)
            except BaseException:
                if os.path.exists(tmp):
                    os.remove(tmp)
                raise
        except OSError as exc:
            logger.error(f"Failed to persist {collection}: {exc}")

    # -- API ---------------------------------------------------------------
    async def connect(self) -> None:
        logger.info(f"Storage backend ready: JSON store ({self.data_dir})")

    async def insert_one(self, collection: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            store = self._data.setdefault(collection, {})
            doc = _deep_copy(doc)
            key = doc.get("_id")
            if key is None:
                key = _auto_id()
                doc["_id"] = key
            key = str(key)
            if key in store:
                raise ValueError(f"duplicate key: {key}")
            store[key] = doc
            self._flush(collection)
            return _deep_copy(doc)

    async def find_one(self, collection: str, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            for doc in self._data.get(collection, {}).values():
                if _matches(doc, query):
                    return _deep_copy(doc)
        return None

    async def find(
        self,
        collection: str,
        query: Optional[Dict[str, Any]] = None,
        *,
        sort: Optional[List[tuple]] = None,
        limit: int = 0,
        projection: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            docs = [d for d in self._data.get(collection, {}).values() if _matches(d, query)]
        docs = _sort_docs(docs, sort)
        if limit:
            docs = docs[:limit]
        result = [_deep_copy(d) for d in docs]
        if projection:
            keep = {k for k, v in projection.items() if v}
            result = [{k: v for k, v in d.items() if k in keep} for d in result]
        return result

    async def update_one(
        self,
        collection: str,
        query: Dict[str, Any],
        update: Dict[str, Any],
        *,
        upsert: bool = False,
    ) -> int:
        with self._lock:
            store = self._data.setdefault(collection, {})
            for key, doc in store.items():
                if _matches(doc, query):
                    _apply_update(doc, update)
                    self._flush(collection)
                    return 1
            if not upsert:
                return 0
            base = {}
            set_on_insert = update.get("$setOnInsert", {})
            base.update({k: v for k, v in query.items() if not isinstance(v, dict)})
            base.update(set_on_insert)
            base.update({k: v for k, v in update.get("$set", {}).items()})
            doc = {"_id": base.pop("_id", _auto_id()), **base}
            for op, fields in update.items():
                if op not in ("$set", "$setOnInsert"):
                    _apply_update(doc, {op: fields})
            store[str(doc["_id"])] = doc
            self._flush(collection)
            return 1

    async def delete_one(self, collection: str, query: Dict[str, Any]) -> int:
        with self._lock:
            store = self._data.setdefault(collection, {})
            for key, doc in list(store.items()):
                if _matches(doc, query):
                    del store[key]
                    self._flush(collection)
                    return 1
        return 0

    async def count(self, collection: str, query: Optional[Dict[str, Any]] = None) -> int:
        with self._lock:
            return sum(1 for d in self._data.get(collection, {}).values() if _matches(d, query))

    # -- introspection (used by admin panel / tests) -----------------------
    def snapshot(self) -> Dict[str, int]:
        return {c: len(v) for c, v in self._data.items()}


class MemoryStore(JsonStore):
    """In-process store, nothing touches the disk (used by tests)."""

    name = "memory"

    def __init__(self, data_dir: Optional[str] = None):
        super().__init__(data_dir=data_dir, persist=False)


def _auto_id() -> str:
    return f"id_{int(time.time() * 1000000)}_{os.urandom(4).hex()}"


# ---------------------------------------------------------------------------
# MongoDB backed store
# ---------------------------------------------------------------------------
class MongoStore(BaseStore):
    """MongoDB store.

    Uses ``pymongo.AsyncMongoClient`` when available (pymongo >= 4.9) and falls
    back to running the sync client in a worker thread otherwise, so the event
    loop is never blocked.
    """

    name = "mongo"

    def __init__(self, uri: str, db_name: str, server_selection_timeout_ms: int = 4000):
        self.uri = uri
        self.db_name = db_name
        self.server_selection_timeout_ms = server_selection_timeout_ms
        self._client = None
        self._db = None
        self._async = None

    async def connect(self) -> None:
        from pymongo import AsyncMongoClient  # pymongo >= 4.9
        self._client = AsyncMongoClient(
            self.uri,
            serverSelectionTimeoutMS=self.server_selection_timeout_ms,
            tz_aware=True,
        )
        self._db = self._client[self.db_name]
        await self._db.command("ping")
        await self._ensure_indexes()
        logger.info(f"Storage backend ready: MongoDB db={self.db_name}")

    async def _ensure_indexes(self) -> None:
        try:
            await self._db[COLL_USERS].create_index("role")
            await self._db[COLL_USERS].create_index("last_seen")
            await self._db[COLL_ACCOUNTS].create_index("owner_id")
            await self._db[COLL_ACCOUNTS].create_index("phone")
            await self._db[COLL_ORDERS].create_index([("user_id", 1), ("created_at", -1)])
            await self._db[COLL_ORDERS].create_index("status")
            await self._db[COLL_TRANSACTIONS].create_index([("user_id", 1), ("created_at", -1)])
            await self._db[COLL_CHANNELS].create_index("owner_id")
            await self._db[COLL_PEERS].create_index("updated_at")
            await self._db[COLL_LOGS].create_index("ts")
        except Exception as exc:
            logger.warning(f"Index creation skipped: {exc}")

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as exc:
                logger.debug(f"Error closing Mongo client: {exc}")
            self._client = None
            self._db = None

    # -- primitives --------------------------------------------------------
    async def insert_one(self, collection: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(doc)
        payload.setdefault("created_at", _utcnow_iso())
        result = await self._db[collection].insert_one(payload)
        payload.setdefault("_id", result.inserted_id)
        return payload

    async def find_one(self, collection: str, query: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return await self._db[collection].find_one(dict(query))

    async def find(
        self,
        collection: str,
        query: Optional[Dict[str, Any]] = None,
        *,
        sort: Optional[List[tuple]] = None,
        limit: int = 0,
        projection: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        cursor = self._db[collection].find(dict(query or {}), dict(projection or {}))
        if sort:
            cursor = cursor.sort(list(sort))
        if limit:
            cursor = cursor.limit(limit)
        return [doc async for doc in cursor]

    async def update_one(
        self,
        collection: str,
        query: Dict[str, Any],
        update: Dict[str, Any],
        *,
        upsert: bool = False,
    ) -> int:
        result = await self._db[collection].update_one(dict(query), dict(update), upsert=upsert)
        return int(result.modified_count or result.upserted_id is not None)

    async def delete_one(self, collection: str, query: Dict[str, Any]) -> int:
        result = await self._db[collection].delete_one(dict(query))
        return int(result.deleted_count)

    async def count(self, collection: str, query: Optional[Dict[str, Any]] = None) -> int:
        return int(await self._db[collection].count_documents(dict(query or {})))

    # -- atomic helpers ----------------------------------------------------
    async def atomic_inc(
        self,
        collection: str,
        query: Dict[str, Any],
        increments: Dict[str, float],
        *,
        min_values: Optional[Dict[str, float]] = None,
    ) -> Optional[Dict[str, Any]]:
        """$inc guarded by a filter so balances can never go negative."""
        import pymongo
        filters = dict(query)
        for path, floor in (min_values or {}).items():
            filters[path] = {"$gte": floor}
        try:
            return await self._db[collection].find_one_and_update(
                filters, {"$inc": increments}, return_document=pymongo.ReturnDocument.AFTER
            )
        except Exception as exc:
            logger.error(f"atomic_inc failed on {collection}: {exc}")
            return None

    async def snapshot(self) -> Dict[str, int]:
        out = {}
        for collection in ALL_COLLECTIONS:
            try:
                out[collection] = await self.count(collection)
            except Exception:
                out[collection] = -1
        return out


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------
_store: Optional[BaseStore] = None
_store_lock = asyncio.Lock() if False else threading.Lock()  # lock created lazily-safe


def _backend_choice() -> str:
    """Which store to use: 'memory' | 'mongo' | 'json'."""
    from src.config import STORAGE_BACKEND, MONGODB_URI
    choice = (STORAGE_BACKEND or "auto").lower()
    if choice == "auto":
        return "mongo" if MONGODB_URI else "json"
    return choice


def _strict_mongo() -> bool:
    """True only when the operator explicitly asked for MongoDB *only*.

    ``STORAGE_BACKEND=auto`` (the default) must never crash because a cloud
    database is temporarily unreachable — it degrades to the JSON store and
    logs the reason. Only ``STORAGE_BACKEND=mongo`` is allowed to fail hard.
    """
    from src.config import STORAGE_BACKEND
    return (STORAGE_BACKEND or "").lower() in ("mongo", "mongodb")


async def init_store(force: Optional[str] = None) -> BaseStore:
    """Create (once) and return the process-wide store instance."""
    global _store
    if _store is not None and force is None:
        return _store

    from src.config import MONGODB_URI, MONGODB_DB, DATA_DIR
    choice = (force or _backend_choice()).lower()

    if choice == "memory":
        _store = MemoryStore()
        await _store.connect()
        return _store

    if choice in ("mongo", "mongodb"):
        if not MONGODB_URI:
            if _strict_mongo():
                raise RuntimeError(
                    "STORAGE_BACKEND=mongo but no MongoDB URI is configured "
                    "(set MONGODB_URI / MONGO_URL)"
                )
            logger.warning("MongoDB requested but no URI is set; using the JSON store")
        else:
            # Constructing the client can already fail (bad URI, DNS/SRV lookup
            # for an Atlas host, missing dnspython), so it lives inside the try
            # together with the connection attempt.
            try:
                candidate = MongoStore(MONGODB_URI, MONGODB_DB)
                await candidate.connect()
                _store = candidate
                return _store
            except ImportError as exc:
                if _strict_mongo():
                    raise
                logger.error(f"pymongo not installed ({exc}); falling back to JSON store")
            except Exception as exc:
                if _strict_mongo():
                    raise
                logger.error(
                    "MongoDB unreachable (%s). Falling back to the JSON store so the bot "
                    "keeps running — fix the MONGODB_URI / MONGO_URL value to re-enable "
                    "durable cloud storage.", exc,
                )

    _store = JsonStore(DATA_DIR)
    await _store.connect()
    return _store


def get_store_sync() -> BaseStore:
    """Return the already-initialised store (or a lazily created memory store).

    Handlers call this inside async code; ``init_store`` runs at startup so in
    practice the store is always ready. The lazy path keeps unit tests that
    instantiate a handler directly from exploding.
    """
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store


async def get_store() -> BaseStore:
    return await init_store()


async def reset_store(store: Optional[BaseStore] = None) -> None:
    """Test helper: swap/reset the singleton."""
    global _store
    _store = store
