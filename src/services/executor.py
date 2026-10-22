"""
Order execution engine.

Takes an order from MongoDB and actually performs the work using the signed-in
account pool, keeping the order document (and the user) updated along the way.
Failures refund automatically; partial deliveries are reported honestly instead
of pretending everything worked.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from src.services.channels import get_channel_service
from src.services.engagement import (
    EngagementService,
    get_engagement_service,
    looks_like_invite_problem,
)
from src.services.notify import Notifier, get_notifier
from src.services.orders import (
    STATUS_COMPLETED,
    STATUS_PARTIAL,
    OrderService,
    get_order_service,
)
from src.services.settings import get_settings_service
from src.services.users import get_user_service

logger = logging.getLogger(__name__)

MAX_CONCURRENT_ORDERS = 3


class OrderExecutor:
    """Runs orders in the background, at most ``MAX_CONCURRENT_ORDERS`` at once."""

    def __init__(
        self,
        engagement: Optional[EngagementService] = None,
        orders: Optional[OrderService] = None,
        notifier: Optional[Notifier] = None,
        channels=None,
        settings=None,
        users=None,
    ):
        self._engagement = engagement
        self._orders = orders
        self._notifier = notifier
        self._channels = channels
        self._settings = settings
        self._users = users
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_ORDERS)
        self._tasks: Dict[str, asyncio.Task] = {}

    # -- dependencies ------------------------------------------------------
    @property
    def engagement(self) -> EngagementService:
        if self._engagement is None:
            self._engagement = get_engagement_service()
        return self._engagement

    @property
    def orders(self) -> OrderService:
        if self._orders is None:
            self._orders = get_order_service()
        return self._orders

    @property
    def notifier(self) -> Notifier:
        if self._notifier is None:
            self._notifier = get_notifier()
        return self._notifier

    @property
    def channels(self):
        if self._channels is None:
            self._channels = get_channel_service()
        return self._channels

    @property
    def settings(self):
        if self._settings is None:
            self._settings = get_settings_service()
        return self._settings

    @property
    def users(self):
        if self._users is None:
            self._users = get_user_service()
        return self._users

    # ------------------------------------------------------------------
    # scheduling
    # ------------------------------------------------------------------
    def submit(self, order: Dict[str, Any]) -> None:
        """Schedule an order (non-blocking, used by panel handlers)."""
        order_id = order["_id"]
        if order_id in self._tasks and not self._tasks[order_id].done():
            return
        task = asyncio.create_task(self._guarded_run(order_id))
        self._tasks[order_id] = task
        task.add_done_callback(lambda t: self._tasks.pop(order_id, None))

    async def _guarded_run(self, order_id: str) -> None:
        async with self._semaphore:
            try:
                await self.run(order_id)
            except Exception as exc:  # never let a task die silently
                logger.error(f"Order {order_id} crashed: {exc}", exc_info=True)
                try:
                    await self.orders.fail(order_id, f"internal error: {exc}")
                except Exception:
                    pass

    async def run(self, order_id: str) -> Dict[str, Any]:
        order = await self.orders.get(order_id)
        if order is None:
            raise ValueError(f"Unknown order {order_id}")
        if order.get("status") not in ("pending", "processing"):
            logger.info(f"Order {order_id} already finished ({order.get('status')})")
            return order

        await self.orders.mark_processing(order_id)
        # Whoever pays for a channel is remembered as its sponsor *before* the
        # work starts: the production case was a channel attributed to one id
        # while the plan holder was another, so their plan never applied. It is
        # idempotent, and it also covers an order that ends up failing.
        await self._register_sponsor(order)
        kind = order.get("kind")
        try:
            if kind == "reaction":
                result = await self._run_reactions(order)
            elif kind == "views":
                result = await self._run_views(order)
            elif kind == "members":
                result = await self._run_members(order)
            elif kind == "comment":
                result = await self._run_comments(order)
            elif kind in ("auto_reactions", "auto_views"):
                result = await self._run_subscription(order)
            else:
                raise ValueError(f"Unsupported order kind: {kind}")
        except Exception as exc:
            logger.error(f"Order {order_id} failed: {exc}")
            await self.orders.fail(order_id, str(exc))
            await self._notify_failure(order, str(exc))
            return await self.orders.get(order_id) or order

        status = STATUS_COMPLETED
        nothing_worked = (
            isinstance(result, dict)
            and result.get("ok", 0) == 0
            and (result.get("failed", 0) > 0 or result.get("errors"))
        )
        if nothing_worked:
            reason = "; ".join(result.get("errors", [])[:2]) or "all attempts failed"
            await self.orders.fail(order_id, reason)
            await self._notify_failure(order, reason)
            return await self.orders.get(order_id) or order
        if isinstance(result, dict) and result.get("failed", 0) > 0:
            status = STATUS_PARTIAL

        await self.orders.complete(order_id, result, status=status)
        await self._notify_success(order, result, status)
        return await self.orders.get(order_id) or order

    async def _register_sponsor(self, order: Dict[str, Any]) -> None:
        """Remember the paying user behind a channel.

        Entitlements are resolved from the channel's owner / added-by / sponsors;
        in production the channel was attributed to one id while the plan holder
        was another, so the paid plan never applied. Registering the buyer fixes
        that for good — one order is enough for the plan to unlock the channel.
        """
        try:
            payload = order.get("payload") or {}
            user_id = int(order.get("user_id") or 0)
            if not user_id:
                return
            channel_id = int(payload.get("channel_id") or 0)
            if channel_id:
                await self.channels.add_sponsor(channel_id, user_id)
                return
            link = payload.get("link") or payload.get("channel")
            if not link:
                return
            doc = await self.channels.doc_for_link(link)
            if doc:
                await self.channels.add_sponsor(doc["_id"], user_id)
        except Exception as exc:  # noqa: BLE001 - bookkeeping only
            logger.debug(f"sponsor registration failed: {exc}")

    # ------------------------------------------------------------------
    # kind handlers
    # ------------------------------------------------------------------
    async def _run_reactions(self, order: Dict[str, Any]) -> Dict[str, Any]:
        payload = order.get("payload") or {}
        accounts = int(payload.get("accounts") or order.get("quantity") or 0)
        emojis = payload.get("emojis") or None
        join = bool(payload.get("join", True))
        progress = lambda text: self.orders.set_progress(order["_id"], text)  # noqa: E731
        result = await self.engagement.react_to_link(
            payload["link"], accounts, emojis=emojis,
            join_if_needed=join, progress=progress,
        )
        if (result.ok == 0 and join and result.errors
                and looks_like_invite_problem(result.errors)):
            # The invite link was revoked/expired (or missing) so nobody could
            # join. Ask the bot for a fresh link and try the whole batch once more
            # instead of handing the user a failure they cannot act on.
            fresh = await self.engagement.refresh_invite(payload["link"])
            if fresh:
                logger.info(f"Order {order['_id']}: retrying reactions with a fresh invite link")
                result = await self.engagement.react_to_link(
                    payload["link"], accounts, emojis=emojis,
                    join_if_needed=join, progress=progress,
                )
        await self.users.bump_stat(order["user_id"], "reactions", result.ok)
        return self._result_dict(result, requested=accounts)

    async def _run_views(self, order: Dict[str, Any]) -> Dict[str, Any]:
        payload = order.get("payload") or {}
        quantity = int(payload.get("views") or order.get("quantity") or 0)
        result = await self.engagement.send_views(
            payload["link"], quantity,
            account_count=payload.get("accounts"),
            progress=lambda text: self.orders.set_progress(order["_id"], text),
        )
        await self.users.bump_stat(order["user_id"], "views", result.ok)
        return self._result_dict(result, requested=quantity)

    async def _run_members(self, order: Dict[str, Any]) -> Dict[str, Any]:
        payload = order.get("payload") or {}
        count = int(payload.get("members") or order.get("quantity") or 0)
        result = await self.engagement.add_members(
            payload.get("channel") or payload.get("link"),
            count,
            invite_link=payload.get("invite_link"),
            progress=lambda text: self.orders.set_progress(order["_id"], text),
        )
        await self.users.bump_stat(order["user_id"], "members", result.ok)
        return self._result_dict(result, requested=count)

    async def _run_comments(self, order: Dict[str, Any]) -> Dict[str, Any]:
        payload = order.get("payload") or {}
        accounts = int(payload.get("accounts") or order.get("quantity") or 0)
        result = await self.engagement.post_comments(
            payload["link"], payload.get("text", ""),
            account_count=accounts,
            per_account_texts=payload.get("texts"),
            progress=lambda text: self.orders.set_progress(order["_id"], text),
        )
        await self.users.bump_stat(order["user_id"], "comments", result.ok)
        return self._result_dict(result, requested=accounts)

    async def _run_subscription(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Turn a paid plan into channel automation credits."""
        payload = order.get("payload") or {}
        channel_id = int(payload.get("channel_id") or 0)
        if not channel_id:
            raise ValueError("This plan needs a channel — add the bot to your channel first")
        posts = int(payload.get("posts") or 0)
        accounts = int(payload.get("accounts") or 0)
        if order["kind"] == "auto_reactions":
            await self.channels.set_auto_reactions(
                channel_id, True, accounts=accounts or None,
                posts_left=posts or None, emojis=payload.get("emojis") or None,
            )
        else:
            await self.channels.set_auto_views(
                channel_id, True,
                views_per_post=int(payload.get("views_per_post") or 0) or None,
                posts_left=posts or None,
            )
        await self.channels.add_sponsor(channel_id, int(order["user_id"]))
        channel = await self.channels.get(channel_id) or {}
        return {
            "ok": 1, "failed": 0, "skipped": 0, "errors": [],
            "accounts_used": accounts,
            "message": (
                f"⚙️ Automation activated for <b>{channel.get('title', channel_id)}</b>"
                + (f" — {posts} posts" if posts else " — unlimited posts (free plan)")
            ),
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _result_dict(result, requested: int = 0) -> Dict[str, Any]:
        if isinstance(result, dict):
            return result
        data = {
            "ok": result.ok,
            "failed": result.failed,
            "skipped": result.skipped,
            "errors": list(result.errors),
            "requested": requested,
        }
        data.update(result.details or {})
        return data

    async def _notify_success(self, order: Dict[str, Any], result: Dict[str, Any], status: str) -> None:
        title = order.get("title") or order.get("kind")
        summary = (
            f"{'✅' if status == STATUS_COMPLETED else '🟡'} <b>Order finished</b>\n\n"
            f"🧩 {title}\n"
            f"🆔 <code>{order['_id']}</code>\n"
            f"✅ Success: {result.get('ok', 0)}\n"
        )
        if result.get("failed"):
            summary += f"❌ Failed: {result['failed']}\n"
        if result.get("shortfall"):
            summary += (
                f"\n⚠️ {result['shortfall']} could not be delivered because the account pool "
                f"was smaller than the requested amount."
            )
        if result.get("message"):
            summary += f"\n{result['message']}"
        await self.notifier.to_user(order["user_id"], summary)

    async def _notify_failure(self, order: Dict[str, Any], reason: str) -> None:
        refreshed = await self.orders.get(order["_id"]) or order
        refunded = refreshed.get("status") == "refunded"
        text = (
            f"❌ <b>Order failed</b>\n\n"
            f"🧩 {order.get('title') or order.get('kind')}\n"
            f"🆔 <code>{order['_id']}</code>\n"
            f"📝 {reason[:180]}\n"
        )
        if refunded:
            text += f"\n💸 {order.get('price', 0):.2f} refunded to your wallet."
        await self.notifier.to_user(order["user_id"], text)


_executor: Optional[OrderExecutor] = None


def get_order_executor(engagement=None, orders=None, notifier=None, channels=None,
                       settings=None, users=None) -> OrderExecutor:
    """Process-wide executor. Passing any dependency rebuilds it (used by tests)."""
    global _executor
    deps = (engagement, orders, notifier, channels, settings, users)
    if _executor is None or any(d is not None for d in deps):
        _executor = OrderExecutor(engagement, orders, notifier, channels, settings, users)
    return _executor


def reset_order_executor() -> None:
    global _executor
    _executor = None
