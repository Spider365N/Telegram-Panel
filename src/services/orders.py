"""
Order lifecycle: quoting, creation, status tracking and refunds.

An order is a durable work item stored in MongoDB::

    {
      _id: "ord_...", user_id: 123, kind: "reaction"|"views"|"members"|...,
      plan_id: "react_200", quantity: 200, price: 149, paid_with: "wallet"|"free",
      status: "pending"|"processing"|"completed"|"partial"|"failed"|"cancelled"|"refunded",
      payload: {...},  result: {...},  progress: "…",
      balance_before, created_at, updated_at, started_at, finished_at
    }

Money is only deducted after the price is quoted from the *current* settings
document, and every failure path refunds automatically (configurable).
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.db.store import COLL_ORDERS, get_store_sync
from src.services.settings import get_settings_service
from src.services.users import UserService, get_user_service

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_COMPLETED = "completed"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_REFUNDED = "refunded"

ACTIVE_STATUSES = (STATUS_PENDING, STATUS_PROCESSING)
FINAL_STATUSES = (STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_REFUNDED)

KIND_LABELS = {
    "reaction": "👍 Reactions",
    "views": "📈 Views",
    "auto_views": "🔄 Auto Views",
    "auto_reactions": "🔄 Auto Reactions",
    "members": "👥 Members",
    "comment": "💬 Comments",
}

STATUS_EMOJI = {
    STATUS_PENDING: "🕓",
    STATUS_PROCESSING: "⚙️",
    STATUS_COMPLETED: "✅",
    STATUS_PARTIAL: "🟡",
    STATUS_FAILED: "❌",
    STATUS_CANCELLED: "🚫",
    STATUS_REFUNDED: "💸",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return f"ord_{int(time.time())}_{uuid.uuid4().hex[:6]}"


class OrderService:
    def __init__(self, store=None, settings=None, users: Optional[UserService] = None):
        self._store = store
        self._settings = settings
        self._users = users

    @property
    def store(self):
        return self._store or get_store_sync()

    @property
    def settings(self):
        if self._settings is None:
            self._settings = get_settings_service()
        return self._settings

    @property
    def users(self) -> UserService:
        if self._users is None:
            self._users = get_user_service()
        return self._users

    # ------------------------------------------------------------------
    # quoting
    # ------------------------------------------------------------------
    async def quote_plan(self, kind: str, plan_id: str) -> Optional[Dict[str, Any]]:
        """Look up a paid plan for an order kind."""
        section_map = {
            "reaction": "reactions", "auto_reactions": "reactions",
            "views": "views", "auto_views": "views",
            "members": "members", "comment": "comments", "channels": "channels",
        }
        section = section_map.get(kind)
        if not section:
            return None
        plan = await self.settings.get_plan(section, plan_id)
        if plan is None:
            return None
        return {
            "plan_id": plan.get("id"),
            "title": plan.get("title", plan_id),
            "quantity": int(plan.get("quantity", 0)),
            "price": round(float(plan.get("price", 0)), 2),
            "accounts": plan.get("accounts"),
            "auto_posts": plan.get("auto_posts"),
            "description": plan.get("description", ""),
        }

    async def free_quote(self, kind: str) -> Optional[Dict[str, Any]]:
        """Free-tier quote (used by the reaction free flow and free views)."""
        if kind == "reaction":
            cfg = await self.settings.section("reactions")
            available = 0
            try:
                from src.services.accounts import get_account_registry
                available = await get_account_registry().available_count()
            except Exception:
                available = 0
            limit = int(cfg.get("free_account_limit", 10))
            minimum = max(1, int(cfg.get("free_min_accounts", 1)))
            # "up to N accounts, or fewer if the pool is smaller" — never more
            # than what is really signed in, so the panel cannot promise 10
            # reactions from an empty pool.
            usable = min(available, limit)
            return {
                "plan_id": "free_reaction",
                "title": "Free Reactions",
                "quantity": usable,
                "price": 0.0,
                "accounts": usable,
                "available_accounts": available,
                "sufficient": available >= minimum,
                "reason": None if available else "No signed-in accounts available",
                "daily_limit": int(cfg.get("free_daily_orders", 3)),
                "cooldown": int(cfg.get("free_cooldown_seconds", 60)),
            }
        if kind == "views":
            cfg = await self.settings.section("views")
            return {
                "plan_id": "free_views",
                "title": "Free Views",
                "quantity": int(cfg.get("free_quantity", 100)),
                "price": 0.0,
                "daily_limit": int(cfg.get("free_daily_orders", 1)),
                "cooldown": 0,
            }
        return None

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------
    async def create_order(
        self,
        user_id: int,
        kind: str,
        payload: Dict[str, Any],
        *,
        plan: Optional[Dict[str, Any]] = None,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        paid_with: str = "wallet",
        charge: bool = True,
    ) -> Dict[str, Any]:
        """Create an order, charging the wallet atomically when needed."""
        user_id = int(user_id)
        await self.users.get_or_create(user_id)
        settings_orders = await self.settings.section("orders")

        active = await self.count_active(user_id)
        max_active = int(settings_orders.get("max_active_per_user", 5))
        if max_active and active >= max_active:
            raise ValueError(
                f"You already have {active} active orders (limit {max_active}). "
                f"Wait for them to finish or cancel one from Order Management."
            )

        plan = plan or {}
        amount = float(price if price is not None else plan.get("price", 0) or 0)
        quantity = int(quantity if quantity is not None else plan.get("quantity", 0) or 0)
        quoted = amount

        balance_before = await self.users.balance(user_id)
        # ------------------------------------------------------------------
        # Who pays?
        #   owner/admin  → god mode: never charged, everything is unlimited
        #   active plan  → the plan already covers its service, so no second
        #                  payment is asked for (that was the bug the operator
        #                  reported: plan granted, still asked to pay for
        #                  comments, then the rest)
        #   everybody else → wallet
        # ------------------------------------------------------------------
        covered_by = None
        if plan.get("covered_by_plan") and amount <= 0:
            # The panel quoted this order as "included in your plan": it is a
            # plan payment, not a freebie (the production log showed both the
            # double-charge bug and orders never saying who paid).
            covered_by = "plan"
        if charge and amount > 0:
            try:
                if await self.users.is_admin(user_id):
                    covered_by = "owner"
                elif await self._covered_by_plan(user_id, kind):
                    covered_by = "plan"
            except Exception as exc:  # noqa: BLE001 - never block a purchase on this
                logger.debug(f"charge policy lookup failed for {user_id}: {exc}")
        if covered_by:
            payload = dict(payload or {})
            payload["price_quoted"] = quoted
            amount = 0.0
            logger.info(
                f"Order {kind} for {user_id} covered by {covered_by} — no wallet charge "
                f"(quoted {quoted:.2f})"
            )
        elif charge and amount > 0:
            ok = await self.users.debit(user_id, amount, reason=f"order:{kind}", reference=None)
            if not ok:
                raise ValueError(
                    f"This order costs {amount:.2f} but your wallet has "
                    f"{balance_before:.2f}. Top up first (Wallet ▸ ➕ Add funds)."
                )

        order = {
            "_id": _new_id(),
            "user_id": user_id,
            "kind": kind,
            "status": STATUS_PENDING,
            "plan_id": plan.get("plan_id") or payload.get("plan_id"),
            "title": plan.get("title") or KIND_LABELS.get(kind, kind),
            "quantity": quantity,
            "price": amount,
            "paid_with": covered_by or ("free" if amount <= 0 else paid_with),
            "payload": payload,
            "result": {},
            "progress": "Queued",
            "balance_before": balance_before,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        await self.store.insert_one(COLL_ORDERS, order)
        await self.users.bump_stat(user_id, "orders")
        logger.info(f"Order created {order['_id']} kind={kind} user={user_id} amount={amount}")
        return order

    # Every order kind → the plan feature that covers it.
    KIND_FEATURES = {
        "reaction": "reactions", "auto_reactions": "reactions",
        "views": "views", "auto_views": "views",
        "members": "members", "comment": "comments", "channels": "channels",
    }

    async def _covered_by_plan(self, user_id: int, kind: str) -> bool:
        """Is this order already paid for by an active plan of the user?"""
        feature = self.KIND_FEATURES.get(kind)
        if not feature:
            return False
        try:
            premium = await self.settings.section("premium")
        except Exception:  # noqa: BLE001 - default to "included"
            premium = {}
        if not premium.get("plan_orders_included", True):
            return False
        if not await self.users.has_feature(int(user_id), feature):
            return False
        try:
            per_day = int(premium.get("plan_orders_per_day", 0) or 0)
        except (TypeError, ValueError):
            per_day = 0
        if per_day > 0:
            used = await self.users.get_counter(int(user_id), f"plan_{feature}")
            if used >= per_day:
                logger.info(f"User {user_id} reached the daily plan limit for {feature} ({per_day})")
                return False
            await self.users.bump_counter(int(user_id), f"plan_{feature}")
        return True

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    async def get(self, order_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_ORDERS, {"_id": order_id})

    async def list_for_user(self, user_id: int, limit: int = 10,
                            statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {"user_id": int(user_id)}
        if statuses:
            query["status"] = {"$in": list(statuses)}
        return await self.store.find(COLL_ORDERS, query, sort=[("created_at", -1)], limit=limit)

    async def list_all(self, limit: int = 10, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {}
        if statuses:
            query["status"] = {"$in": list(statuses)}
        return await self.store.find(COLL_ORDERS, query, sort=[("created_at", -1)], limit=limit)

    async def list_active(self, limit: int = 20) -> List[Dict[str, Any]]:
        return await self.store.find(
            COLL_ORDERS, {"status": {"$in": list(ACTIVE_STATUSES)}},
            sort=[("created_at", 1)], limit=limit,
        )

    async def count_active(self, user_id: int) -> int:
        return await self.store.count(
            COLL_ORDERS, {"user_id": int(user_id), "status": {"$in": list(ACTIVE_STATUSES)}}
        )

    async def stats(self) -> Dict[str, Any]:
        total = await self.store.count(COLL_ORDERS, {})
        completed = await self.store.count(COLL_ORDERS, {"status": STATUS_COMPLETED})
        failed = await self.store.count(COLL_ORDERS, {"status": {"$in": [STATUS_FAILED, STATUS_CANCELLED]}})
        pending = await self.store.count(COLL_ORDERS, {"status": {"$in": list(ACTIVE_STATUSES)}})
        orders = await self.store.find(COLL_ORDERS, {}, limit=1000)
        # Only real wallet payments are revenue — orders covered by a plan or by
        # the owner's god mode are work, not income.
        revenue = sum(
            float(o.get("price") or 0) for o in orders
            if (o.get("paid_with") or "wallet") == "wallet"
        )
        return {"total": total, "completed": completed, "failed": failed,
                "pending": pending, "revenue": round(revenue, 2)}

    # ------------------------------------------------------------------
    # state transitions
    # ------------------------------------------------------------------
    async def update(self, order_id: str, **fields: Any) -> None:
        fields["updated_at"] = _now_iso()
        await self.store.update_one(COLL_ORDERS, {"_id": order_id}, {"$set": fields})

    async def set_progress(self, order_id: str, text: str) -> None:
        await self.update(order_id, progress=text[:200])

    async def mark_processing(self, order_id: str) -> None:
        await self.update(order_id, status=STATUS_PROCESSING, started_at=_now_iso(), progress="Starting")

    async def complete(self, order_id: str, result: Optional[Dict[str, Any]] = None,
                       status: str = STATUS_COMPLETED) -> None:
        await self.update(order_id, status=status, result=result or {},
                          finished_at=_now_iso(), progress="Finished")

    async def fail(self, order_id: str, reason: str, *, refund: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """Fail an order and refund the user (default per settings)."""
        order = await self.get(order_id)
        if order is None:
            return None
        if order.get("status") in FINAL_STATUSES and order.get("status") != STATUS_PARTIAL:
            return order
        cfg = await self.settings.section("orders")
        should_refund = cfg.get("refund_on_failure", True) if refund is None else refund
        amount = float(order.get("price") or 0)
        new_status = STATUS_FAILED
        if should_refund and amount > 0 and (order.get("paid_with") or "wallet") == "wallet":
            await self.users.credit(
                order["user_id"], amount, reason=f"refund:{order['kind']}",
                reference=order_id, count_as_topup=False,
            )
            new_status = STATUS_REFUNDED
            logger.info(f"Refunded {amount} to user {order['user_id']} for order {order_id}")
        await self.update(order_id, status=new_status, error=reason[:300],
                          finished_at=_now_iso(), progress="Failed")
        return await self.get(order_id)

    async def cancel(self, order_id: str, by_user: bool = True) -> Optional[Dict[str, Any]]:
        order = await self.get(order_id)
        if order is None:
            return None
        if order.get("status") not in ACTIVE_STATUSES:
            raise ValueError("Only pending or processing orders can be cancelled")
        return await self.fail(order_id, "cancelled by user" if by_user else "cancelled by admin",
                               refund=True)

    async def add_quantity(self, order_id: str, quantity: int, *, account_delta: int = 0) -> None:
        """Extend a recurring order (auto views / auto reactions)."""
        order = await self.get(order_id)
        if not order:
            return
        payload = dict(order.get("payload") or {})
        payload["extra_quantity"] = int(payload.get("extra_quantity", 0)) + int(quantity)
        if account_delta:
            payload["accounts"] = int(payload.get("accounts", 0)) + account_delta
        await self.update(order_id, payload=payload, quantity=int(order.get("quantity", 0)) + quantity)


_order_service: Optional[OrderService] = None


def get_order_service(store=None, settings=None, users=None) -> OrderService:
    global _order_service
    if _order_service is None or store is not None or settings is not None or users is not None:
        _order_service = OrderService(store, settings, users)
    return _order_service


def reset_order_service() -> None:
    global _order_service
    _order_service = None
