"""
Admin panel — everything the owner/admin can configure or moderate.

* dashboard with live stats
* pricing editor for every service (reactions, views, members, comments, channels, premium)
* free-tier limits (accounts per order, daily orders, cooldowns, per-post automation)
* reaction emoji list editor
* users: roles, wallets, premium grants, bans
* orders: inspect, cancel/refund, retry
* top-ups: approve / reject
* channels: who added the bot, automation state
* accounts: pool status + add/list (session strings live in MongoDB)
* broadcast, payment settings, support settings, announcement
* owner-only: Send PV (hidden from every admin but the owner)
"""
from __future__ import annotations

import asyncio
import logging
from html import escape as esc
from typing import Any, Dict, List, Optional

from telethon import Button

from src import config as app_config
from src.panels.common import (
    DANGER,
    PRIMARY,
    SUCCESS,
    PanelBase,
    Screen,
    fmt_date,
    fmt_money,
    fmt_number,
    kind_label,
    status_line,
)
from src.services.orders import ACTIVE_STATUSES, FINAL_STATUSES
from src.services.settings import DEFAULT_POSITIVE_EMOJIS
from src.services.forcesub import get_force_sub_service
from src.services.requests import get_plan_request_service
from src.services.users import ROLE_ADMIN, ROLE_OWNER, ROLE_USER

logger = logging.getLogger(__name__)

SECTION_TITLES = {
    "reactions": "❤️ Reactions",
    "views": "📈 Views",
    "members": "👥 Members",
    "comments": "💬 Comments",
    "channels": "📡 Channels",
    "premium": "👑 Premium",
}

# Section → label of the per-plan quantity field (channels sell *slots*, not posts)
QUANTITY_LABELS = {"channels": "Slots", "premium": "Accounts"}

# Fields an admin can edit for a plan, per section
PLAN_FIELDS = ("title", "price", "quantity")


def btn(label: str, action: str, style: Optional[str] = None):
    return Button.inline(label, action, style=style) if style else Button.inline(label, action)


