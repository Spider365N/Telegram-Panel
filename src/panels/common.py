"""
Shared helpers for the user and admin panels.

`Screen` is a tiny "render this text with these buttons" value object, and
`PanelBase` gives both panels service access, formatting helpers, input-state
handling and navigation utilities.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from telethon import Button

from src import config as app_config
from src.services.accounts import get_account_registry
from src.services.autoengage import get_auto_engagement
from src.services.channels import get_channel_service
from src.services.engagement import get_engagement_service
from src.services.executor import get_order_executor
from src.services.notify import get_notifier
from src.services.orders import KIND_LABELS, STATUS_EMOJI, get_order_service
from src.services.payments import get_payment_service
from src.services.settings import get_settings_service
from src.services.users import ROLE_ADMIN, ROLE_OWNER, get_user_service
from src.utils import is_bot_message  # noqa: F401  (re-exported for panels)

logger = logging.getLogger(__name__)

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"

# Prefixes that keep panel callbacks from colliding with legacy handlers
USER_PREFIX = "u:"
ADMIN_PREFIX = "a:"


@dataclass
class Screen:
    """A rendered panel screen."""

    text: str
    buttons: List[List[Any]] = field(default_factory=list)
    parse_mode: Optional[str] = "html"

    def with_back(self, action: str) -> "Screen":
        self.buttons = list(self.buttons) + [[Button.inline("⬅️ Back", action, style=PRIMARY)]]
        return self


def fmt_money(amount: float) -> str:
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        amount = 0.0
    symbol = getattr(app_config, "CURRENCY", "\u20b9")
    if amount == int(amount):
        return f"{symbol}{int(amount)}"
    return f"{symbol}{amount:.2f}"


def fmt_date(timestamp: Any) -> str:
    """A short, human date for a unix timestamp (timezone-aware, no warnings)."""
    try:
        import datetime
        moment = datetime.datetime.fromtimestamp(float(timestamp), datetime.timezone.utc)
        return moment.strftime("%d %b %Y")
    except (TypeError, ValueError, OSError):
        return "—"


def fmt_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def status_line(status: str) -> str:
    return f"{STATUS_EMOJI.get(status, '•')} {str(status).title()}"


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind.title())


def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "—"
    filled = max(0, min(width, int(round(width * done / total))))
    return "▰" * filled + "▱" * (width - filled)


class PanelBase:
    """Common behaviour for `UserPanel` and `AdminPanel`."""

    prefix = USER_PREFIX

    def __init__(self, router):
        self.router = router

    # -- service accessors -------------------------------------------------
    @property
    def settings(self):
        return get_settings_service()

    @property
    def users(self):
        return get_user_service()

    @property
    def orders(self):
        return get_order_service()

    @property
    def payments(self):
        return get_payment_service()

    @property
    def channels(self):
        return get_channel_service()

    @property
    def accounts(self):
        return get_account_registry()

    @property
    def engagement(self):
        return get_engagement_service()

    @property
    def executor(self):
        return get_order_executor()

    @property
    def notifier(self):
        return get_notifier()

    @property
    def auto(self):
        return get_auto_engagement()

    # -- event helpers -----------------------------------------------------
    @staticmethod
    def user_of(event) -> Dict[str, Any]:
        sender = getattr(event, "sender", None)
        return {
            "id": getattr(event, "sender_id", None),
            "username": getattr(sender, "username", None),
            "first_name": getattr(sender, "first_name", None),
        }

    async def ensure_user(self, event) -> Dict[str, Any]:
        info = self.user_of(event)
        return await self.users.get_or_create(
            int(info["id"]), info.get("username"), info.get("first_name")
        )

    async def render(self, event, screen: Screen) -> None:
        """Show a screen, editing the current message when possible."""
        await self.router.render(event, screen)

    async def answer(self, event, text: str = "", alert: bool = False) -> None:
        await self.router.answer(event, text, alert)

    # -- input state -------------------------------------------------------
    async def set_input(self, event, action: str, data: Optional[Dict[str, Any]] = None,
                        prompt: Optional[str] = None, buttons: Optional[List[List[Any]]] = None) -> None:
        """Ask the user for text input (link / amount / UTR / price …).

        Stores the pending action and immediately renders the prompt with a
        Cancel button, so the user always sees what the bot is waiting for.
        """
        panel = "admin" if self.prefix == ADMIN_PREFIX else "user"
        payload = dict(data or {})
        payload.pop("prompt", None)
        payload.pop("buttons", None)
        await self.router.request_input(
            event,
            panel=panel,
            action=action,
            prompt=prompt or "✍️ Send the value:",
            data=payload,
            buttons=buttons,
        )

    async def get_input(self, chat_id: int) -> Dict[str, Any]:
        return await self.router.get_input(chat_id)

    async def clear_input(self, chat_id: int) -> None:
        await self.router.clear_input(chat_id)

    # -- navigation --------------------------------------------------------
    def cb(self, action: str, *args: Any) -> str:
        parts = [self.prefix + action, *[str(a) for a in args]]
        return ":".join(parts)

    @staticmethod
    def back_row(action: str, label: str = "⬅️ Back") -> List[Any]:
        return [Button.inline(label, action, style=PRIMARY)]

    @staticmethod
    def home_row(panel: Optional[str] = None) -> List[Any]:
        """Main-menu button — ``"admin"`` gives the admin panel's home."""
        if panel == "admin":
            target = ADMIN_PREFIX + "home"
        else:
            target = panel or (USER_PREFIX + "home")
        return [Button.inline("🏠 Main Menu", target, style=PRIMARY)]

    # -- permissions -------------------------------------------------------
    async def is_admin(self, user_id: int) -> bool:
        return await self.users.is_admin(user_id)

    async def is_owner(self, user_id: int) -> bool:
        return await self.users.is_owner(user_id)

    async def role(self, user_id: int) -> str:
        return await self.users.role_of(user_id)

    # -- multi-user isolation ---------------------------------------------
    async def channels_of(self, user_id: int) -> List[Dict[str, Any]]:
        """Channels this user may see.

        * owner  → everything (super powers)
        * admin  → their own channels + channels nobody could be attributed to
        * user   → strictly their own channels

        A user can never see or manage somebody else's channel.
        """
        user_id = int(user_id)
        if await self.is_owner(user_id):
            return await self.channels.list_all(limit=200)
        # Sponsors (users a paid order registered for a channel) and the person
        # who actually added the bot must see the channel too — otherwise a plan
        # holder cannot even open the channel their own plan is running on.
        channels = await self.channels.channels_for_user(user_id)
        if await self.is_admin(user_id):
            known = {c["_id"] for c in channels}
            try:
                for channel in await self.channels.list_unowned(limit=200):
                    if channel["_id"] not in known:
                        channels.append(channel)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"unowned channel lookup failed: {exc}")
        return channels

    async def manage_target(self, user_id: int, channel_id) -> Optional[Dict[str, Any]]:
        """Fetch a channel only if *user_id* is allowed to manage it."""
        try:
            channel_id = int(channel_id)
        except (TypeError, ValueError):
            return None
        channel = await self.channels.get(channel_id)
        if channel is None:
            return None
        user_id = int(user_id)
        if await self.is_owner(user_id):
            return channel
        owner_id = channel.get("owner_id")
        if owner_id and int(owner_id) == user_id:
            return channel
        added_by = channel.get("added_by")
        if added_by and int(added_by) == user_id:
            return channel
        sponsors = [int(s) for s in (channel.get("sponsors") or []) if str(s).lstrip("-").isdigit()]
        if user_id in sponsors:
            return channel
        # Owner/admin god mode: the panel operators are never blocked, and a
        # channel nobody could be attributed to stays manageable for them.
        if await self.is_admin(user_id):
            return channel
        return None

    # -- premium gates -----------------------------------------------------
    async def feature_locked(self, event, feature: str, title: str,
                             note: str = "") -> bool:
        """Render "premium only" when a feature is not in the user's plan.

        Returns True when the feature is locked (the caller then stops).
        """
        user_id = int(getattr(event, "sender_id", 0) or 0)
        if await self.users.has_feature(user_id, feature):
            return False
        support = await self.settings.section("support")
        text = (
            f"{title}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔒 <b>Premium service</b>\n\n"
            "Only <b>auto reactions</b> are free. Views, comments, members and "
            "extra channels are activated by the owner/admin after a quick chat.\n"
        )
        if note:
            text += f"\n{note}\n"
        text += (
            "\nTap <b>Request activation</b> below and the owner gets your "
            "request instantly — or message them directly."
        )
        rows: List[List[Any]] = [
            [Button.inline("📨 Request activation", self.cb("reqplan", feature), SUCCESS)],
        ]
        username = str(support.get("username") or "").lstrip("@")
        owner = await self.owner_handle()
        contact = username or owner
        if contact:
            rows.append([Button.url("💬 Contact owner", f"https://t.me/{contact}")])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))
        return True

    async def owner_handle(self) -> str:
        """@username of the owner, when we can find it (for contact buttons)."""
        try:
            from src import config as app_config
            owner_id = int(getattr(app_config, "OWNER_ID", 0) or 0)
            if not owner_id:
                return ""
            from src.runtime import get_bot
            bot = get_bot()
            client = getattr(bot, "tbot", None)
            me = await client.get_entity(owner_id) if client is not None else None
            return str(getattr(me, "username", "") or "")
        except Exception:
            return ""

    async def notify_admins_request(self, event, feature: str) -> bool:
        """Forward a "please activate <feature>" request to the admins/owner."""
        user_id = int(getattr(event, "sender_id", 0) or 0)
        user = await self.users.get_user(user_id) or {}
        labels = (await self.settings.section("premium")).get("feature_labels", {})
        label = labels.get(feature, feature)
        text = (
            "📨 <b>Plan request</b>\n\n"
            f"👤 <b>{user.get('first_name') or 'User'}</b>"
            f" @{user.get('username') or '—'}\n"
            f"🆔 <code>{user_id}</code>\n"
            f"🎯 wants: <b>{label}</b>\n\n"
            "Reply with a plan: admin panel → 👥 Users → open the user → "
            "👑 Give plan, or use <code>/grant "
            f"{user_id} {feature} 30</code>."
        )
        try:
            await self.notifier.to_admins(
                text,
                buttons=[[Button.inline("👤 Open user", f"a:user:{user_id}", style=PRIMARY)]],
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"plan request forward failed: {exc}")
            return False
        return True

    # -- formatting --------------------------------------------------------
    @staticmethod
    def order_block(order: Dict[str, Any], *, detailed: bool = False) -> str:
        result = order.get("result") or {}
        text = (
            f"{status_line(order.get('status', 'pending'))} • <b>{order.get('title') or kind_label(order.get('kind'))}</b>\n"
            f"🆔 <code>{order.get('_id')}</code>\n"
            f"📦 Qty: {fmt_number(order.get('quantity'))}   💰 {fmt_money(order.get('price'))}\n"
            f"🕒 {str(order.get('created_at', ''))[:19].replace('T', ' ')}"
        )
        if detailed:
            text += f"\n⚙️ Progress: {order.get('progress', '—')}"
            if result:
                text += (
                    f"\n✅ Success: {result.get('ok', 0)}   ❌ Failed: {result.get('failed', 0)}"
                )
            if order.get("error"):
                text += f"\n📝 {str(order['error'])[:160]}"
        return text

    @staticmethod
    def active_of(orders: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [o for o in orders if o.get("status") in ("pending", "processing")]


class CallbackCache:
    """Guards against double taps / duplicate callback deliveries.

    Telegram can redeliver the same callback within milliseconds; a long window
    would swallow legitimate repeat taps (the panel edits one message in place,
    so the same callback data legitimately recurs).
    """

    def __init__(self, ttl: float = 0.75):
        self.ttl = ttl
        self._seen: Dict[str, float] = {}

    def seen(self, key: str) -> bool:
        now = time.time()
        if len(self._seen) > 2000:
            self._seen = {k: v for k, v in self._seen.items() if now - v < self.ttl}
        last = self._seen.get(key)
        self._seen[key] = now
        return bool(last and (now - last) < self.ttl)
