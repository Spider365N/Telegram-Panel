"""Storage backends, dynamic settings, wallets, orders and payments."""
from __future__ import annotations

import json
import os

import pytest

from src.db.store import JsonStore, MemoryStore, _matches, _apply_update
from tests.fakes import build_stack


class TestQueryEngine:
    def test_matches_nested_paths(self):
        doc = {"user": {"plan": {"type": "premium"}}, "balance": 10}
        assert _matches(doc, {"user.plan.type": "premium"})
        assert _matches(doc, {"balance": {"$gte": 5}})
        assert not _matches(doc, {"balance": {"$lt": 5}})

    def test_matches_operators(self):
        doc = {"status": "active", "tags": ["a", "b"]}
        assert _matches(doc, {"status": {"$in": ["active", "x"]}})
        assert _matches(doc, {"status": {"$ne": "revoked"}})
        assert _matches(doc, {"$or": [{"status": "nope"}, {"status": "active"}]})

    def test_apply_update_operators(self):
        doc = {"a": 1, "list": [1]}
        _apply_update(doc, {"$set": {"b.c": 5}, "$inc": {"a": 2}, "$push": {"list": 9}})
        assert doc["a"] == 3 and doc["b"]["c"] == 5 and doc["list"] == [1, 9]
        _apply_update(doc, {"$unset": {"b": ""}})
        assert doc["b"] is None


@pytest.mark.asyncio
class TestStores:
    async def test_memory_store_crud(self):
        store = MemoryStore()
        await store.insert_one("users", {"_id": 1, "name": "a"})
        assert (await store.find_one("users", {"_id": 1}))["name"] == "a"
        await store.update_one("users", {"_id": 1}, {"$set": {"name": "b"}})
        assert (await store.find_one("users", {"_id": 1}))["name"] == "b"
        assert await store.count("users") == 1
        await store.delete_one("users", {"_id": 1})
        assert await store.count("users") == 0

    async def test_upsert(self):
        store = MemoryStore()
        await store.update_one("settings", {"_id": "global"}, {"$set": {"data": {"x": 1}}}, upsert=True)
        doc = await store.find_one("settings", {"_id": "global"})
        assert doc["data"] == {"x": 1}

    async def test_json_store_persists_across_instances(self, tmp_path):
        store = JsonStore(str(tmp_path))
        await store.insert_one("accounts", {"_id": "a1", "phone": "+9111"})
        reopened = JsonStore(str(tmp_path))
        doc = await reopened.find_one("accounts", {"_id": "a1"})
        assert doc and doc["phone"] == "+9111"
        # atomic write: the file on disk is valid JSON
        with open(os.path.join(str(tmp_path), "accounts.json"), encoding="utf-8") as fh:
            assert json.load(fh)["a1"]["phone"] == "+9111"

    async def test_find_sort_and_limit(self):
        store = MemoryStore()
        for i in range(5):
            await store.insert_one("orders", {"_id": str(i), "created_at": f"2026-10-0{i}"})
        docs = await store.find("orders", sort=[("created_at", -1)], limit=2)
        assert [d["_id"] for d in docs] == ["4", "3"]


@pytest.mark.asyncio
class TestSettingsService:
    async def test_defaults_and_edits(self):
        stack = await build_stack()
        settings = await stack.settings.get()
        assert settings["reactions"]["free_account_limit"] == 10
        await stack.settings.set_path("reactions.free_account_limit", 4)
        assert (await stack.settings.get(refresh=True))["reactions"]["free_account_limit"] == 4

    async def test_only_the_delta_is_stored(self):
        stack = await build_stack()
        await stack.settings.set_path("views.free_quantity", 250)
        raw = await stack.store.find_one("settings", {"_id": "global"})
        assert raw["data"]["views"]["free_quantity"] == 250
        assert "free_daily_orders" not in raw["data"].get("views", {})

    async def test_plan_crud(self):
        stack = await build_stack()
        plans = await stack.settings.plans("views")
        plans.append({"id": "views_custom_1", "title": "Custom", "quantity": 250, "price": 25})
        await stack.settings.update_section("views", {"plans": plans})
        assert (await stack.settings.get_plan("views", "views_custom_1"))["price"] == 25

    async def test_emoji_helpers(self):
        from src.services.settings import DEFAULT_POSITIVE_EMOJIS

        stack = await build_stack()
        assert "👍" in await stack.settings.emojis()
        # supported emoji are kept, unsupported ones (Telegram rejects them)
        # are dropped instead of breaking every reaction
        await stack.settings.set_path("reactions.emojis", ["🔥", "🚀", "✅"])
        assert await stack.settings.emojis() == ["🔥"]
        await stack.settings.set_path("reactions.emojis", ["🚀"])
        assert await stack.settings.emojis() == list(DEFAULT_POSITIVE_EMOJIS)
        # and a stored broken list is repaired in the database itself
        await stack.settings.set_path("reactions.emojis", ["👍", "✅"])
        assert await stack.settings.repair_reaction_emojis() == ["👍"]
        assert (await stack.settings.get(refresh=True))["reactions"]["emojis"] == ["👍"]


