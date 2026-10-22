"""
Wallet top-ups (manual UPI / UTR flow) and premium plan purchases.

Flow
----
1. User taps "💳 Add Money" → panel shows the UPI id + amount.
2. User pays and submits the UTR/reference number.
3. A ``topup`` request is stored as ``pending`` and every admin is notified with
   Approve / Reject buttons.
4. Approval credits the wallet atomically and writes a transaction row.

Premium plans are bought straight from the wallet (or free of charge when the
admin grants them), optionally unlocking services for N days.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.db.store import COLL_TRANSACTIONS, get_store_sync
from src.services.settings import get_settings_service
from src.services.users import UserService, get_user_service

logger = logging.getLogger(__name__)

TOPUP_PENDING = "pending"
TOPUP_APPROVED = "approved"
TOPUP_REJECTED = "rejected"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PaymentService:
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
    # Top-ups
    # ------------------------------------------------------------------
    async def create_topup(
        self,
        user_id: int,
        amount: float,
        utr: str,
        method: str = "upi",
        note: str = "",
    ) -> Dict[str, Any]:
        amount = round(float(amount), 2)
        cfg = await self.settings.section("payments")
        minimum = float(cfg.get("min_topup", 0) or 0)
        if amount < minimum:
            raise ValueError(f"Minimum top-up amount is {minimum:.0f}")
        if amount <= 0:
            raise ValueError("Amount must be greater than zero")
        utr = (utr or "").strip()
        if len(utr) < 4:
            raise ValueError("Please send a valid UTR / reference number")

        # Duplicate UTR protection
        duplicate = await self.store.find_one(
            COLL_TRANSACTIONS, {"utr": utr, "status": {"$ne": TOPUP_REJECTED}}
        )
        if duplicate:
            raise ValueError("This UTR is already submitted. Please check with support.")

        request = {
            "_id": f"top_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            "user_id": int(user_id),
            "kind": "topup_request",
            "type": "credit",
            "amount": amount,
            "method": method,
            "utr": utr,
            "note": note,
            "status": TOPUP_PENDING,
            "created_at": _now_iso(),
        }
        await self.users.get_or_create(user_id)
        await self.store.insert_one(COLL_TRANSACTIONS, request)
        logger.info(f"Top-up request {request['_id']} from {user_id} for {amount}")
        return request

    async def get_topup(self, request_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_TRANSACTIONS, {"_id": request_id})

    async def pending_topups(self, limit: int = 20) -> List[Dict[str, Any]]:
        return await self.store.find(
            COLL_TRANSACTIONS,
            {"kind": "topup_request", "status": TOPUP_PENDING},
            sort=[("created_at", 1)], limit=limit,
        )

    async def approve_topup(self, request_id: str, admin_id: Optional[int] = None,
                            amount_override: Optional[float] = None) -> Dict[str, Any]:
        request = await self.get_topup(request_id)
        if request is None:
            raise ValueError("Top-up request not found")
        if request.get("status") != TOPUP_PENDING:
            raise ValueError(f"Request already {request.get('status')}")
        amount = float(amount_override if amount_override is not None else request.get("amount", 0))
        await self.users.credit(
            request["user_id"], amount, reason="topup approved",
            reference=request_id, count_as_topup=True,
        )
        await self.store.update_one(
            COLL_TRANSACTIONS, {"_id": request_id},
            {"$set": {"status": TOPUP_APPROVED, "approved_by": admin_id,
                      "approved_at": _now_iso(), "amount": amount}},
        )
        request.update({"status": TOPUP_APPROVED, "amount": amount})
        return request

    async def reject_topup(self, request_id: str, reason: str = "",
                           admin_id: Optional[int] = None) -> Dict[str, Any]:
        request = await self.get_topup(request_id)
        if request is None:
            raise ValueError("Top-up request not found")
        await self.store.update_one(
            COLL_TRANSACTIONS, {"_id": request_id},
            {"$set": {"status": TOPUP_REJECTED, "reason": reason,
                      "reviewed_by": admin_id, "reviewed_at": _now_iso()}},
        )
        request.update({"status": TOPUP_REJECTED, "reason": reason})
        return request

    # ------------------------------------------------------------------
    # Premium plans
    # ------------------------------------------------------------------
    async def premium_plans(self) -> List[Dict[str, Any]]:
        cfg = await self.settings.section("premium")
        return list(cfg.get("plans", []))

    async def get_premium_plan(self, plan_id: str) -> Optional[Dict[str, Any]]:
        for plan in await self.premium_plans():
            if str(plan.get("id")) == str(plan_id):
                return plan
        return None

    async def buy_premium(self, user_id: int, plan_id: str) -> Dict[str, Any]:
        """Buy a premium plan from the wallet balance."""
        plan = await self.get_premium_plan(plan_id)
        if plan is None:
            raise ValueError("Unknown premium plan")
        price = float(plan.get("price", 0) or 0)
        if price > 0 and await self.users.is_admin(user_id):
            # Owner/admin god mode: every feature is free for them.
            logger.info(f"Premium plan {plan_id} granted to admin {user_id} without payment")
            price = 0.0
        if price > 0:
            paid = await self.users.debit(
                user_id, price, reason="premium purchase", reference=str(plan_id)
            )
            if not paid:
                raise ValueError(
                    f"This plan costs {price:.2f} but your wallet has less. "
                    f"Top up first (Wallet ▸ ➕ Add funds)."
                )
        granted = await self.users.grant_premium(
            user_id, days=int(plan.get("days", 30)),
            accounts=plan.get("accounts"), note=str(plan.get("title", plan_id)),
        )
        await self.store.insert_one(COLL_TRANSACTIONS, {
            "_id": f"prm_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            "user_id": int(user_id),
            "kind": "premium_purchase",
            "type": "debit",
            "amount": price,
            "plan_id": plan_id,
            "status": "completed",
            "created_at": _now_iso(),
        })
        logger.info(f"Premium granted to {user_id} via plan {plan_id}")
        return {"plan": plan, "granted": granted}

    async def expired_premiums(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Users whose premium just lapsed (used by the daily maintenance task)."""
        docs = await self.store.find(
            COLL_TRANSACTIONS, {"kind": "premium_purchase", "status": "completed"}, limit=limit
        )
        return docs

    async def income_stats(self) -> Dict[str, Any]:
        rows = await self.store.find(COLL_TRANSACTIONS, {}, limit=2000)
        approved = [r for r in rows if r.get("status") == TOPUP_APPROVED]
        premium = [r for r in rows if r.get("kind") == "premium_purchase"]
        return {
            "topups": round(sum(float(r.get("amount", 0)) for r in approved), 2),
            "topup_count": len(approved),
            "premium_revenue": round(sum(float(r.get("amount", 0)) for r in premium), 2),
            "premium_count": len(premium),
            "pending_topups": len([r for r in rows if r.get("status") == TOPUP_PENDING]),
        }


_payment_service: Optional[PaymentService] = None


def get_payment_service(store=None, settings=None, users=None) -> PaymentService:
    global _payment_service
    if _payment_service is None or store is not None or settings is not None or users is not None:
        _payment_service = PaymentService(store, settings, users)
    return _payment_service


def reset_payment_service() -> None:
    global _payment_service
    _payment_service = None
