"""
Live MongoDB integration tests.

Skipped unless ``TEST_MONGODB_URI`` is set, e.g.

    TEST_MONGODB_URI="mongodb://127.0.0.1:27017" pytest tests/test_mongodb_live.py

They prove the durability promises on a real server: accounts (with session
strings), wallets, orders, settings and the peer access-hash cache survive a
process restart, and money movements stay atomic.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from src.db.store import MongoStore

MONGO_URI = os.getenv("TEST_MONGODB_URI", "")
pytestmark = pytest.mark.skipif(not MONGO_URI, reason="TEST_MONGODB_URI not configured")

TEST_DB = os.getenv("TEST_MONGODB_DB", "telegram_panel_live_test")


@pytest_asyncio.fixture
async def mongo_store():
    store = MongoStore(MONGO_URI, TEST_DB, server_selection_timeout_ms=4000)
    await store.connect()
    # clean slate
    for collection in ("users", "accounts", "orders", "settings", "transactions",
                       "channels", "peers", "event_logs"):
        for doc in await store.find(collection, {}):
            await store.delete_one(collection, {"_id": doc["_id"]})
    yield store
    await store.close()


@pytest.mark.asyncio
class TestMongoPersistence:
    async def test_account_session_strings_round_trip(self, mongo_store):
        from src.services.accounts import AccountStore
        from tests.fakes import FakeTelegramClient

        accounts = AccountStore(mongo_store)
        client = FakeTelegramClient("phone1", user_id=555)
        client._me = type("Me", (), {
            "id": 555, "username": "tester", "first_name": "Test", "last_name": "",
            "phone": "+15550000011",
        })()
        saved = await accounts.save_signed_in(client, phone="+15550000011", owner_id=1)
        doc_id = saved["_id"]
        await mongo_store.close()

        reopened = MongoStore(MONGO_URI, TEST_DB)
        await reopened.connect()
        try:
            doc = await AccountStore(reopened).get(doc_id)
            assert doc["session_string"]
            assert doc["tg_user_id"] == 555
            assert doc["status"] == "active"
        finally:
            await reopened.close()

    async def test_wallet_and_orders_survive_restart(self, mongo_store):
        from src.services.orders import OrderService
        from src.services.settings import SettingsService
        from src.services.users import UserService

        settings = SettingsService(mongo_store)
        users = UserService(mongo_store, settings)
        orders = OrderService(mongo_store, settings, users)

        await settings.set_path("reactions.free_account_limit", 8)
        await users.credit(9001, 1000, "topup")
        plan = await orders.quote_plan("reaction", "react_200")
        order = await orders.create_order(9001, "reaction", {"link": "x"}, plan=plan)

        reopened = MongoStore(MONGO_URI, TEST_DB)
        await reopened.connect()
        try:
            settings2 = SettingsService(reopened)
            users2 = UserService(reopened, settings2)
            orders2 = OrderService(reopened, settings2, users2)

            assert (await settings2.get(refresh=True))["reactions"]["free_account_limit"] == 8
            assert await users2.balance(9001) == 851  # 1000 - 149
            restored = await orders2.get(order["_id"])
            assert restored and restored["price"] == 149
            txns = await users2.transactions(9001)
            assert {t["type"] for t in txns} >= {"credit", "debit"}
        finally:
            await reopened.close()

    async def test_atomic_debit_guard_on_real_server(self, mongo_store):
        from src.services.settings import SettingsService
        from src.services.users import UserService

        users = UserService(mongo_store, SettingsService(mongo_store))
        await users.credit(9002, 100, "topup")
        # 20 concurrent debits of 10 against a balance of 100: exactly 10 succeed
        results = await asyncio_gather(users.debit(9002, 10, "order") for _ in range(20))
        assert sum(1 for r in results if r) == 10
        assert await users.balance(9002) == 0

    async def test_peer_access_hash_cache_survives_restart(self, mongo_store):
        from src.peers import PeerCache
        from tests.fakes import FakeTelegramClient, make_channel

        channel = make_channel(3852827894)
        client = FakeTelegramClient("phone1")
        await PeerCache(mongo_store).put(client, channel)

        reopened = MongoStore(MONGO_URI, TEST_DB)
        await reopened.connect()
        try:
            fresh = PeerCache(reopened)
            assert await fresh.get(client, 3852827894) == channel.access_hash
        finally:
            await reopened.close()

    async def test_counters_and_quotas(self, mongo_store):
        from src.services.settings import SettingsService
        from src.services.users import UserService

        users = UserService(mongo_store, SettingsService(mongo_store))
        await users.get_or_create(9003)
        for _ in range(3):
            await users.bump_counter(9003, "free_react")
        quota = await users.check_daily_quota(9003, "free_react", 3)
        assert not quota["ok"] and quota["used"] == 3

    async def test_aggregate_like_queries(self, mongo_store):
        for index in range(5):
            await mongo_store.insert_one("orders", {"_id": f"o{index}", "status": "completed",
                                                    "user_id": 1, "price": 10.0})
        assert await mongo_store.count("orders", {"status": "completed"}) == 5
        top = await mongo_store.find("orders", {"user_id": 1}, sort=[("price", -1)], limit=2)
        assert len(top) == 2


async def asyncio_gather(iterable):
    import asyncio
    return await asyncio.gather(*list(iterable))


def pytest_configure(config):  # pragma: no cover - keeps async fixtures honest
    config.addinivalue_line("markers", "asyncio: run the test in an event loop")