@pytest.mark.asyncio
class TestUsersAndWallet:
    async def test_registration_and_roles(self, monkeypatch):
        stack = await build_stack()
        user = await stack.users.get_or_create(111, "u", "User")
        assert user["role"] == "user"
        await stack.users.set_role(111, "admin")
        assert await stack.users.is_admin(111)

    async def test_owner_detection(self, monkeypatch):
        import src.config as config_module
        monkeypatch.setattr(config_module, "OWNER_ID", 42)
        stack = await build_stack()
        assert await stack.users.is_owner(42)
        await stack.users.get_or_create(42)
        assert await stack.users.role_of(42) == "owner"
        assert await stack.users.is_premium(42)  # owner is unlimited

    async def test_wallet_credit_debit_and_guardrails(self):
        stack = await build_stack()
        await stack.users.credit(222, 100, "topup")
        assert await stack.users.balance(222) == 100
        assert await stack.users.debit(222, 40, "order")
        assert await stack.users.balance(222) == 60
        assert not await stack.users.debit(222, 1000, "too much")
        assert await stack.users.balance(222) == 60

    async def test_transactions_are_recorded(self):
        stack = await build_stack()
        await stack.users.credit(333, 50, "topup")
        await stack.users.debit(333, 10, "order")
        txns = await stack.users.transactions(333)
        assert {t["type"] for t in txns} == {"credit", "debit"}

    async def test_daily_quota(self):
        stack = await build_stack()
        await stack.users.get_or_create(444)
        assert (await stack.users.check_daily_quota(444, "free_react", 2))["ok"]
        await stack.users.bump_counter(444, "free_react")
        await stack.users.bump_counter(444, "free_react")
        quota = await stack.users.check_daily_quota(444, "free_react", 2)
        assert not quota["ok"] and quota["remaining"] == 0

    async def test_effective_limits_free_vs_premium(self):
        stack = await build_stack()
        await stack.users.get_or_create(555)
        free = await stack.users.effective_limits(555)
        assert free["premium"] is False and free["comment_accounts"] == 0
        await stack.users.grant_premium(555, days=30, accounts=20)
        premium = await stack.users.effective_limits(555)
        assert premium["premium"] is True
        assert premium["accounts"] == 20
        assert premium["comment_accounts"] > 0

    async def test_expired_premium_falls_back_to_free(self):
        stack = await build_stack()
        await stack.users.get_or_create(556)
        await stack.users.grant_premium(556, days=1)
        user = await stack.users.get_user(556)
        user["plan"]["expires_at"] = 1  # long past
        await stack.store.update_one("users", {"_id": 556}, {"$set": {"plan": user["plan"]}})
        assert (await stack.users.plan_of(556))["type"] == "free"


