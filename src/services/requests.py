"""
Plan requests — the user asks, the owner decides.

Flow (exactly what the operator asked for):

1. A user opens **Buy Premium**, sees every plan with its details and taps one.
2. ``request()`` stores a pending request (MongoDB, ``plan_requests``) and
   notifies every admin/owner with one message that already carries the numbers
   they need (who, which plan, how many days/channels/accounts, price).
3. The admin takes the payment however they want and activates the plan from the
   bot panel, the web admin panel or with one tap on the notification — the
   request is marked ``approved`` and the user gets their activation message.

Everything is stored, so a crash/restart never loses a pending request, and the
web admin panel shows the same queue live.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

logger = logging.getLogger(__name__)

COLL_REQUESTS = "plan_requests"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def _new_id() -> str:
    return f"req_{int(time.time())}_{int(time.time() * 1000) % 100000}"


class PlanRequestService:
    """Small, dependency-light store for "user wants a plan" requests."""

    def __init__(self, store=None, notifier=None, users=None):
        self._store = store
        self._notifier = notifier
        self.users = users

    @property
    def store(self):
        if self._store is None:
            from src.db.store import get_store_sync

            self._store = get_store_sync()
        return self._store

    # ------------------------------------------------------------------
    async def request(
        self,
        user_id: int,
        *,
        kind: str = "premium",
        plan_id: str = "",
        title: str = "",
        price: float = 0.0,
        days: int = 0,
        accounts: int = 0,
        channels: int = 0,
        note: str = "",
        username: str = "",
    ) -> Dict[str, Any]:
        """Store a pending request and ping the admins."""
        doc = {
            "_id": _new_id(),
            "user_id": int(user_id),
            "username": str(username or ""),
            "kind": str(kind or "premium"),
            "plan_id": str(plan_id or ""),
            "title": str(title or plan_id or kind),
            "price": float(price or 0),
            "days": int(days or 0),
            "accounts": int(accounts or 0),
            "channels": int(channels or 0),
            "note": str(note or ""),
            "status": STATUS_PENDING,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        await self.store.insert_one(COLL_REQUESTS, doc)
        logger.info(
            f"Plan request {doc['_id']} from {user_id}: {doc['title']} "
            f"(₹{doc['price']}, {doc['days']}d, {doc['channels']} ch)"
        )
        await self._notify_admins(doc)
        return doc

    async def _notify_admins(self, doc: Dict[str, Any]) -> None:
        try:
            notifier = self._notifier
            if notifier is None:
                from src.services.notify import get_notifier

                notifier = get_notifier()
            lines = [
                "📨 <b>New plan request</b>",
                "━━━━━━━━━━━━━━━━━━━━",
                f"👤 User: <code>{doc['user_id']}</code>"
                + (f" (@{doc['username']})" if doc.get("username") else ""),
                f"👑 Plan: <b>{doc['title']}</b>",
            ]
            if doc.get("days"):
                lines.append(f"🗓 Duration: <b>{doc['days']} days</b>")
            if doc.get("accounts"):
                lines.append(f"👥 Accounts: <b>{doc['accounts']}</b>")
            if doc.get("channels"):
                lines.append(f"📡 Channels: <b>{doc['channels']}</b>")
            if doc.get("price"):
                lines.append(f"💵 Price: <b>₹{doc['price']:g}</b>")
            if doc.get("note"):
                lines.append(f"📝 {doc['note']}")
            lines.append("")
            lines.append(
                "Take the payment, then grant the plan from the admin panel "
                "(or the web dashboard ▸ Requests)."
            )
            lines.append(
                "👉 Open <b>🛠 Admin panel ▸ 👑 Plans ▸ 📨 Requests</b> to approve it, "
                "or use the web dashboard."
            )
            await notifier.to_admins("\n".join(lines))
        except Exception as exc:  # noqa: BLE001 - never break the user's flow
            logger.debug(f"plan-request notice failed: {exc}")

    # ------------------------------------------------------------------
    async def get(self, request_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_REQUESTS, {"_id": request_id})

    async def list(self, status: Optional[str] = None, limit: int = 50,
                   user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        query: Dict[str, Any] = {}
        if status:
            query["status"] = status
        if user_id is not None:
            query["user_id"] = int(user_id)
        return await self.store.find(COLL_REQUESTS, query,
                                     sort=[("created_at", -1)], limit=limit)

    async def pending_count(self) -> int:
        return await self.store.count(COLL_REQUESTS, {"status": STATUS_PENDING})

    async def mark(self, request_id: str, status: str, *, by: int = 0,
                   note: str = "") -> Optional[Dict[str, Any]]:
        await self.store.update_one(
            COLL_REQUESTS, {"_id": request_id},
            {"$set": {"status": status, "handled_by": int(by or 0),
                      "handled_at": _now_iso(), "handler_note": note,
                      "updated_at": _now_iso()}},
        )
        doc = await self.get(request_id)
        if doc is not None:
            logger.info(f"Plan request {request_id} → {status} (by {by or 'system'})")
        return doc

    async def approve(self, request_id: str, *, by: int = 0, days: Optional[int] = None,
                      channels: Optional[int] = None, accounts: Optional[int] = None,
                      notify: bool = True) -> Optional[Dict[str, Any]]:
        """Grant the plan behind a request and mark it approved."""
        doc = await self.get(request_id)
        if doc is None:
            return None
        users = self.users
        if users is None:
            from src.services.users import get_user_service

            users = get_user_service()
        kind = doc.get("kind") or "premium"
        kinds = ["all"] if kind in ("premium", "all", "") else [kind]
        plan = await users.grant_plan(
            int(doc["user_id"]),
            kinds=kinds,
            days=int(days or doc.get("days") or 30),
            accounts=accounts or doc.get("accounts") or None,
            channels=channels if channels is not None else (doc.get("channels") or None),
            note=f"request {doc['_id']}",
            notify=notify,
        )
        await self.mark(request_id, STATUS_APPROVED, by=by)
        return {"request": doc, "plan": plan}

    async def reject(self, request_id: str, *, by: int = 0, reason: str = "") -> Optional[Dict[str, Any]]:
        doc = await self.mark(request_id, STATUS_REJECTED, by=by, note=reason)
        if doc is None:
            return None
        try:
            from src.services.notify import get_notifier

            await get_notifier().to_user(
                int(doc["user_id"]),
                "❌ <b>Plan request declined</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Plan: <b>{doc.get('title')}</b>\n"
                + (f"📝 {reason}\n" if reason else "")
                + "\nMessage the owner if you think this was a mistake.",
            )
        except Exception as exc:  # noqa: BLE001 - notice only
            logger.debug(f"rejection notice failed: {exc}")
        return doc


_requests: Optional[PlanRequestService] = None


def get_plan_request_service(store=None, notifier=None, users=None) -> PlanRequestService:
    global _requests
    if _requests is None or store is not None or notifier is not None or users is not None:
        _requests = PlanRequestService(store or (_requests.store if _requests else None),
                                       notifier, users)
    return _requests


def reset_plan_request_service() -> None:
    global _requests
    _requests = None
