"""Database package: pluggable persistence (MongoDB / JSON / memory)."""
from __future__ import annotations

from src.db.store import (  # noqa: F401
    ALL_COLLECTIONS,
    COLL_ACCOUNTS,
    COLL_CHANNELS,
    COLL_COUNTERS,
    COLL_LOGS,
    COLL_ORDERS,
    COLL_PEERS,
    COLL_SETTINGS,
    COLL_TRANSACTIONS,
    COLL_USERS,
    BaseStore,
    JsonStore,
    MemoryStore,
    MongoStore,
    get_store,
    get_store_sync,
    init_store,
    reset_store,
)

__all__ = [
    "ALL_COLLECTIONS", "BaseStore", "JsonStore", "MemoryStore", "MongoStore",
    "get_store", "get_store_sync", "init_store", "reset_store",
    "COLL_USERS", "COLL_ACCOUNTS", "COLL_ORDERS", "COLL_SETTINGS",
    "COLL_TRANSACTIONS", "COLL_CHANNELS", "COLL_PEERS", "COLL_LOGS", "COLL_COUNTERS",
]