class AdminPanel(PanelBase):
    prefix = "a:"

    # ==================================================================
    async def show_main(self, event) -> None:
        user = await self.ensure_user(event)
        owner = await self.is_owner(user["_id"])
        stats = await self._dashboard_stats()
        settings = await self.settings.get()
        text = (
            "🛠 <b>ADMIN PANEL</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>{esc(str(user.get('first_name') or 'Admin'))}</b>"
            f"{' • 👑 Owner' if owner else ' • Admin'}\n"
            f"🗄 Storage: <b>{esc(str(getattr(app_config, 'STORAGE_BACKEND', 'auto')))}</b>"
            f"{' (MongoDB ✅)' if getattr(app_config, 'MONGODB_URI', '') else ' (JSON fallback)'}\n\n"
            "<b>Live stats</b>\n"
            f"👥 Users: <b>{stats['users']}</b> • 🧾 Orders: <b>{stats['orders']}</b>\n"
            f"⚙️ Active orders: <b>{stats['active_orders']}</b>\n"
            f"💰 Revenue: <b>{fmt_money(stats['revenue'])}</b>\n"
            f"👤 Account pool: <b>{stats['accounts_live']}</b>/{stats['accounts_total']} live\n"
            f"📡 Channels: <b>{stats['channels']}</b> • 💳 Pending top-ups: <b>{stats['topups']}</b>\n"
            f"📨 Plan requests: <b>{stats['requests']}</b> waiting\n"
        )
        rows = [
            [btn("💰 Pricing", self.cb_admin("pricing"), PRIMARY),
             btn("🎁 Free Limits", self.cb_admin("limits"), SUCCESS)],
            [btn("👥 Users", self.cb_admin("users"), PRIMARY),
             btn("🧾 Orders", self.cb_admin("orders"), PRIMARY)],
            [btn(f"📨 Plan requests ({stats['requests']})", self.cb_admin("requests"), SUCCESS)],
            [btn(f"💳 Top-ups ({stats['topups']})", self.cb_admin("topups"), SUCCESS),
             btn("📡 Channels", self.cb_admin("channels"), PRIMARY)],
            [btn("👤 Accounts", self.cb_admin("accounts"), PRIMARY),
             btn("😀 Emojis", self.cb_admin("emojis"), PRIMARY)],
            [btn("📣 Broadcast", self.cb_admin("broadcast"), PRIMARY),
             btn("⚙️ Settings", self.cb_admin("settings"), PRIMARY)],
            [btn("🧩 Legacy menu", self.cb_admin("legacy"), PRIMARY),
             btn("🔄 Refresh", self.cb_admin("home"), PRIMARY)],
        ]
        if owner:
            rows.append([btn("📢 Force Subscribe (owner)", self.cb_admin("fs"), SUCCESS)])
            rows.append([btn("✉️ Send PV (owner)", self.cb_admin("sendpv"), DANGER)])
        rows.append([Button.inline("👤 User Panel", "u:home", style=PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def _dashboard_stats(self) -> Dict[str, Any]:
        out = {"users": 0, "orders": 0, "active_orders": 0, "revenue": 0.0,
               "accounts_live": 0, "accounts_total": 0, "channels": 0, "topups": 0,
               "requests": 0}
        try:
            out["users"] = await self.users.count_users()
        except Exception as exc:
            logger.debug(f"users stat failed: {exc}")
        try:
            order_stats = await self.orders.stats()
            out["orders"] = order_stats["total"]
            out["active_orders"] = order_stats["pending"]
            out["revenue"] = order_stats["revenue"]
        except Exception as exc:
            logger.debug(f"orders stat failed: {exc}")
        try:
            account_stats = await self.accounts.stats()
            out["accounts_live"] = account_stats.get("live", 0)
            out["accounts_total"] = account_stats.get("total", 0)
        except Exception as exc:
            logger.debug(f"accounts stat failed: {exc}")
        try:
            out["channels"] = await self.channels.count()
        except Exception as exc:
            logger.debug(f"channels stat failed: {exc}")
        try:
            out["topups"] = len(await self.payments.pending_topups(limit=50))
        except Exception as exc:
            logger.debug(f"topup stat failed: {exc}")
        return out

    def cb_admin(self, action: str, *args: Any) -> str:
        return self.cb(action, *args)

    # ==================================================================
    async def dispatch(self, action: str, args: List[str], event) -> None:
        user_id = int(getattr(event, "sender_id", 0) or 0)
        if not await self.users.is_admin(user_id):
            await self.answer(event, "⛔ Admin only", alert=True)
            return
        await self.ensure_user(event)
        handler = {
            "home": self.show_main,
            "pricing": self.show_pricing,
            "section": self.show_section_plans,
            "plan_edit": self.prompt_plan_field,
            "plan_del": self.delete_plan,
            "plan_add": self.prompt_new_plan,
            "toggle": self.toggle_setting,
            "limits": self.show_limits,
            "limit_edit": self.prompt_limit,
            "comment_texts": self.prompt_comment_texts,
            "comment_texts_reset": self.reset_comment_texts,
            "emojis": self.show_emojis,
            "emoji_add": self.prompt_emoji_add,
            "emoji_del": self.delete_emoji,
            "emoji_reset": self.reset_emojis,
            "users": self.show_users,
            "user": self.show_user,
            "user_role": self.set_user_role,
            "user_ban": self.toggle_ban,
            "user_credit": self.prompt_user_credit,
            "user_prem": self.prompt_user_premium,
            "user_prem_revoke": self.revoke_premium,
            "orders": self.show_orders,
            "order": self.show_order,
            "order_cancel": self.cancel_order,
            "order_retry": self.retry_order,
            "fs": self.show_force_sub,
            "fs_toggle": self.toggle_force_sub,
            "fs_add": self.prompt_force_sub_add,
            "fs_del": self.delete_force_sub,
            "requests": self.show_requests,
            "req_ok": self.approve_request,
            "req_no": self.reject_request,
            "topups": self.show_topups,
            "topup_ok": self.approve_topup,
            "topup_no": self.reject_topup,
            "channels": self.show_channels,
            "chan": self.show_channel,
            "chan_toggle": self.toggle_channel_auto,
            "accounts": self.show_accounts,
            "acct_restore": self.restore_accounts,
            "acct_sync": self.sync_accounts,
            "broadcast": self.prompt_broadcast,
            "settings": self.show_settings,
            "setting_edit": self.prompt_setting,
            "legacy": self.show_legacy,
            "sendpv": self.show_send_pv,
            "cancel": self._cancel,
            "noop": self._noop,
        }.get(action)
        if handler is None:
            logger.warning(f"Unknown admin action: {action}")
            await self.answer(event, "Unknown action", alert=False)
            await self.show_main(event)
            return
        await self._call(handler, event, args)

    @staticmethod
    async def _call(handler, event, args: List[str]) -> None:
        import inspect
        try:
            params = inspect.signature(handler).parameters
        except (TypeError, ValueError):
            params = {}
        if len(params) >= 2:
            await handler(event, args)
        else:
            await handler(event)

    async def _cancel(self, event, args) -> None:
        await self.clear_input(getattr(event, "chat_id", 0) or 0)
        await self.answer(event, "Cancelled")
        await self.show_main(event)

    async def _noop(self, event, args) -> None:
        await self.answer(event, "")

    # ==================================================================
    # input handling
    # ==================================================================
    async def handle_input(self, action: str, text: str, event, data: Dict[str, Any]) -> None:
        if action == "plan_field":
            await self._save_plan_field(event, data, text)
        elif action == "plan_new_title":
            await self._new_plan_step(event, data, text, "title")
        elif action == "plan_new_qty":
            await self._new_plan_step(event, data, text, "quantity")
        elif action == "plan_new_price":
            await self._new_plan_step(event, data, text, "price")
        elif action == "limit_value":
            await self._save_limit(event, data, text)
        elif action == "comment_texts":
            await self._save_comment_texts(event, data, text)
        elif action == "emoji_add_text":
            await self._save_emojis(event, data, text)
        elif action == "user_credit_value":
            await self._save_user_credit(event, data, text)
        elif action == "user_prem_days":
            await self._save_user_premium(event, data, text)
        elif action == "broadcast_text":
            await self._send_broadcast(event, data, text)
        elif action == "setting_value":
            await self._save_setting(event, data, text)
        elif action == "req_days":
            await self._set_request_days(event, data, text)
        elif action == "fs_link":
            await self._save_force_sub_channel(event, data, text)
        elif action == "topup_amount":
            await self._approve_with_amount(event, data, text)
        else:
            logger.warning(f"Unhandled admin input: {action}")
            await self.answer(event, "⚠️ Unexpected input.", alert=True)
            await self.show_main(event)

    # ==================================================================
    # pricing
    # ==================================================================
    async def show_pricing(self, event) -> None:
        settings = await self.settings.get()
        text = "💰 <b>Pricing</b>\n━━━━━━━━━━━━━━━━━━━━\nEverything users pay for. Tap a service to edit its plans.\n"
        rows = []
        for section, title in SECTION_TITLES.items():
            cfg = settings.get(section, {}) or {}
            plans = cfg.get("plans", [])
            enabled = cfg.get("enabled", True)
            text += f"\n{title}: {len(plans)} plans • {'🟢 on' if enabled else '🔴 off'}"
            rows.append([btn(f"{title} ({len(plans)})", self.cb_admin("section", section), PRIMARY)])
        rows.append([btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def show_section_plans(self, event, args) -> None:
        section = args[0] if args else "reactions"
        cfg = await self.settings.section(section)
        plans = cfg.get("plans", [])
        text = f"{SECTION_TITLES.get(section, section)} <b>plans</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        if not plans:
            text += "\nNo plans yet — add one below."
        rows = []
        for plan in plans:
            pid = plan.get("id")
            extra = ""
            if plan.get("auto_posts"):
                extra = f" • {plan.get('auto_posts')} posts"
            if plan.get("days"):
                extra = f" • {plan.get('days')} days"
            if plan.get("slots"):
                extra = f" • +{plan.get('slots')} channel(s)"
            if plan.get("channels"):
                extra += f" • +{plan.get('channels')} channels"
            text += (
                f"\n<b>{esc(str(plan.get('title')))}</b>\n"
                f"  id: <code>{esc(str(pid))}</code>\n"
                f"  price: {fmt_money(plan.get('price'))} • "
                f"{QUANTITY_LABELS.get(section, 'qty')}: {fmt_number(plan.get('quantity'))}{extra}\n"
            )
            rows.append([
                btn("💵 Price", self.cb_admin("plan_edit", section, pid, "price"), PRIMARY),
                btn("📦 " + QUANTITY_LABELS.get(section, "Qty"),
                    self.cb_admin("plan_edit", section, pid, "quantity"), PRIMARY),
                btn("✏️ Title", self.cb_admin("plan_edit", section, pid, "title"), PRIMARY),
                btn("🗑", self.cb_admin("plan_del", section, pid), DANGER),
            ])
        rows.append([btn("➕ Add plan", self.cb_admin("plan_add", section), SUCCESS)])
        if section in ("comments", "reactions", "views"):
            premium_only = cfg.get("premium_only", section == "comments")
            if section == "comments":
                label = "🔒 Premium only: " + ("ON ✅" if premium_only else "OFF")
                rows.append([btn(label, self.cb_admin("toggle", section, "premium_only"), PRIMARY)])
        if section == "comments":
            rows.append([btn(f"👥 Max accounts: {cfg.get('max_accounts', 10)}",
                             self.cb_admin("limit_edit", "comments.max_accounts"), PRIMARY)])
        if section == "channels":
            rows.append([btn(f"📡 Free channels/user: {cfg.get('free_limit', 2)}",
                             self.cb_admin("limit_edit", "channels.free_limit"), PRIMARY)])
            rows.append([btn("🔒 Lock user-added channels: "
                             + ("ON" if cfg.get("lock_added", True) else "OFF"),
                             self.cb_admin("toggle", "channels", "lock_added"), PRIMARY)])
            rows.append([btn("🔄 Auto-start on add: "
                             + ("ON" if cfg.get("auto_enable", True) else "OFF"),
                             self.cb_admin("toggle", "channels", "auto_enable"), SUCCESS)])
        rows.append([btn("⬅️ Back", self.cb_admin("pricing"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def prompt_plan_field(self, event, args) -> None:
        section, plan_id, field = (args + ["", "", ""])[:3]
        plan = await self.settings.get_plan(section, plan_id)
        if plan is None:
            await self.answer(event, "Plan not found", alert=True)
            return
        current = plan.get(field)
        await self.set_input(
            event, "plan_field", {"panel": "admin", "section": section,
                                  "plan_id": plan_id, "field": field},
            prompt=(
                f"✏️ <b>{esc(str(plan.get('title')))}</b> — edit <b>{field}</b>\n\n"
                f"Current value: <code>{esc(str(current))}</code>\n"
                f"Send the new value."
            ),
        )

    async def _save_plan_field(self, event, data: Dict[str, Any], text: str) -> None:
        section, plan_id, field = data.get("section"), data.get("plan_id"), data.get("field")
        plans = await self.settings.plans(section)
        found = False
        for plan in plans:
            if str(plan.get("id")) == str(plan_id):
                if field in ("price", "quantity"):
                    try:
                        value = float(str(text).replace(",", "").strip())
                    except ValueError:
                        raise ValueError("Send a number, e.g. 149")
                    plan[field] = int(value) if field == "quantity" else round(value, 2)
                else:
                    plan[field] = str(text).strip()[:64]
                found = True
                break
        if not found:
            raise ValueError("Plan not found")
        await self.settings.update_section(section, {"plans": plans})
        await self.show_section_plans(event, [section])

    async def prompt_new_plan(self, event, args) -> None:
        section = args[0] if args else "reactions"
        await self.set_input(
            event, "plan_new_title", {"panel": "admin", "section": section, "draft": {}},
            prompt=(
                f"➕ <b>New plan in {SECTION_TITLES.get(section, section)}</b>\n\n"
                "Step 1/3 — send the plan <b>title</b> (shown to users)."
            ),
        )

    async def _new_plan_step(self, event, data: Dict[str, Any], text: str, step: str) -> None:
        section = data.get("section")
        draft = dict(data.get("draft") or {})
        if step == "title":
            draft["title"] = str(text).strip()[:64]
            await self.set_input(
                event, "plan_new_qty", {"panel": "admin", "section": section, "draft": draft},
                prompt="Step 2/3 — send the <b>quantity</b> (e.g. 500 or 1000).",
            )
            return
        if step == "quantity":
            try:
                draft["quantity"] = int(str(text).replace(",", "").strip())
            except ValueError:
                raise ValueError("Send a whole number, e.g. 500")
            await self.set_input(
                event, "plan_new_price", {"panel": "admin", "section": section, "draft": draft},
                prompt="Step 3/3 — send the <b>price</b> in "
                   + str(getattr(app_config, "CURRENCY", "\u20b9")) + " (e.g. 149).",
            )
            return
        try:
            draft["price"] = round(float(str(text).replace(",", "").strip()), 2)
        except ValueError:
            raise ValueError("Send a number, e.g. 149")
        plans = await self.settings.plans(section)
        base_id = "".join(ch for ch in str(draft.get("title", "plan")).lower() if ch.isalnum())[:12] or "plan"
        plan_id = f"{section[:4]}_{base_id}"
        existing_ids = {str(p.get("id")) for p in plans}
        suffix = 1
        while plan_id in existing_ids:
            suffix += 1
            plan_id = f"{section[:4]}_{base_id}{suffix}"
        draft["id"] = plan_id
        draft.setdefault("description", f"{draft['quantity']} units")
        plans.append(draft)
        await self.settings.update_section(section, {"plans": plans})
        await self.render(event, Screen(
            f"✅ Plan created:\n<b>{esc(str(draft['title']))}</b> — "
            f"{fmt_money(draft['price'])} / {fmt_number(draft['quantity'])}",
            [[Button.inline("⬅️ Back to plans", self.cb_admin("section", section), PRIMARY)]],
        ))

    async def delete_plan(self, event, args) -> None:
        section = args[0] if args else ""
        plan_id = args[1] if len(args) > 1 else ""
        plans = [p for p in await self.settings.plans(section) if str(p.get("id")) != str(plan_id)]
        await self.settings.update_section(section, {"plans": plans})
        await self.answer(event, "Plan deleted 🗑")
        await self.show_section_plans(event, [section])

    async def toggle_setting(self, event, args) -> None:
        section = args[0] if args else ""
        key = args[1] if len(args) > 1 else ""
        section_data = await self.settings.section(section or "reactions")
        current = section_data.get(key, False) if section else (await self.settings.get()).get(key, False)
        await self.settings.set_path(f"{section}.{key}" if section else key, not bool(current))
        await self.answer(event, f"{key}: {'off' if current else 'on'}")
        if section in SECTION_TITLES:
            await self.show_section_plans(event, [section])
        else:
            await self.show_limits(event)

    # ==================================================================
    # free limits
    # ==================================================================
    async def show_limits(self, event) -> None:
        settings = await self.settings.get()
        reactions = settings.get("reactions", {})
        views = settings.get("views", {})
        accounts = settings.get("accounts", {})
        comments = settings.get("comments", {})
        auto = reactions.get("auto", {}) or {}
        auto_views = views.get("auto", {}) or {}
        text = (
            "🎁 <b>Free Limits & Automation</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Free reactions (for every user)</b>\n"
            f"• accounts per order: <b>{reactions.get('free_account_limit')}</b>\n"
            f"• orders per day: <b>{reactions.get('free_daily_orders')}</b>\n"
            f"• cooldown: <b>{reactions.get('free_cooldown_seconds')}s</b>\n"
            f"• auto react enabled: <b>{'yes' if (auto.get('enabled')) else 'no'}</b>\n"
            f"• accounts per new post: <b>{auto.get('min_accounts')}–"
            f"{auto.get('accounts_per_post')}</b> (random)\n"
            f"• delay between accounts: <b>{auto.get('delay_min')}–"
            f"{auto.get('delay_max')}s</b>\n"
            f"• private channels auto-join: "
            f"<b>{'yes' if auto.get('join_private_channels') else 'no'}</b>\n\n"
            "<b>Views</b>\n"
            f"• free trial views/order: <b>{views.get('free_quantity')}</b>\n"
            f"• free orders/day: <b>{views.get('free_daily_orders')}</b>\n"
            f"• auto views per post: <b>{auto_views.get('views_per_post')}</b>\n\n"
            "<b>Accounts</b>\n"
            f"• free user cap: <b>{accounts.get('free_limit')}</b>\n"
            f"• premium user cap: <b>{accounts.get('premium_limit')}</b>\n\n"
            "<b>Channels (auto reactions)</b>\n"
            f"• free channels per user: <b>{(settings.get('channels') or {}).get('free_limit', 2)}</b>\n"
            f"• auto-start on add: <b>{'yes' if (settings.get('channels') or {}).get('auto_enable', True) else 'no'}</b>\n"
            f"• lock user-added channels: <b>{'yes' if (settings.get('channels') or {}).get('lock_added', True) else 'no'}</b>\n"
            f"• channel plans: <b>{len((settings.get('channels') or {}).get('plans') or [])}</b>\n\n"
            "<b>Comments</b>\n"
            f"• premium only: <b>{'yes' if comments.get('premium_only') else 'no'}</b>\n"
            f"• max accounts: <b>{comments.get('max_accounts')}</b>\n"
            f"• auto comments: <b>{'yes' if (comments.get('auto') or {}).get('enabled', True) else 'no'}</b>\n"
            f"• auto comments/post: <b>{(comments.get('auto') or {}).get('accounts_per_post')}</b>\n"
            f"• positive comment pool: <b>{len((comments.get('auto') or {}).get('texts') or [])}</b> text(s)\n\n"
            "<b>Plans & pool</b>\n"
            f"• plan covers orders (no extra payment): "
            f"<b>{'yes' if settings.get('premium', {}).get('plan_orders_included', True) else 'no'}</b>\n"
            f"• plan orders/day (0 = unlimited): "
            f"<b>{settings.get('premium', {}).get('plan_orders_per_day', 0)}</b>\n"
            f"• pool supervision every: <b>{accounts.get('supervise_seconds', 60)}s</b>\n"
        )
        rows = [
            [btn(f"❤️ Free accounts/order: {reactions.get('free_account_limit')}",
                 self.cb_admin("limit_edit", "reactions.free_account_limit"), PRIMARY)],
            [btn(f"🔁 Free orders/day: {reactions.get('free_daily_orders')}",
                 self.cb_admin("limit_edit", "reactions.free_daily_orders"), PRIMARY)],
            [btn(f"⏱ Free cooldown: {reactions.get('free_cooldown_seconds')}s",
                 self.cb_admin("limit_edit", "reactions.free_cooldown_seconds"), PRIMARY)],
            [btn(f"📡 Free channels/user: {(settings.get('channels') or {}).get('free_limit', 2)}",
                 self.cb_admin("limit_edit", "channels.free_limit"), SUCCESS),
             btn("📡 Auto-start: " + ("ON" if (settings.get('channels') or {}).get('auto_enable', True) else "OFF"),
                 self.cb_admin("toggle", "channels", "auto_enable"), SUCCESS)],
            [btn("🔒 Lock user channels: " + ("ON" if (settings.get('channels') or {}).get('lock_added', True) else "OFF"),
                 self.cb_admin("toggle", "channels", "lock_added"), PRIMARY),
             btn("📡 Channel plans", self.cb_admin("section", "channels"), PRIMARY)],
            [btn(f"🔄 Max auto accounts/post: {auto.get('accounts_per_post')}",
                 self.cb_admin("limit_edit", "reactions.auto.accounts_per_post"), SUCCESS),
             btn(f"Min: {auto.get('min_accounts')}",
                 self.cb_admin("limit_edit", "reactions.auto.min_accounts"), SUCCESS)],
            [btn(f"⏱ Auto delay min: {auto.get('delay_min')}s",
                 self.cb_admin("limit_edit", "reactions.auto.delay_min"), PRIMARY),
             btn(f"Max: {auto.get('delay_max')}s",
                 self.cb_admin("limit_edit", "reactions.auto.delay_max"), PRIMARY)],
            [btn("🔀 Private-channel auto-join: "
                 + ("ON" if auto.get("join_private_channels") else "OFF"),
                 self.cb_admin("toggle", "reactions", "auto.join_private_channels"), PRIMARY)],
            [btn(f"📈 Auto views/post: {auto_views.get('views_per_post')}",
                 self.cb_admin("limit_edit", "views.auto.views_per_post"), SUCCESS)],
            [btn(f"🎁 Free views: {views.get('free_quantity')}",
                 self.cb_admin("limit_edit", "views.free_quantity"), PRIMARY)],
            [btn(f"👤 Free account cap: {accounts.get('free_limit')}",
                 self.cb_admin("limit_edit", "accounts.free_limit"), PRIMARY),
             btn(f"👑 Premium cap: {accounts.get('premium_limit')}",
                 self.cb_admin("limit_edit", "accounts.premium_limit"), PRIMARY)],
            [btn(f"💬 Comment accounts: {comments.get('max_accounts')}",
                 self.cb_admin("limit_edit", "comments.max_accounts"), PRIMARY),
             btn("🔒 Premium only: " + ("ON" if comments.get("premium_only") else "OFF"),
                 self.cb_admin("toggle", "comments", "premium_only"), PRIMARY)],
            [btn(f"💬 Auto comments/post: {(comments.get('auto') or {}).get('accounts_per_post')}",
                 self.cb_admin("limit_edit", "comments.auto.accounts_per_post"), SUCCESS),
             btn("💬 Auto comments: "
                 + ("ON" if (comments.get('auto') or {}).get('enabled', True) else "OFF"),
                 self.cb_admin("toggle", "comments", "auto.enabled"), SUCCESS)],
            [btn("📝 Edit positive comment pool", self.cb_admin("comment_texts"), PRIMARY),
             btn("↩️ Default comments", self.cb_admin("comment_texts_reset"), PRIMARY)],
            [btn("👑 Plan covers orders: "
                 + ("ON" if settings.get('premium', {}).get('plan_orders_included', True) else "OFF"),
                 self.cb_admin("toggle", "premium", "plan_orders_included"), SUCCESS),
             btn(f"📅 Plan orders/day: {settings.get('premium', {}).get('plan_orders_per_day', 0)}",
                 self.cb_admin("limit_edit", "premium.plan_orders_per_day"), PRIMARY)],
            [btn(f"🩺 Pool check every {accounts.get('supervise_seconds', 60)}s",
                 self.cb_admin("limit_edit", "accounts.supervise_seconds"), PRIMARY)],
            [btn("🔀 Auto reactions master: " + ("ON" if auto.get("enabled") else "OFF"),
                 self.cb_admin("toggle", "reactions", "auto.enabled"), SUCCESS)],
            [btn("⬅️ Back", self.cb_admin("home"), PRIMARY)],
        ]
        await self.render(event, Screen(text, rows))

    async def prompt_limit(self, event, args) -> None:
        path = args[0] if args else ""
        if not path:
            await self.answer(event, "Missing limit", alert=True)
            return
        settings = await self.settings.get()
        node: Any = settings
        for part in path.split("."):
            node = (node or {}).get(part) if isinstance(node, dict) else None
        await self.set_input(
            event, "limit_value", {"panel": "admin", "path": path},
            prompt=(
                f"🎁 <b>{esc(path)}</b>\n\nCurrent value: <code>{esc(str(node))}</code>\n"
                f"Send the new value."
            ),
        )

    async def _save_limit(self, event, data: Dict[str, Any], text: str) -> None:
        path = data.get("path")
        raw = str(text).replace(",", "").strip()
        try:
            value: Any = float(raw)
            value = int(value) if value.is_integer() else value
        except ValueError:
            if raw.lower() in ("true", "on", "yes"):
                value = True
            elif raw.lower() in ("false", "off", "no"):
                value = False
            else:
                raise ValueError("Send a number (or on/off)")
        await self.settings.set_path(path, value)
        await self.render(event, Screen(
            f"✅ <b>{esc(path)}</b> set to <code>{esc(str(value))}</code>",
            [[Button.inline("⬅️ Back to limits", self.cb_admin("limits"), PRIMARY)]],
        ))

    async def prompt_comment_texts(self, event) -> None:
        """Edit the pool of positive comments the bot posts automatically."""
        current = (await self.settings.section("comments")).get("auto", {}) or {}
        texts = [str(t) for t in (current.get("texts") or [])]
        preview = "\n".join(f"• {esc(t)}" for t in texts[:8]) or "• (empty)"
        await self.set_input(
            event, "comment_texts", {"panel": "admin"},
            prompt=(
                "📝 <b>Positive comment pool</b>\n\n"
                f"Current ({len(texts)}):\n{preview}\n\n"
                "Send the new list — <b>one comment per line</b> (or comma "
                "separated). Each account picks a random one, so the comments "
                "never look copy-pasted."
            ),
        )

    async def _save_comment_texts(self, event, data: Dict[str, Any], text: str) -> None:
        raw = str(text).replace(",", "\n").split("\n")
        texts = [line.strip() for line in raw if line.strip()]
        if not texts:
            raise ValueError("Send at least one comment")
        await self.settings.set_path("comments.auto.texts", texts[:50])
        await self.render(event, Screen(
            f"✅ <b>{len(texts[:50])}</b> positive comment(s) saved.",
            [[Button.inline("⬅️ Back to limits", self.cb_admin("limits"), PRIMARY)]],
        ))

    async def reset_comment_texts(self, event) -> None:
        from src.services.settings import DEFAULT_POSITIVE_COMMENTS

        await self.settings.set_path("comments.auto.texts", list(DEFAULT_POSITIVE_COMMENTS))
        await self.answer(event, "Default positive comments restored ✅")
        await self.show_limits(event)

    # ==================================================================
    # emojis
    # ==================================================================
    async def show_emojis(self, event) -> None:
        emojis = await self.settings.emojis()
        cfg = await self.settings.section("reactions")
        text = (
            "😀 <b>Reaction Emojis</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Random mode: <b>{'ON' if cfg.get('random_emoji', True) else 'OFF'}</b>\n\n"
            f"Current list ({len(emojis)}):\n{' '.join(emojis)}\n\n"
            "These are the <b>positive reactions</b> used for free and paid orders, and for "
            "auto reactions on new posts.\n\n"
            "<i>Telegram only accepts its own reaction set — anything else (like 🚀 or ✅) "
            "is refused by the server and will be ignored here.</i>"
        )
        rows = [[btn("➕ Add emojis", self.cb_admin("emoji_add"), SUCCESS),
                 btn("♻️ Reset to default", self.cb_admin("emoji_reset"), PRIMARY)]]
        chunk: List[Any] = []
        for index, emoji in enumerate(emojis):
            chunk.append(btn(f"🗑 {emoji}", self.cb_admin("emoji_del", index), DANGER))
            if len(chunk) == 5:
                rows.append(chunk)
                chunk = []
        if chunk:
            rows.append(chunk)
        rows.append([btn("🔀 Random mode: " + ("ON" if cfg.get("random_emoji", True) else "OFF"),
                         self.cb_admin("toggle", "reactions", "random_emoji"), PRIMARY)])
        rows.append([btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def prompt_emoji_add(self, event) -> None:
        await self.set_input(
            event, "emoji_add_text", {"panel": "admin"},
            prompt=("➕ Send the emoji(s) to add — separate several with spaces or commas.\n\n"
                    "Example: 🚀 🔥 😍 💯"),
        )

    async def _save_emojis(self, event, data: Dict[str, Any], text: str) -> None:
        from src.services.settings import sanitize_reaction_emojis

        new = [e.strip() for e in str(text).replace(",", " ").split() if e.strip()]
        if not new:
            raise ValueError("No emoji found in that message")
        accepted, refused = sanitize_reaction_emojis(new)
        if refused:
            # Telegram only accepts its own reaction set — telling the admin now
            # is much better than losing a whole post's reactions later.
            await self.answer(
                event,
                "⚠️ Telegram does not accept: " + " ".join(refused) +
                " — only its own reaction set works, so they were ignored.",
                alert=True,
            )
        if not accepted:
            await self.answer(event, "None of those emojis can be used as a reaction.", alert=True)
            await self.show_emojis(event)
            return
        current = await self.settings.emojis()
        merged = current + [e for e in accepted if e not in current]
        await self.settings.set_path("reactions.emojis", merged[:60])
        await self.answer(event, f"Added {len(accepted)} emoji(s) ✅")
        await self.show_emojis(event)

    async def delete_emoji(self, event, args) -> None:
        index = int(args[0]) if args and str(args[0]).lstrip("-").isdigit() else -1
        emojis = await self.settings.emojis()
        if 0 <= index < len(emojis):
            removed = emojis.pop(index)
            await self.settings.set_path("reactions.emojis", emojis or list(DEFAULT_POSITIVE_EMOJIS))
            await self.answer(event, f"Removed {removed}")
        await self.show_emojis(event)

    async def reset_emojis(self, event) -> None:
        await self.settings.set_path("reactions.emojis", list(DEFAULT_POSITIVE_EMOJIS))
        await self.answer(event, "Emoji list reset ♻️")
        await self.show_emojis(event)

    # ==================================================================
    # users
    # ==================================================================
    async def show_users(self, event, args=None) -> None:
        page = 0
        if args and args and str(args[0]).isdigit():
            page = int(args[0])
        per_page = 8
        users = await self.users.list_users(limit=per_page + 1, skip=page * per_page)
        has_next = len(users) > per_page
        users = users[:per_page]
        total = await self.users.count_users()
        text = (
            "👥 <b>Users</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Total registered: <b>{total}</b>\n"
        )
        rows = []
        for user in users:
            role = user.get("role", ROLE_USER)
            flag = "👑" if role in (ROLE_OWNER, ROLE_ADMIN) else ("🚫" if user.get("banned") else "👤")
            text += (
                f"\n{flag} <code>{user['_id']}</code> — {esc(str(user.get('first_name') or '—'))}"
                f" • {fmt_money(user.get('balance', 0))}"
                + (" • ♾ unlimited" if (user.get("plan") or {}).get("unlimited")
                   else (" • 👑 " + "/".join(str(k) for k in (user.get("plan") or {}).get("kinds") or []))
                   if (user.get("plan") or {}).get("type") not in (None, "free") else "")
            )
            rows.append([btn(f"{flag} {user.get('first_name') or user['_id']}",
                             self.cb_admin("user", user["_id"]), PRIMARY)])
        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", self.cb_admin("users", page - 1)))
        if has_next:
            nav.append(btn("Next ➡️", self.cb_admin("users", page + 1)))
        if nav:
            rows.append(nav)
        rows.append([btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def show_user(self, event, args) -> None:
        user_id = int(args[0]) if args and str(args[0]).lstrip("-").isdigit() else 0
        user = await self.users.get_user(user_id)
        if not user:
            await self.answer(event, "User not found", alert=True)
            return await self.show_users(event)
        limits = await self.users.effective_limits(user_id)
        orders = await self.orders.list_for_user(user_id, limit=200)
        stats = user.get("stats") or {}
        plan = limits.get("plan") or {}
        kinds = ", ".join(str(k) for k in (plan.get("kinds") or [])) or "—"
        expires = ""
        if plan.get("expires_at"):
            expires = " • till " + fmt_date(plan["expires_at"])
        elif plan.get("unlimited"):
            expires = " • unlimited (no expiry)"
        text = (
            f"👤 <b>User</b> <code>{user_id}</code>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Name: {esc(str(user.get('first_name') or '—'))} @{esc(str(user.get('username') or '—'))}\n"
            f"Role: <b>{user.get('role', ROLE_USER)}</b> • "
            f"Plan: <b>{esc(str(plan.get('type') or 'free'))}</b>{expires}\n"
            f"Services: <b>{esc(kinds)}</b> • account cap: <b>{limits.get('accounts')}</b>\n"
            f"Wallet: <b>{fmt_money(user.get('balance', 0))}</b> • spent: {fmt_money(user.get('total_spent', 0))}\n"
            f"Banned: <b>{'yes 🚫' if user.get('banned') else 'no'}</b>\n\n"
            f"Orders: <b>{len(orders)}</b> • reactions: {fmt_number(stats.get('reactions', 0))}"
            f" • views: {fmt_number(stats.get('views', 0))}\n"
            f"Created: {str(user.get('created_at', ''))[:19]}"
        )
        rows = [
            [btn("💰 Credit wallet", self.cb_admin("user_credit", user_id, "credit"), SUCCESS),
             btn("💸 Debit wallet", self.cb_admin("user_credit", user_id, "debit"), DANGER)],
            [btn("👑 Give plan", self.cb_admin("user_prem", user_id), SUCCESS),
             btn("🚫 Revoke plan", self.cb_admin("user_prem_revoke", user_id), DANGER)],
            [btn("🛡 Make admin", self.cb_admin("user_role", user_id, ROLE_ADMIN), PRIMARY),
             btn("👤 Make user", self.cb_admin("user_role", user_id, ROLE_USER), PRIMARY)],
            [btn("🚫 Ban" if not user.get("banned") else "✅ Unban",
                 self.cb_admin("user_ban", user_id), DANGER)],
            [btn("⬅️ Users", self.cb_admin("users"), PRIMARY)],
        ]
        await self.render(event, Screen(text, rows))

    async def set_user_role(self, event, args) -> None:
        user_id = int(args[0])
        role = args[1] if len(args) > 1 else ROLE_USER
        owner_id = int(getattr(app_config, "OWNER_ID", 0) or 0)
        if user_id == owner_id and role != ROLE_OWNER:
            await self.answer(event, "The owner's role cannot be changed", alert=True)
            return
        await self.users.set_role(user_id, role)
        await self.answer(event, f"Role set to {role}")
        await self.show_user(event, [str(user_id)])

    async def toggle_ban(self, event, args) -> None:
        user_id = int(args[0])
        user = await self.users.get_user(user_id)
        banned = not bool((user or {}).get("banned"))
        await self.users.set_banned(user_id, banned)
        await self.answer(event, "Banned 🚫" if banned else "Unbanned ✅")
        await self.show_user(event, [str(user_id)])

    async def prompt_user_credit(self, event, args) -> None:
        user_id = int(args[0])
        mode = args[1] if len(args) > 1 else "credit"
        await self.set_input(
            event, "user_credit_value", {"panel": "admin", "user_id": user_id, "mode": mode},
            prompt=f"{'💰 Credit' if mode == 'credit' else '💸 Debit'} amount for <code>{user_id}</code>?",
        )

    async def _save_user_credit(self, event, data: Dict[str, Any], text: str) -> None:
        user_id = int(data.get("user_id"))
        mode = data.get("mode", "credit")
        try:
            amount = float(str(text).replace(",", "").strip())
        except ValueError:
            raise ValueError("Send a number, e.g. 100")
        if amount <= 0:
            raise ValueError("Amount must be positive")
        if mode == "credit":
            balance = await self.users.credit(user_id, amount, "admin credit", count_as_topup=False)
            await self.notifier.to_user(user_id, f"💰 {fmt_money(amount)} was added to your wallet by support.\nBalance: {fmt_money(balance)}")
        else:
            ok = await self.users.debit(user_id, amount, "admin debit", count_as_topup=False)
            if not ok:
                raise ValueError("User does not have that much balance")
        await self.show_user(event, [str(user_id)])

    async def prompt_user_premium(self, event, args) -> None:
        """Grant a plan: pick the service, then the duration.

        callback shape: ``a:user_prem:<user_id>[:<kind>:<days>]``.
        ``kind=custom`` asks for the number of days.
        """
        user_id = int(args[0])
        if len(args) > 2:                       # kind + days chosen
            kind, days = args[1], args[2]
            if str(days).isdigit():
                await self._grant_plan_kind(event, user_id, kind, int(days))
                return
        if len(args) > 1 and str(args[1]).isdigit():   # legacy: plain days
            await self._grant_plan_kind(event, user_id, "all", int(args[1]))
            return
        if len(args) > 1 and args[1] == "custom":
            await self.prompt_user_premium_days(event, user_id)
            return
        if len(args) > 1:                        # kind chosen → pick days
            kind = args[1]
            days_choices = [7, 30, 90, 365]
            rows = [[btn(f"{d} days", self.cb_admin("user_prem", user_id, kind, d), SUCCESS)
                     for d in days_choices]]
            rows.append([btn("✏️ Custom days", self.cb_admin("user_prem", user_id, kind, "custom"),
                             PRIMARY)])
            rows.append([btn("⬅️ Back", self.cb_admin("user_prem", user_id), PRIMARY)])
            labels = (await self.settings.section("premium")).get("feature_labels", {})
            await self.render(event, Screen(
                f"👑 <b>{esc(str(labels.get(kind, kind)))}</b> for <code>{user_id}</code>\n\n"
                "How many days should it stay active?", rows))
            return
        rows = [
            [btn("👑 Everything (views + comments + members + channels)",
                 self.cb_admin("user_prem", user_id, "all"), SUCCESS)],
            [btn("🔄 Auto Reactions (beyond free)", self.cb_admin("user_prem", user_id, "reactions"), PRIMARY)],
            [btn("🔄 Auto Views", self.cb_admin("user_prem", user_id, "views"), PRIMARY)],
            [btn("💬 Auto Comments", self.cb_admin("user_prem", user_id, "comments"), PRIMARY)],
            [btn("📡 Extra Channel Slots", self.cb_admin("user_prem", user_id, "channels"), PRIMARY)],
            [btn("👥 Members", self.cb_admin("user_prem", user_id, "members"), PRIMARY)],
        ]
        if await self.is_owner(int(getattr(event, "sender_id", 0) or 0)):
            rows.append([btn("♾ Unlimited (no limits, no expiry)",
                             self.cb_admin("user_prem", user_id, "unlimited"), DANGER)])
        plans = await self.payments.premium_plans()
        for plan in plans[:3]:
            rows.append([btn(f"📦 {plan.get('title')} • {fmt_money(plan.get('price'))}",
                             self.cb_admin("user_prem", user_id, ",".join(
                                 plan.get("kinds") or ["all"]), int(plan.get("days") or 30)))])
        rows.append([btn("⬅️ Back", self.cb_admin("user", user_id), PRIMARY)])
        await self.render(event, Screen(
            f"👑 <b>Give a plan to</b> <code>{user_id}</code>\n\n"
            "Pick the service, then how many days it should run.", rows))

    async def _grant_plan_kind(self, event, user_id: int, kind: str, days: int) -> None:
        """Grant a plan of one kind (or everything / unlimited)."""
        labels = (await self.settings.section("premium")).get("feature_labels", {})
        if kind == "unlimited":
            if not await self.is_owner(int(getattr(event, "sender_id", 0) or 0)):
                await self.answer(event, "Only the owner can grant unlimited", alert=True)
                return
            plan = await self.users.grant_plan(user_id, unlimited=True, note="owner grant")
            # users.grant_plan() already notifies the user with the full details.
            await self.answer(event, "Unlimited plan granted ♾")
        else:
            kinds = self.users.normalise_kinds(kind)
            plan = await self.users.grant_plan(
                user_id, kinds=kinds, days=days, note="admin grant")
            pretty = ", ".join(str(labels.get(k, k)) for k in kinds)
            # users.grant_plan() sends the "plan activated" message itself
            # (services + duration + expiry) so every grant path looks the same.
            await self.answer(event, f"Plan granted for {days} days ({pretty[:40]})")
        logger.info(f"Plan granted to {user_id} by {event.sender_id}: {kind}")
        await self.show_user(event, [str(user_id)])

    async def _grant_premium_days(self, event, user_id: int, days: int) -> None:
        await self._grant_plan_kind(event, user_id, "all", days)

    async def revoke_premium(self, event, args) -> None:
        user_id = int(args[0])
        await self.users.revoke_premium(user_id)
        try:
            await self.notifier.to_user(
                user_id, "ℹ️ Your premium plan was removed by support.\n"
                         "Free auto reactions stay active.")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"revoke notice failed: {exc}")
        await self.answer(event, "Plan revoked")
        await self.show_user(event, [str(user_id)])

    async def prompt_user_premium_days(self, event, user_id: int) -> None:
        await self.set_input(
            event, "user_prem_days", {"panel": "admin", "user_id": user_id},
            prompt="👑 How many days of premium? (e.g. 30)",
        )

    async def _save_user_premium(self, event, data: Dict[str, Any], text: str) -> None:
        user_id = int(data.get("user_id"))
        try:
            days = int(str(text).strip())
        except ValueError:
            raise ValueError("Send a whole number of days, e.g. 30")
        if days <= 0 or days > 3650:
            raise ValueError("Choose between 1 and 3650 days")
        await self._grant_premium_days(event, user_id, days)

    # ==================================================================
    # orders
    # ==================================================================
    async def show_orders(self, event, args=None) -> None:
        page = 0
        if args and args and str(args[0]).isdigit():
            page = int(args[0])
        per_page = 8
        orders = await self.orders.list_all(limit=per_page + 1 + page * per_page)
        orders = orders[page * per_page:(page + 1) * per_page + 1]
        has_next = len(orders) > per_page
        orders = orders[:per_page]
        stats = await self.orders.stats()
        text = (
            "🧾 <b>Orders</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Total: <b>{stats['total']}</b> • completed: {stats['completed']} • "
            f"active: {stats['pending']} • failed: {stats['failed']}\n"
            f"Revenue: <b>{fmt_money(stats['revenue'])}</b>\n"
        )
        rows = []
        for order in orders:
            text += (
                f"\n{status_line(order.get('status'))} • {esc(str(order.get('title') or kind_label(order.get('kind'))))}\n"
                f"  👤 <code>{order.get('user_id')}</code> • {fmt_money(order.get('price'))} • "
                f"qty {fmt_number(order.get('quantity'))}\n"
                f"  🆔 <code>{order['_id']}</code>"
            )
            rows.append([btn(f"{kind_label(order.get('kind'))} • {order.get('status')}",
                             self.cb_admin("order", order["_id"]), PRIMARY)])
        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", self.cb_admin("orders", page - 1)))
        if has_next:
            nav.append(btn("Next ➡️", self.cb_admin("orders", page + 1)))
        if nav:
            rows.append(nav)
        rows.append([btn("🔄 Refresh", self.cb_admin("orders", page), PRIMARY)])
        rows.append([btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def show_order(self, event, args) -> None:
        order_id = args[0] if args else ""
        order = await self.orders.get(order_id)
        if not order:
            await self.answer(event, "Order not found", alert=True)
            return await self.show_orders(event)
        text = "🧾 <b>Order detail</b>\n━━━━━━━━━━━━━━━━━━━━\n" + self.order_block(order, detailed=True)
        payload = order.get("payload") or {}
        if payload:
            text += "\n<b>Payload</b>\n" + "\n".join(
                f"• {esc(str(k))}: <code>{esc(str(v)[:60])}</code>" for k, v in list(payload.items())[:8]
            )
        rows = []
        if order.get("status") in ACTIVE_STATUSES:
            rows.append([btn("🚫 Cancel + refund", self.cb_admin("order_cancel", order_id), DANGER)])
        if order.get("status") in ("failed", "cancelled"):
            rows.append([btn("🔁 Retry", self.cb_admin("order_retry", order_id), SUCCESS)])
        rows.append([btn("⬅️ Orders", self.cb_admin("orders"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def cancel_order(self, event, args) -> None:
        order_id = args[0] if args else ""
        try:
            order = await self.orders.cancel(order_id, by_user=False)
            if order and order.get("status") == "refunded":
                await self.notifier.to_user(order["user_id"],
                                            f"💸 Order <code>{order_id}</code> was cancelled by support and refunded.")
        except ValueError as exc:
            await self.answer(event, str(exc), alert=True)
        await self.show_order(event, [order_id])

    async def retry_order(self, event, args) -> None:
        order_id = args[0] if args else ""
        order = await self.orders.get(order_id)
        if not order:
            await self.answer(event, "Order not found", alert=True)
            return
        price = float(order.get("price") or 0)
        if price > 0:
            charged = await self.users.debit(order["user_id"], price, reason=f"retry:{order['kind']}",
                                             reference=order_id)
            if not charged:
                await self.answer(event, "User has insufficient balance for the retry", alert=True)
                return
        await self.orders.update(order_id, status="pending", progress="Retrying", error=None)
        self.executor.submit({"_id": order_id})
        await self.answer(event, "Order requeued 🔁")
        await self.show_order(event, [order_id])

    # ==================================================================
    # top-ups
    # ==================================================================
    # ==================================================================
    # force subscribe (owner only)
    # ==================================================================
    async def show_force_sub(self, event) -> None:
        user_id = int(getattr(event, "sender_id", 0) or 0)
        if not await self.users.is_owner(user_id):
            await self.answer(event, "⛔ Owner only", alert=True)
            return await self.show_main(event)
        service = get_force_sub_service()
        cfg = await service.config()
        channels = cfg["channels"]
        text = (
            "📢 <b>FORCE SUBSCRIBE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"State: <b>{'🟢 ON' if cfg['enabled'] else '🔴 OFF'}</b> • "
            f"channels: <b>{len(channels)}</b>\n"
            f"Cache: <b>{cfg['cache_seconds']}s</b>\n\n"
            "A user must join <b>every</b> channel below before the bot answers. "
            "Owners and admins are never blocked, and if a channel cannot be checked "
            "the user is let through (so a mistake can never lock everybody out).\n\n"
            "<i>The bot must be a member of each channel — for private ones add the "
            "bot first, then send the invite link.</i>\n"
        )
        rows: List[List[Any]] = []
        if channels:
            text += "\n<b>Channels</b>\n"
            for doc in channels:
                link = service.join_link(doc)
                state = "🟢" if doc.get("enabled", True) else "⚪️"
                text += (f"{state} <b>{esc(str(doc.get('title')))}</b> "
                         f"<code>{doc.get('id')}</code>\n"
                         + (f"   {esc(link)}\n" if link else ""))
                rows.append([
                    btn(f"🗑 {str(doc.get('title'))[:22]}",
                        self.cb_admin("fs_del", doc.get("id")), DANGER),
                ])
        else:
            text += "\n<i>No channels yet — add one below.</i>\n"
        rows.append([btn(("🔴 Turn OFF" if cfg["enabled"] else "🟢 Turn ON"),
                         self.cb_admin("fs_toggle"),
                         (DANGER if cfg["enabled"] else SUCCESS))])
        rows.append([btn("➕ Add channel", self.cb_admin("fs_add"), SUCCESS)])
        rows.append([btn("🔄 Refresh", self.cb_admin("fs"), PRIMARY),
                     btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def toggle_force_sub(self, event) -> None:
        if not await self.users.is_owner(int(getattr(event, "sender_id", 0) or 0)):
            await self.answer(event, "⛔ Owner only", alert=True)
            return
        service = get_force_sub_service()
        enabled = await service.toggle()
        if enabled and not (await service.channels()):
            await self.answer(event, "Add a channel first", alert=True)
            await service.set_enabled(False)
        else:
            await self.answer(event, f"Force subscribe {'ON ✅' if enabled else 'OFF'}")
        await self.show_force_sub(event)

    async def prompt_force_sub_add(self, event) -> None:
        if not await self.users.is_owner(int(getattr(event, "sender_id", 0) or 0)):
            await self.answer(event, "⛔ Owner only", alert=True)
            return
        await self.set_input(
            event, "fs_link", {"panel": "admin"},
            prompt=("➕ <b>Add a force-subscribe channel</b>\n\n"
                    "Send <b>@username</b>, a <b>t.me</b> link, a private invite link "
                    "(the bot must already be a member) or a <b>-100…</b> channel id.\n"
                    "You can add a title like this: <code>@mychannel | Updates</code>"),
        )

    async def _save_force_sub_channel(self, event, data: Dict[str, Any], text: str) -> None:
        raw, _, title = str(text or "").partition("|")
        service = get_force_sub_service()
        try:
            doc = await service.add_channel(raw.strip(), title=title.strip(),
                                            added_by=int(event.sender_id))
        except ValueError as exc:
            await self.render(event, Screen(
                f"⚠️ <b>Could not add that channel</b>\n\n{esc(str(exc))}",
                [[btn("➕ Try again", self.cb_admin("fs_add"), PRIMARY)],
                 [btn("📢 Force Subscribe", self.cb_admin("fs"), PRIMARY)]],
            ))
            return
        await self.render(event, Screen(
            "✅ <b>Channel added</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 <b>{esc(str(doc.get('title')))}</b>\n"
            f"🆔 <code>{doc.get('id')}</code>\n"
            + (f"🔗 @{esc(str(doc.get('username')))}\n" if doc.get("username") else ""),
            [[btn("📢 Force Subscribe", self.cb_admin("fs"), PRIMARY)],
             [btn("⬅️ Back", self.cb_admin("home"), PRIMARY)]],
        ))

    async def delete_force_sub(self, event, args) -> None:
        if not await self.users.is_owner(int(getattr(event, "sender_id", 0) or 0)):
            await self.answer(event, "⛔ Owner only", alert=True)
            return
        removed = await get_force_sub_service().remove_channel(args[0] if args else "")
        await self.answer(event, "Channel removed ✅" if removed else "Not found", alert=not removed)
        await self.show_force_sub(event)

    # ==================================================================
    # plan requests (user → owner)
    # ==================================================================
    async def show_requests(self, event) -> None:
        requests = get_plan_request_service(users=self.users)
        pending = await requests.list(status="pending", limit=10)
        recent = await requests.list(limit=10)
        text = (
            "📨 <b>PLAN REQUESTS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ Waiting: <b>{len(pending)}</b>\n\n"
            "A user tapped <i>Request activation</i> in the bot. Take the payment, "
            "then approve here — the plan (days, accounts, channels) is granted "
            "exactly as requested and the user is notified.\n"
        )
        rows: List[List[Any]] = []
        for doc in pending:
            text += (
                f"\n▸ <b>{esc(str(doc.get('title')))}</b> — <code>{doc.get('user_id')}</code>\n"
                f"   💵 {fmt_money(doc.get('price'))}"
                + (f" • 🗓 {doc.get('days')}d" if doc.get("days") else "")
                + (f" • 📡 +{doc.get('channels')}" if doc.get("channels") else "")
                + (f" • 👥 {doc.get('accounts')}" if doc.get("accounts") else "")
                + (f"\n   📝 {esc(str(doc.get('note')))}" if doc.get("note") else "")
                + "\n"
            )
            rows.append([
                btn("✅ Approve", self.cb_admin("req_ok", doc["_id"]), SUCCESS),
                btn("❌ Reject", self.cb_admin("req_no", doc["_id"]), DANGER),
                btn("⚙️ Custom", self.cb_admin("req_ok", doc["_id"], "custom"), PRIMARY),
            ])
        if not pending:
            text += "\n<i>No pending requests.</i>\n"
        if recent:
            text += "\n<b>Recent</b>\n"
            for doc in recent[:6]:
                emoji = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(doc.get("status"), "•")
                text += (f"{emoji} {esc(str(doc.get('title')))} — <code>{doc.get('user_id')}</code>"
                         f" ({str(doc.get('created_at'))[:10]})\n")
        rows.append([btn("🔄 Refresh", self.cb_admin("requests"), PRIMARY),
                     btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def approve_request(self, event, args) -> None:
        request_id = args[0] if args else ""
        custom = len(args) > 1 and args[1] == "custom"
        requests = get_plan_request_service(users=self.users)
        doc = await requests.get(request_id)
        if doc is None:
            await self.answer(event, "Request not found", alert=True)
            return await self.show_requests(event)
        if custom:
            await self.set_input(
                event, "req_days",
                {"panel": "admin", "request_id": request_id},
                prompt=(f"⚙️ <b>Custom approval</b>\n\n"
                        f"Plan: <b>{esc(str(doc.get('title')))}</b> for "
                        f"<code>{doc.get('user_id')}</code>\n\n"
                        "Send <b>days:accounts:channels</b> — e.g. <code>30:25:5</code> "
                        "(use 0 to skip a field)."),
            )
            return
        outcome = await requests.approve(request_id, by=int(event.sender_id),
                                         notify=True)
        if outcome is None:
            await self.answer(event, "Request not found", alert=True)
        else:
            await self.answer(event, "Plan activated ✅")
        await self.show_requests(event)

    async def _set_request_days(self, event, data: Dict[str, Any], text: str) -> None:
        parts = [p.strip() for p in str(text).replace(",", ":").split(":")]
        def _int(index):
            try:
                return int(parts[index]) if index < len(parts) and parts[index] else 0
            except ValueError:
                return 0
        days, accounts, channels = _int(0), _int(1), _int(2)
        requests = get_plan_request_service(users=self.users)
        outcome = await requests.approve(
            str(data.get("request_id")), by=int(event.sender_id),
            days=days or None, accounts=accounts or None, channels=channels or None,
        )
        doc = (outcome or {}).get("request") or {}
        await self.render(event, Screen(
            "✅ <b>Plan activated</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <code>{doc.get('user_id')}</code>\n"
            f"👑 {esc(str(doc.get('title')))}"
            + (f" • 🗓 {days}d" if days else "")
            + (f" • 👥 {accounts}" if accounts else "")
            + (f" • 📡 +{channels}" if channels else ""),
            [[btn("📨 Plan requests", self.cb_admin("requests"), PRIMARY)],
             self.home_row("admin")],
        ))

    async def reject_request(self, event, args) -> None:
        request_id = args[0] if args else ""
        await get_plan_request_service(users=self.users).reject(
            request_id, by=int(event.sender_id), reason="Declined by the owner")
        await self.answer(event, "Request declined")
        await self.show_requests(event)

    async def show_topups(self, event) -> None:
        pending = await self.payments.pending_topups(limit=15)
        income = await self.payments.income_stats()
        text = (
            "💳 <b>Top-ups</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Pending: <b>{len(pending)}</b>\n"
            f"Approved total: <b>{fmt_money(income['topups'])}</b> ({income['topup_count']})\n"
            f"Premium revenue: <b>{fmt_money(income['premium_revenue'])}</b>\n"
        )
        rows = []
        for request in pending:
            text += (
                f"\n👤 <code>{request.get('user_id')}</code> — <b>{fmt_money(request.get('amount', 0))}</b>\n"
                f"  🔖 UTR: <code>{esc(str(request.get('utr')))}</code>\n"
                f"  🆔 <code>{request['_id']}</code>"
            )
            rows.append([
                btn(f"✅ {fmt_money(request.get('amount', 0))}",
                    self.cb_admin("topup_ok", request["_id"]), SUCCESS),
                btn("✏️ Amount", self.cb_admin("topup_amount", request["_id"]), PRIMARY),
                btn("❌", self.cb_admin("topup_no", request["_id"]), DANGER),
            ])
        if not pending:
            text += "\n\nNo pending requests 🎉"
        rows.append([btn("🔄 Refresh", self.cb_admin("topups"), PRIMARY)])
        rows.append([btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def approve_topup(self, event, args) -> None:
        request_id = args[0] if args else ""
        try:
            request = await self.payments.approve_topup(request_id, admin_id=event.sender_id)
        except ValueError as exc:
            await self.answer(event, str(exc), alert=True)
            return await self.show_topups(event)
        balance = await self.users.balance(request["user_id"])
        await self.notifier.to_user(
            request["user_id"],
            f"✅ <b>Top-up approved</b>\n\nAmount: {fmt_money(request.get('amount', 0))}\n"
            f"New balance: <b>{fmt_money(balance)}</b>",
        )
        await self.answer(event, "Approved ✅")
        await self.show_topups(event)

    async def reject_topup(self, event, args) -> None:
        request_id = args[0] if args else ""
        try:
            request = await self.payments.reject_topup(request_id, "rejected by admin",
                                                       admin_id=event.sender_id)
            await self.notifier.to_user(
                request["user_id"],
                f"❌ Top-up <code>{request_id}</code> was rejected.\n"
                "If you believe this is a mistake, contact support with your UTR.",
            )
        except ValueError as exc:
            await self.answer(event, str(exc), alert=True)
        await self.show_topups(event)

    async def prompt_topup_amount(self, event, args) -> None:
        request_id = args[0] if args else ""
        await self.set_input(
            event, "topup_amount", {"panel": "admin", "request_id": request_id},
            prompt="✏️ Send the amount to credit for this top-up:",
        )

    async def _approve_with_amount(self, event, data: Dict[str, Any], text: str) -> None:
        request_id = data.get("request_id")
        try:
            amount = float(str(text).replace(",", "").strip())
        except ValueError:
            raise ValueError("Send a number, e.g. 100")
        try:
            request = await self.payments.approve_topup(request_id, admin_id=event.sender_id,
                                                        amount_override=amount)
        except ValueError as exc:
            raise ValueError(str(exc))
        await self.notifier.to_user(
            request["user_id"],
            f"✅ <b>Top-up approved</b>\n\nAmount: {fmt_money(amount)}",
        )
        await self.show_topups(event)

    # ==================================================================
    # channels
    # ==================================================================
    async def show_channels(self, event, args=None) -> None:
        page = int(args[0]) if args and str(args[0]).isdigit() else 0
        per_page = 8
        channels = await self.channels.list_all(limit=per_page + 1 + page * per_page)
        channels = channels[page * per_page:(page + 1) * per_page + 1]
        has_next = len(channels) > per_page
        channels = channels[:per_page]
        stats = await self.channels.stats()
        text = (
            "📡 <b>Channels</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Total: <b>{stats['total']}</b> • auto reactions: {stats['auto_reactions']} • "
            f"auto views: {stats['auto_views']}\n"
            f"Posts seen: <b>{fmt_number(stats['posts_seen'])}</b>\n"
        )
        rows = []
        for channel in channels:
            ar = channel.get("auto_reactions") or {}
            av = channel.get("auto_views") or {}
            text += (
                f"\n📢 <b>{esc(str(channel.get('title')))}</b>\n"
                f"  👤 owner: <code>{channel.get('owner_id')}</code> • "
                f"r:{'🟢' if ar.get('enabled') else '🔴'} v:{'🟢' if av.get('enabled') else '🔴'}\n"
                f"  🆔 <code>{channel['_id']}</code>"
            )
            rows.append([btn(f"⚙️ {channel.get('title')}", self.cb_admin("chan", channel["_id"]), PRIMARY)])
        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", self.cb_admin("channels", page - 1)))
        if has_next:
            nav.append(btn("Next ➡️", self.cb_admin("channels", page + 1)))
        if nav:
            rows.append(nav)
        rows.append([btn("⬅️ Back", self.cb_admin("home"), PRIMARY)])
        await self.render(event, Screen(text, rows))

    async def show_channel(self, event, args) -> None:
        channel_id = int(args[0]) if args else 0
        channel = await self.channels.get(channel_id)
        if not channel:
            await self.answer(event, "Channel not found", alert=True)
            return await self.show_channels(event)
        ar = channel.get("auto_reactions") or {}
        av = channel.get("auto_views") or {}
        stats = channel.get("stats") or {}
        text = (
            f"📡 <b>{esc(str(channel.get('title')))}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <code>{channel['_id']}</code>\n"
            f"👤 owner: <code>{channel.get('owner_id')}</code>\n"
            f"🔗 @{esc(str(channel.get('username') or '—'))}\n\n"
            "<b>Automation</b>\n"
            f"• reactions: {'🟢 on' if ar.get('enabled') else '🔴 off'} "
            f"(accounts: {ar.get('accounts') or 'auto'}, posts left: {ar.get('posts_left')})\n"
            f"• views: {'🟢 on' if av.get('enabled') else '🔴 off'} "
            f"(per post: {av.get('views_per_post') or 'auto'}, posts left: {av.get('posts_left')})\n\n"
            "<b>Stats</b>\n"
            f"• posts seen: {fmt_number(stats.get('posts_seen', 0))}\n"
            f"• reactions sent: {fmt_number(stats.get('reactions_sent', 0))}\n"
            f"• views sent: {fmt_number(stats.get('views_sent', 0))}\n"
        )
        rows = [
            [btn("🔄 Toggle reactions", self.cb_admin("chan_toggle", channel_id, "reactions"), PRIMARY),
             btn("📈 Toggle views", self.cb_admin("chan_toggle", channel_id, "views"), PRIMARY)],
            [btn("⬅️ Channels", self.cb_admin("channels"), PRIMARY)],
        ]
        await self.render(event, Screen(text, rows))

    async def toggle_channel_auto(self, event, args) -> None:
        channel_id = int(args[0])
        kind = args[1] if len(args) > 1 else "reactions"
        channel = await self.channels.get(channel_id)
        if not channel:
            await self.answer(event, "Channel not found", alert=True)
            return
        if kind == "reactions":
            enabled = not bool((channel.get("auto_reactions") or {}).get("enabled"))
            await self.channels.set_auto_reactions(channel_id, enabled)
        else:
            enabled = not bool((channel.get("auto_views") or {}).get("enabled"))
            await self.channels.set_auto_views(channel_id, enabled)
        await self.answer(event, f"{kind}: {'on' if enabled else 'off'}")
        await self.show_channel(event, [str(channel_id)])

    # ==================================================================
    # accounts (pool)
    # ==================================================================
    async def show_accounts(self, event) -> None:
        stats = await self.accounts.stats()
        docs = await self.accounts.store.list_accounts(limit=20)
        text = (
            "👤 <b>Account Pool</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Total: <b>{stats['total']}</b> • live: <b>{stats.get('live', 0)}</b> • "
            f"revoked: <b>{stats['revoked']}</b> • inactive: {stats['inactive']}\n\n"
            "Session strings are stored in the database, so accounts survive a crash or a "
            "restart even if the session files are gone.\n"
        )
        for doc in docs[:12]:
            status = doc.get("status", "active")
            icon = {"active": "🟢", "inactive": "🟡", "revoked": "🔴", "error": "⚠️"}.get(status, "⚪")
            text += (
                f"\n{icon} <b>{esc(str(doc.get('name') or doc['_id']))}</b>\n"
                f"  📱 <code>{esc(str(doc.get('phone') or '—'))}</code> • "
                f"{'@' + esc(str(doc.get('username'))) if doc.get('username') else 'no username'}\n"
                f"  👍 {doc.get('reactions_done', 0)} • 📈 {doc.get('views_done', 0)} • 🚪 {doc.get('joins_done', 0)}"
            )
        rows = [
            [Button.inline("➕ Add account (full login)", "add_account", style=SUCCESS)],
            [btn("⬇️ Restore from DB", self.cb_admin("acct_restore"), PRIMARY),
             btn("🔄 Sync live", self.cb_admin("acct_sync"), PRIMARY)],
            [btn("📋 Legacy account list", "account_management", PRIMARY)],
            [btn("⬅️ Back", self.cb_admin("home"), PRIMARY)],
        ]
        await self.render(event, Screen(text, rows))

    async def restore_accounts(self, event) -> None:
        restored = await self.accounts.restore_missing_clients()
        await self.answer(event, f"Restored {restored} account(s) from the database")
        await self.show_accounts(event)

    async def sync_accounts(self, event) -> None:
        count = await self.accounts.sync_from_live_clients()
        await self.answer(event, f"Synced {count} live account(s) into the database")
        await self.show_accounts(event)

    # ==================================================================
    # broadcast / settings / legacy
    # ==================================================================
    async def prompt_broadcast(self, event) -> None:
        await self.set_input(
            event, "broadcast_text", {"panel": "admin"},
            prompt=("📣 <b>Broadcast</b>\n\nSend the message to deliver to every registered user.\n"
                    "HTML is supported (<b>bold</b>, <i>italic</i>, <code>code</code>)."),
        )

    async def _send_broadcast(self, event, data: Dict[str, Any], text: str) -> None:
        users = await self.users.list_users(limit=5000)
        ids = [int(u["_id"]) for u in users if not u.get("banned")]
        await self.render(event, Screen(
            f"📣 Broadcasting to <b>{len(ids)}</b> users…",
            [[btn("⬅️ Back", self.cb_admin("home"), PRIMARY)]],
        ))
        result = await self.notifier.broadcast(ids, text)
        await self.render(event, Screen(
            f"📣 <b>Broadcast finished</b>\n\n✅ Sent: {result['sent']}\n❌ Failed: {result['failed']}",
            [[btn("⬅️ Back", self.cb_admin("home"), PRIMARY)]],
        ))

    async def show_settings(self, event) -> None:
        settings = await self.settings.get()
        payments = settings.get("payments", {})
        support = settings.get("support", {})
        panel_cfg = settings.get("panel", {})
        orders_cfg = settings.get("orders", {})
        text = (
            "⚙️ <b>Settings</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 UPI id: <code>{esc(str(payments.get('upi_id') or '—'))}</code>\n"
            f"💳 UPI name: {esc(str(payments.get('upi_name') or '—'))}\n"
            f"🆘 Support: @{esc(str(support.get('username') or '—'))}\n"
            f"📣 Announcement: {esc(str(panel_cfg.get('announcement') or '—'))}\n"
            f"🔥 Welcome message: <b>{'set ✅' if panel_cfg.get('welcome') else 'empty ⚠️'}</b>\n"
            f"💸 Refund on failure: <b>{'yes' if orders_cfg.get('refund_on_failure') else 'no'}</b>\n"
            f"🔢 Max active orders/user: <b>{orders_cfg.get('max_active_per_user')}</b>\n"
            f"🎁 Min top-up: <b>{fmt_money(payments.get('min_topup', 0))}</b>\n\n"
            f"🗄 Storage backend: <b>{esc(str(getattr(app_config, 'STORAGE_BACKEND', 'auto')))}</b>\n"
            f"🔌 MongoDB: <b>{'configured ✅' if getattr(app_config, 'MONGODB_URI', '') else 'not configured ⚠️'}</b>\n"
        )
        rows = [
            [btn("💳 UPI id", self.cb_admin("setting_edit", "payments.upi_id"), PRIMARY),
             btn("👤 UPI name", self.cb_admin("setting_edit", "payments.upi_name"), PRIMARY)],
            [btn("🆘 Support user", self.cb_admin("setting_edit", "support.username"), PRIMARY)],
            [btn("📣 Announcement", self.cb_admin("setting_edit", "panel.announcement"), PRIMARY)],
            [btn("🔥 Welcome message", self.cb_admin("setting_edit", "panel.welcome"), PRIMARY)],
            [btn("🎁 Min top-up", self.cb_admin("setting_edit", "payments.min_topup"), PRIMARY),
             btn("🔢 Max active orders", self.cb_admin("setting_edit", "orders.max_active_per_user"), PRIMARY)],
            [btn("💸 Refund on failure: " + ("ON" if orders_cfg.get("refund_on_failure") else "OFF"),
                 self.cb_admin("toggle", "orders", "refund_on_failure"), PRIMARY)],
            [btn("⬅️ Back", self.cb_admin("home"), PRIMARY)],
        ]
        await self.render(event, Screen(text, rows))

    async def prompt_setting(self, event, args) -> None:
        path = args[0] if args else ""
        settings = await self.settings.get()
        node: Any = settings
        for part in path.split("."):
            node = (node or {}).get(part) if isinstance(node, dict) else None
        await self.set_input(
            event, "setting_value", {"panel": "admin", "path": path},
            prompt=f"⚙️ <b>{esc(path)}</b>\n\nCurrent: <code>{esc(str(node))}</code>\n\nSend the new value.",
        )

    async def _save_setting(self, event, data: Dict[str, Any], text: str) -> None:
        path = data.get("path")
        raw = str(text).strip()
        value: Any = raw
        if path in ("payments.min_topup", "orders.max_active_per_user"):
            try:
                value = float(raw.replace(",", ""))
                value = int(value) if value.is_integer() else value
            except ValueError:
                raise ValueError("Send a number")
        if path == "support.username":
            value = raw.lstrip("@")
        await self.settings.set_path(path, value)
        await self.show_settings(event)

    async def show_legacy(self, event) -> None:
        text = (
            "🧩 <b>Legacy tools</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "The original monitoring and bulk operation menus.\n\n"
            "• 📡 Monitor mode — keyword filtering + forwarding\n"
            "• 🎯 Individual — run one action from one account\n"
            "• ⚡ Bulk — run an action from many accounts\n"
            "• 📊 Reports — stats and report status"
        )
        rows = [
            [Button.inline("📡 Monitor Mode", "monitor_mode", style=PRIMARY)],
            [Button.inline("🎯 Individual", "individual_keyboard", style=PRIMARY),
             Button.inline("⚡ Bulk", "bulk_operations", style=PRIMARY)],
            [Button.inline("📊 Report Status", "report", style=PRIMARY)],
            [btn("⬅️ Back", self.cb_admin("home"), PRIMARY)],
        ]
        await self.render(event, Screen(text, rows))

    async def show_send_pv(self, event) -> None:
        user_id = int(getattr(event, "sender_id", 0) or 0)
        if not await self.is_owner(user_id):
            await self.answer(event, "⛔ Owner only", alert=True)
            return
        text = (
            "✉️ <b>Send PV (owner only)</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "This flow stays exclusive to the owner account and is hidden from every "
            "other admin and all users.\n\n"
            "You'll pick one of your signed-in accounts, then the target user, then the message."
        )
        rows = [
            [Button.inline("✉️ Start Send PV", "send_pv", style=DANGER)],
            [btn("⬅️ Back", self.cb_admin("home"), PRIMARY)],
        ]
        await self.render(event, Screen(text, rows))