@pytest.mark.asyncio
class TestOrders:
    async def test_order_charges_wallet(self):
        stack = await build_stack()
        await stack.users.credit(600, 500, "topup")
        plan = await stack.orders.quote_plan("reaction", "react_200")
        order = await stack.orders.create_order(600, "reaction", {"link": "x"}, plan=plan)
        assert order["price"] == 149
        assert await stack.users.balance(600) == 351
        assert order["status"] == "pending"

    async def test_insufficient_balance_is_rejected(self):
        stack = await build_stack()
        plan = await stack.orders.quote_plan("views", "views_1k")
        with pytest.raises(ValueError):
            await stack.orders.create_order(601, "views", {"link": "x"}, plan=plan)

    async def test_failure_refunds(self):
        stack = await build_stack()
        await stack.users.credit(602, 200, "topup")
        plan = await stack.orders.quote_plan("views", "views_1k")
        order = await stack.orders.create_order(602, "views", {"link": "x"}, plan=plan)
        refunded = await stack.orders.fail(order["_id"], "all attempts failed")
        assert refunded["status"] == "refunded"
        assert await stack.users.balance(602) == 200

    async def test_refund_can_be_disabled(self):
        stack = await build_stack()
        await stack.settings.set_path("orders.refund_on_failure", False)
        await stack.users.credit(603, 200, "topup")
        plan = await stack.orders.quote_plan("views", "views_1k")
        order = await stack.orders.create_order(603, "views", {"link": "x"}, plan=plan)
        failed = await stack.orders.fail(order["_id"], "boom")
        assert failed["status"] == "failed"
        assert await stack.users.balance(603) == 121

    async def test_free_order_costs_nothing(self):
        stack = await build_stack()
        order = await stack.orders.create_order(
            604, "reaction", {"link": "x"}, plan={"plan_id": "free", "price": 0, "quantity": 5},
            charge=False,
        )
        assert order["paid_with"] == "free"
        assert await stack.users.balance(604) == 0

    async def test_active_order_limit(self):
        stack = await build_stack()
        await stack.settings.set_path("orders.max_active_per_user", 1)
        await stack.users.credit(605, 1000, "topup")
        plan = await stack.orders.quote_plan("views", "views_1k")
        await stack.orders.create_order(605, "views", {"link": "x"}, plan=plan)
        with pytest.raises(ValueError):
            await stack.orders.create_order(605, "views", {"link": "y"}, plan=plan)

    async def test_cancel_refunds(self):
        stack = await build_stack()
        await stack.users.credit(606, 1000, "topup")
        plan = await stack.orders.quote_plan("views", "views_1k")
        order = await stack.orders.create_order(606, "views", {"link": "x"}, plan=plan)
        cancelled = await stack.orders.cancel(order["_id"])
        assert cancelled["status"] == "refunded"
        with pytest.raises(ValueError):
            await stack.orders.cancel(order["_id"])


@pytest.mark.asyncio
class TestPayments:
    async def test_topup_flow(self):
        stack = await build_stack()
        request = await stack.payments.create_topup(700, 100, "UTR12345678")
        assert request["status"] == "pending"
        assert len(await stack.payments.pending_topups()) == 1
        await stack.payments.approve_topup(request["_id"], admin_id=1)
        assert await stack.users.balance(700) == 100
        assert await stack.payments.pending_topups() == []

    async def test_duplicate_utr_rejected(self):
        stack = await build_stack()
        await stack.payments.create_topup(701, 100, "SAME-UTR")
        with pytest.raises(ValueError):
            await stack.payments.create_topup(702, 100, "SAME-UTR")

    async def test_minimum_and_validation(self):
        stack = await build_stack()
        with pytest.raises(ValueError):
            await stack.payments.create_topup(703, 1, "UTR12345678")
        with pytest.raises(ValueError):
            await stack.payments.create_topup(703, 500, "x")

    async def test_reject_topup(self):
        stack = await build_stack()
        request = await stack.payments.create_topup(704, 100, "UTRREJECT1")
        await stack.payments.reject_topup(request["_id"], "fake")
        assert await stack.users.balance(704) == 0

    async def test_premium_purchase_from_wallet(self):
        stack = await build_stack()
        await stack.users.credit(705, 500, "topup")
        plan = (await stack.payments.premium_plans())[0]
        outcome = await stack.payments.buy_premium(705, plan["id"])
        assert outcome["granted"]["type"] == "premium"
        assert await stack.users.balance(705) == 500 - plan["price"]
        assert await stack.users.is_premium(705)

    async def test_premium_without_balance(self):
        stack = await build_stack()
        with pytest.raises(ValueError):
            await stack.payments.buy_premium(706, (await stack.payments.premium_plans())[0]["id"])
