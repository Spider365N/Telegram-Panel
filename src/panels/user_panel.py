"""
User panel — what every non-admin sees after /start.

Layout mirrors the reference design:

    📈 Order Views      🔄 Auto Views
    ❤️ Order Reactions  🔄 Auto Reactions
    👤 Account Info     👥 Members
    💳 Add Money        🆘 Support
    🧾 Order Management (+ Add To Channel / Add To Group, Premium, Accounts)

Everything reads its prices and limits from the admin-editable settings, so the
owner can re-price or disable any feature without touching code.
"""
from __future__ import annotations

import logging
from html import escape as esc

from src.utils import html_link, tg_link
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
    progress_bar,
    status_line,
)
from src.services.autoengage import get_auto_engagement
from src.services.forcesub import get_force_sub_service
from src.services.requests import get_plan_request_service
from src.services.orders import ACTIVE_STATUSES

logger = logging.getLogger(__name__)


def btn(label: str, action: str, style: Optional[str] = None):
    return Button.inline(label, action, style=style) if style else Button.inline(label, action)


class UserPanel(PanelBase):
    prefix = "u:"

    # ==================================================================
    # main menu
    # ==================================================================
    # ==================================================================
    # force subscribe (owner-configured join gate)
    # ==================================================================
    async def force_sub_gate(self, event) -> bool:
        """True when the user may continue, False when the join screen was shown."""
        user_id = int(getattr(event, "sender_id", 0) or 0)
        if not user_id:
            return True
        try:
            service = get_force_sub_service()
            if not await service.enabled():
                return True
            if await self.users.is_admin(user_id):
                return True                      # owners/admins are never blocked
            verdict = await service.check(user_id)
        except Exception as exc:  # noqa: BLE001 - never lock the bot out
            logger.debug(f"force-sub gate skipped: {exc}")
            return True
        if verdict.get("ok"):
            return True
        await self.show_force_sub(event, verdict.get("missing") or [])
        return False

    async def show_force_sub(self, event, missing: List[Dict[str, Any]]) -> None:
        cfg = await get_force_sub_service().config()
        text = (
            f"<b>{esc(cfg['title'])}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            + esc(cfg["message"]) + "\n"
        )
        rows: List[List[Any]] = []
        for doc in missing:
            link = get_force_sub_service().join_link(doc)
            label = f"📢 {doc.get('title') or doc.get('id')}"
            if link:
                rows.append([Button.url(label, link)])
            else:
                text += f"\n• <b>{esc(str(doc.get('title')))}</b> (ask the owner for the link)"
        rows.append([btn("✅ I joined — verify", self.cb("fscheck"), SUCCESS)])
        await self.render(event, Screen(text, rows))

    async def recheck_force_sub(self, event) -> None:
        """The *I joined* button: forget the cached answer and check again."""
        user_id = int(getattr(event, "sender_id", 0) or 0)
        service = get_force_sub_service()
        service.invalidate(user_id)
        try:
            verdict = await service.check(user_id, use_cache=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"force-sub recheck failed: {exc}")
            verdict = {"ok": True}
        if verdict.get("ok"):
            await self.answer(event, "Thanks! You are in ✅")
            await self.show_home(event, welcome=True)
        else:
            await self.answer(event, "Not yet — join every channel above first", alert=True)
            await self.show_force_sub(event, verdict.get("missing") or [])

    async def show_home(self, event, welcome: bool = False) -> None:
        user = await self.ensure_user(event)
        settings = await self.settings.get()
        panel_cfg = settings.get("panel", {})
        premium = await self.users.is_premium(user["_id"])
        limits = await self.users.effective_limits(user["_id"])
        pool = await self.accounts.available_count()

        announce = panel_cfg.get("announcement") or ""
        welcome_text = panel_cfg.get("welcome") or ""
        text = ""
        if welcome and welcome_text:
            text += f"{welcome_text}\n\n━━━━━━━━━━━━━━━━━━━━\n"
        text += (
            "🚀 <b>TELEGRAM PANEL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>{esc(user.get('first_name') or 'User')}</b>"
            f"{' • 👑 Premium' if premium else ' • Free plan'}\n"
            f"💳 Wallet: <b>{fmt_money(user.get('balance', 0))}</b>\n"
            f"👥 Accounts in pool: <b>{pool}</b>\n"
        )
        if announce:
            text += f"\n📣 {esc(announce)}\n"
        owned = await self.channels_of(user["_id"])
        active = len([c for c in owned if (c.get("auto_reactions") or {}).get("enabled")])
        text += (
            f"📡 Your channels: <b>{len(owned)}</b> • auto reactions on: <b>{active}</b>\n"
            f"🎁 Free: auto reactions up to <b>{limits.get('free_reaction_accounts')}</b> "
            f"accounts per post\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Pick a service 👇"
        )

        rows: List[List[Any]] = [
            [
                btn("🔄 Auto Reactions 🎁", self.cb("ar"), SUCCESS),
                btn("❤️ Order Reactions", self.cb("or"), PRIMARY),
            ],
            [
                btn("📋 My Channels", self.cb("ch"), PRIMARY),
                btn("➕ Add To Channel", self.cb("addch"), SUCCESS),
            ],
            [
                btn("🔄 Auto Views 👑", self.cb("av"), PRIMARY),
                btn("📈 Order Views 👑", self.cb("ov"), SUCCESS),
            ],
            [
                btn("💬 Comments 👑", self.cb("cm"), PRIMARY),
                btn("💬 Auto Comments 👑", self.cb("ac"), PRIMARY),
            ],
            [
                btn("👥 Members 👑", self.cb("mb"), PRIMARY),
                btn("👤 Account Info", self.cb("ai")),
            ],
            [
                btn("👑 Buy Premium", self.cb("prem"), SUCCESS),
                btn("🧾 My Orders", self.cb("om"), PRIMARY),
            ],
            [
                btn("🆘 Support", self.cb("sp")),
                btn("➕ Add To Group", self.cb("addgr"), SUCCESS),
            ],
        ]
        is_admin = await self.users.is_admin(user["_id"])
        if is_admin:
            text += (
                "\n👑 <b>God mode</b> — you are admin/owner: every feature is "
                "unlocked, no payment, no limits."
            )
        if premium:
            kinds = ", ".join(str(k) for k in (limits.get("plan") or {}).get("kinds") or [])
            text += (
                f"\n👑 <b>{(limits.get('plan') or {}).get('type', 'premium').title()} plan</b>"
                + (f" • {kinds}" if kinds else "")
                + "\n✅ Covered services run <b>without extra payment</b>."
            )
            # one "My Plan" row: the wallet row already carries "Buy Premium"
            rows[-2] = [btn("👑 My Plan", self.cb("prem"), PRIMARY),
                        btn("🧾 My Orders", self.cb("om"), PRIMARY)]
        if await self.users.is_admin(user["_id"]):
            rows.append([btn("🛠 Admin Panel", "a:home", DANGER)])
        await self.render(event, Screen(text, rows))

    # ==================================================================
    # dispatch
    # ==================================================================
    async def dispatch(self, action: str, args: List[str], event) -> None:
        user_id = int(getattr(event, "sender_id", 0) or 0)
        if action != "home":
            await self.ensure_user(event)

        handler = {
            "home": self.show_home,
            "ov": self.show_order_views,
            "av": self.show_auto_views,
            "ac": self.show_auto_comments,
            "or": self.show_order_reactions,
            "ar": self.show_auto_reactions,
            "ai": self.show_account_info,
            "mb": self.show_members,
            "sp": self.show_support,
            "om": self.show_order_management,
            "wallet": self.show_wallet,
            "fscheck": self.recheck_force_sub,
            "prem": self.show_premium,
            "prem_plan": self.show_premium_plan,
            "prem_req": self.request_premium_plan,
            "contact": self.show_contact_owner,
            "chplan": self.show_channel_plans,
            "chplan_req": self.request_channel_plan,
            "chplan_custom": self.prompt_custom_channels,
            "cm": self.show_comments,
            "ch": self.show_my_channels,
            "ch_page": self.show_my_channels,
            "addch": lambda e, a: self.show_add_to_chat(e, "channel"),
            "addgr": lambda e, a: self.show_add_to_chat(e, "group"),
            "cancel": self._cancel,
            "buy_views": self.buy_view_plan,
            "views_custom": self.prompt_custom_views,
            "buy_react": self.buy_reaction_plan,
            "free_react": self.free_reactions,
            "ar_ch": self.show_auto_reactions_channel,
            "ar_toggle": self.toggle_auto_reactions,
            "ar_accounts": self.prompt_auto_reactions_accounts,
            "ar_plan": self.buy_auto_reaction_plan,
            "av_ch": self.show_auto_views_channel,
            "av_toggle": self.toggle_auto_views,
            "av_perpost": self.prompt_auto_views_perpost,
            "av_plan": self.buy_auto_view_plan,
            "ac_ch": self.show_auto_comments_channel,
            "ac_toggle": self.toggle_auto_comments,
            "ac_count": self.prompt_auto_comments_count,
            "ac_use": self.use_comment_plan,
            "buy_members": self.buy_members,
            "ar_plan_go": self.buy_auto_reaction_plan,
            "ch_claim": self.claim_channel,
            "buy_prem": self.buy_premium,
            "buy_comment": self.buy_comment,
            "topup": self.prompt_topup_amount,
            "orders": self.show_orders,
            "order": self.show_order_detail,
            "order_cancel": self.cancel_order,
            "tx": self.show_transactions,
            "refresh": self.refresh,
            "reqplan": self.request_plan,
        }.get(action)
        if handler is None:
            logger.warning(f"Unknown user-panel action: {action}")
            await self.answer(event, "Unknown action", alert=False)
            await self.show_home(event)
            return
        await self._call(handler, event, args)

    @staticmethod
    async def _call(handler, event, args: List[str]) -> None:
        """Call a panel method, passing args only when it accepts them."""
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
        await self.show_home(event)

    async def refresh(self, event, args) -> None:
        await self.show_home(event)

    # ==================================================================
    # input handling
    # ==================================================================
    async def handle_input(self, action: str, text: str, event, data: Dict[str, Any]) -> None:
        if action == "views_link":
            await self._create_views_order(event, data, text)
        elif action == "views_custom_qty":
            await self._create_custom_views_order(event, data, text)
        elif action == "react_link":
            await self._create_reaction_order(event, data, text)
        elif action == "ar_accounts":
            await self._set_auto_reaction_accounts(event, data, text)
        elif action == "av_perpost":
            await self._set_auto_views_perpost(event, data, text)
        elif action == "ac_count":
            await self._set_auto_comments_count(event, data, text)
        elif action == "members_link":
            await self._create_members_order(event, data, text)
        elif action == "members_invite":
            await self._create_members_order(event, data, text, invite=True)
        elif action == "topup_amount":
            await self._ask_topup_amount(event, data, text)
        elif action == "topup_utr":
            await self._create_topup(event, data, text)
        elif action == "chplan_count":
            await self._set_custom_channels(event, data, text)
        elif action == "comment_link":
            await self._comment_link(event, data, text)
        elif action == "comment_text":
            await self._create_comment_order(event, data, text)
        else:
            logger.warning(f"Unhandled user input action: {action}")
            await self.answer(event, "⚠️ That input is no longer expected.", alert=True)
            await self.show_home(event)

    # ==================================================================
    # views
    # ==================================================================
    async def show_order_views(self, event) -> None:
        if await self.feature_locked(event, "views", "📈 <b>Order Views</b>"):
            return
        cfg = await self.settings.section("views")
        if not cfg.get("enabled", True):
            await self.render(event, Screen(
                "⛔️ <b>Order Views</b> is temporarily disabled.",
                [self.home_row()],
            ))
            return
        plans = cfg.get("plans", [])
        rows: List[List[Any]] = []
        text = (
            "📈 <b>Order Views</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Push real views onto any post/channel message.\n"
            "Send the post link after choosing a package.\n\n"
            "<b>Packages</b>\n"
        )
        for plan in plans:
            text += (
                f"\n• <b>{esc(str(plan.get('title')))}</b> — {fmt_money(plan.get('price'))}\n"
                f"  {esc(str(plan.get('description') or ''))}"
                f"{'  ' + esc(str(plan.get('badge'))) if plan.get('badge') else ''}"
            )
            style = SUCCESS if plan.get("badge") else None
            rows.append([btn(f"🛒 {plan.get('title')} • {fmt_money(plan.get('price'))}",
                             self.cb("buy_views", plan.get("id")), style)])
        free = await self.orders.free_quote("views")
        if free and cfg.get("free_quantity") and cfg.get("free_tier_enabled"):
            used = await self.users.check_daily_quota(
                int(event.sender_id), "free_views", int(cfg.get("free_daily_orders", 1))
            )
            label = (
                f"🎁 Free trial • {fmt_number(cfg.get('free_quantity'))} views "
                f"({used['remaining']}/{used['limit']} left today)"
            )
            rows.append([btn(label, self.cb("buy_views", "free_views"), SUCCESS if used["ok"] else None)])
        rows.append([btn("✏️ Custom quantity", self.cb("views_custom"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def buy_view_plan(self, event, args) -> None:
        if await self.feature_locked(event, "views", "📈 <b>Order Views</b>"):
            return
        plan_id = args[0] if args else ""
        user_id = int(event.sender_id)
        if plan_id == "free_views":
            cfg = await self.settings.section("views")
            quota = await self.users.check_daily_quota(
                user_id, "free_views", int(cfg.get("free_daily_orders", 1))
            )
            if not quota["ok"]:
                await self.answer(event, "Daily free limit reached. Buy a package 🙏", alert=True)
                await self.show_order_views(event)
                return
            plan = await self.orders.free_quote("views")
        else:
            plan = await self.orders.quote_plan("views", plan_id)
        if not plan:
            await self.answer(event, "Plan not found", alert=True)
            await self.show_order_views(event)
            return
        await self.set_input(
            event, "views_link", {"panel": "user", "plan": plan},
            prompt=(
                f"📈 <b>{esc(str(plan.get('title')))}</b>\n"
                f"Quantity: <b>{fmt_number(plan.get('quantity'))}</b> views\n"
                f"Price: <b>{fmt_money(plan.get('price'))}</b>"
                f"{' (free tier)' if not plan.get('price') else ''}\n\n"
                "Now send the <b>post link</b> (e.g. https://t.me/yourchannel/123)."
            ),
        )

    async def prompt_custom_views(self, event) -> None:
        if await self.feature_locked(event, "views", "📈 <b>Order Views</b>"):
            return
        await self.set_input(
            event, "views_custom_qty", {"panel": "user"},
            prompt=(
                "✏️ <b>Custom views</b>\n\nSend the number of views you want (e.g. 2500).\n"
                "Price is calculated automatically."
            ),
        )

    async def _ask_custom_views_price(self, quantity: int) -> Dict[str, Any]:
        """Price a custom view order using the cheapest per-1k rate available."""
        plans = await self.settings.plans("views")
        rate = None
        for plan in plans:
            qty = int(plan.get("quantity") or 0)
            price = float(plan.get("price") or 0)
            if qty > 0:
                per_unit = price / qty
                rate = per_unit if rate is None else min(rate, per_unit)
        if rate is None:
            rate = 0.05
        price = round(quantity * rate, 2)
        return {"plan_id": "views_custom", "title": f"Custom {fmt_number(quantity)} views",
                "quantity": quantity, "price": price}

    async def _create_custom_views_order(self, event, data: Dict[str, Any], text: str) -> None:
        try:
            quantity = int(str(text).replace(",", "").strip())
        except ValueError:
            raise ValueError("Please send a plain number, e.g. 2500")
        if quantity < 10:
            raise ValueError("Minimum custom order is 10 views")
        plan = await self._ask_custom_views_price(quantity)
        payload = {"plan": plan}
        await self.set_input(event, "views_link", {"panel": "user", "plan": plan},
                             prompt=f"💸 Price for {fmt_number(quantity)} views: <b>{fmt_money(plan['price'])}</b>\n\nSend the post link.")

    async def _create_views_order(self, event, data: Dict[str, Any], link: str) -> None:
        plan = data.get("plan") or {}
        user_id = int(event.sender_id)
        checked = await self._valid_link(
            event, data, link, "views_link",
            "📈 Send the <b>post link</b> (e.g. https://t.me/yourchannel/123).")
        if checked is None:
            return
        link = checked
        free = float(plan.get("price") or 0) <= 0
        try:
            order = await self.orders.create_order(
                user_id, "views", {"link": link.strip(), "views": int(plan.get("quantity") or 0)},
                plan=plan, charge=not free,
            )
        except ValueError as exc:
            await self._order_error(event, exc, self.show_order_views)
            return
        if free:
            await self.users.bump_counter(user_id, "free_views")
        self.executor.submit(order)
        await self.render(event, Screen(
            "✅ <b>Order placed</b>\n\n"
            + self.order_block(order, detailed=True)
            + "\n\n⚙️ Processing has started. You'll get a message when it finishes.",
            [[Button.inline("🧾 My Orders", self.cb("om"), PRIMARY)], self.home_row()],
        ))

    # ==================================================================
    # reactions
    # ==================================================================
    async def show_order_reactions(self, event) -> None:
        cfg = await self.settings.section("reactions")
        if not cfg.get("enabled", True):
            await self.render(event, Screen("⛔️ <b>Order Reactions</b> is disabled.", [self.home_row()]))
            return
        user_id = int(event.sender_id)
        free = await self.orders.free_quote("reaction")
        limits = await self.users.effective_limits(user_id)
        used = await self.users.check_daily_quota(
            user_id, "free_react", int(cfg.get("free_daily_orders", 3))
        )
        pool = int(free.get("available_accounts") or 0)
        text = (
            "❤️ <b>Order Reactions</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Accounts available in pool: <b>{pool}</b>\n"
            f"Your max accounts per order: <b>{limits.get('reaction_accounts')}</b>\n\n"
            f"🎁 <b>Free reactions</b>: up to <b>{cfg.get('free_account_limit')}</b> "
            f"accounts per order — {used['remaining']}/{used['limit']} orders left today.\n"
            f"Emoji: {'random positive' if cfg.get('random_emoji', True) else 'custom list'}\n"
        )
        if pool <= 0:
            text += (
                "\n⚠️ <b>No account is signed in yet</b>, so reactions cannot run. "
                "Sign in with <b>My Account Info ▸ Add Account</b> and try again.\n"
            )
        text += "\n<b>Paid packages</b>\n"
        rows: List[List[Any]] = [
            [btn(
                (f"🎁 Free • {free.get('accounts', 0)} accounts ({used['remaining']} left)"
                 if pool > 0 else "🎁 Free • pool is empty"),
                self.cb("free_react"),
                SUCCESS if used["ok"] else None,
            )]
        ]
        for plan in cfg.get("plans", []):
            text += (
                f"\n• <b>{esc(str(plan.get('title')))}</b> — {fmt_money(plan.get('price'))}\n"
                f"  {esc(str(plan.get('description') or ''))}\n"
            )
            rows.append([btn(
                f"🛒 {plan.get('title')} • {fmt_money(plan.get('price'))}",
                self.cb("buy_react", plan.get("id")),
                SUCCESS if plan.get("badge") else None,
            )])
        rows.append([btn("🔄 Auto Reactions", self.cb("ar"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def free_reactions(self, event) -> None:
        user_id = int(event.sender_id)
        cfg = await self.settings.section("reactions")
        quota = await self.users.check_daily_quota(user_id, "free_react", int(cfg.get("free_daily_orders", 3)))
        if not quota["ok"]:
            await self.answer(event, f"Free limit reached ({quota['limit']}/day). Try again tomorrow.", alert=True)
            await self.show_order_reactions(event)
            return
        cooldown = int(cfg.get("free_cooldown_seconds", 0) or 0)
        remaining = await self.users.cooldown_remaining(user_id, "free_react", cooldown)
        if remaining > 0:
            await self.answer(event, f"Please wait {remaining}s before the next free order.", alert=True)
            await self.show_order_reactions(event)
            return
        pool = await self.accounts.available_count()
        if pool <= 0:
            await self.answer(event, "No accounts available right now — try later.", alert=True)
            return
        quote = await self.orders.free_quote("reaction")
        accounts = min(int(quote.get("accounts") or 0), int(cfg.get("free_account_limit", 10)))
        plan = {"plan_id": "free_reaction", "title": f"Free • {accounts} reactions",
                "quantity": accounts, "price": 0.0, "accounts": accounts}
        await self.set_input(
            event, "react_link", {"panel": "user", "plan": plan, "free": True},
            prompt=(
                f"🎁 <b>Free reactions</b>\n\n"
                f"Accounts to use: <b>{accounts}</b> (of {pool} in pool)\n"
                f"Emoji: random positive\n\n"
                f"Send the <b>post link</b> now."
            ),
        )

    async def buy_reaction_plan(self, event, args) -> None:
        plan_id = args[0] if args else ""
        plan = await self.orders.quote_plan("reaction", plan_id)
        if not plan:
            await self.answer(event, "Plan not found", alert=True)
            await self.show_order_reactions(event)
            return
        limits = await self.users.effective_limits(int(event.sender_id))
        accounts = int(plan.get("accounts") or limits.get("reaction_accounts") or 5)
        accounts = max(1, min(accounts, int(limits.get("reaction_accounts") or accounts)))
        plan["accounts"] = accounts
        await self.set_input(
            event, "react_link", {"panel": "user", "plan": plan},
            prompt=(
                f"❤️ <b>{esc(str(plan.get('title')))}</b>\n"
                f"Reactions: <b>{fmt_number(plan.get('quantity'))}</b> from <b>{accounts}</b> accounts\n"
                f"Price: <b>{fmt_money(plan.get('price'))}</b>\n\n"
                "Send the <b>post link</b>."
            ),
        )

    async def _valid_link(self, event, data: Dict[str, Any], text: str, action: str,
                          prompt: str) -> Optional[str]:
        """Re-prompt instead of failing inside the order when the text is not a link.

        The production log showed a user typing "Test Message" where a post link
        was expected, which surfaced later as a confusing
        "Unrecognised Telegram link" warning from the worker.
        """
        from src.peers import parse_link

        value = (text or "").strip()
        info = parse_link(value)
        if info.kind == "unknown" or (info.message_id is None and info.kind != "invite"):
            await self.answer(event, "⚠️ That is not a Telegram link — send one like "
                                     "https://t.me/channel/123", alert=True)
            await self.set_input(event, action, data, prompt=prompt)
            return None
        return value

    async def _order_error(self, event, exc, back) -> None:
        """Report a failed order; money problems get a one-tap top-up path."""
        message = str(exc)
        await self.answer(event, f"⚠️ {message}", alert=True)
        if "top up" in message.lower() or "balance" in message.lower():
            await self.render(event, Screen(
                "💳 <b>Not enough balance</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 {esc(message)}\n\n"
                "Add funds and try again — your coins never expire.",
                [[btn("➕ Add Money", self.cb("topup"), SUCCESS)],
                 [btn("🧾 My Orders", self.cb("om"), PRIMARY)],
                 self.home_row()],
            ))
            return
        if back is not None:
            await back(event)

    async def _create_reaction_order(self, event, data: Dict[str, Any], link: str) -> None:
        plan = data.get("plan") or {}
        user_id = int(event.sender_id)
        checked = await self._valid_link(
            event, data, link, "react_link",
            "❤️ Send the <b>post link</b> (e.g. https://t.me/yourchannel/123).")
        if checked is None:
            return
        link = checked
        free = bool(data.get("free")) or float(plan.get("price") or 0) <= 0
        payload = {
            "link": link.strip(),
            "accounts": int(plan.get("accounts") or plan.get("quantity") or 0),
            "join": True,
        }
        try:
            order = await self.orders.create_order(
                user_id, "reaction", payload, plan=plan,
                quantity=int(plan.get("quantity") or 0), charge=not free,
            )
        except ValueError as exc:
            await self._order_error(event, exc, self.show_order_reactions)
            return
        if free:
            await self.users.bump_counter(user_id, "free_react")
            await self.users.mark_action_time(user_id, "free_react")
        self.executor.submit(order)
        await self.render(event, Screen(
            "✅ <b>Reaction order placed</b>\n\n" + self.order_block(order, detailed=True)
            + "\n\n⚙️ Reactions are being applied now — one account at a time with random delays.",
            [[Button.inline("🧾 My Orders", self.cb("om"), PRIMARY)], self.home_row()],
        ))

    # ==================================================================
    # auto views / auto reactions
    # ==================================================================
    async def _channels_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        """Channels this user may manage — see ``PanelBase.channels_of``.

        Strictly isolated per user: a normal user only ever sees the channels
        they added the bot to. Owner sees everything, admins additionally see
        channels whose adder Telegram did not report.
        """
        return await self.channels_of(user_id)

    async def _channel_or_none(self, event, channel_id) -> Optional[Dict[str, Any]]:
        """Channel the sender may manage, or None (with a toast + redirect)."""
        channel = await self.manage_target(int(event.sender_id), channel_id)
        if channel is None:
            await self.answer(event, "⛔️ That channel is not yours", alert=True)
        return channel

    async def request_plan(self, event, args) -> None:
        """Ask the owner to activate a premium service for this user."""
        feature = (args[0] if args else "all").lower()
        sent = await self.notify_admins_request(event, feature)
        labels = (await self.settings.section("premium")).get("feature_labels", {})
        text = (
            "📨 <b>Request sent</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Service: <b>{esc(str(labels.get(feature, feature)))}</b>\n\n"
            + ("The owner has been notified — you'll get a message here once it is "
               "activated." if sent else
               "I could not reach the owner automatically. Please message them directly.")
        )
        rows = []
        contact = str((await self.settings.section("support")).get("username") or "").lstrip("@")
        if contact:
            rows.append([Button.url("💬 Contact owner", f"https://t.me/{contact}")])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def _no_channel_screen(self, event, title: str) -> None:
        await self.render(event, Screen(
            f"{title}\n\n"
            "You haven't added me to a channel/group yet.\n\n"
            "Tap a button below: Telegram will ask which chat to use, then grant me "
            "<b>post / edit / delete messages</b> permissions. As soon as I'm added I start "
            "working automatically.",
            [
                [Button.inline("➕ Add To Channel", self.cb("addch"), SUCCESS),
                 Button.inline("➕ Add To Group", self.cb("addgr"), SUCCESS)],
                self.home_row(),
            ],
        ))

    async def show_auto_views(self, event) -> None:
        if await self.feature_locked(event, "views", "🔄 <b>Auto Views</b>"):
            return
        cfg = await self.settings.section("views")
        channels = await self._channels_for_user(int(event.sender_id))
        if not channels:
            await self._no_channel_screen(event, "🔄 <b>Auto Views</b>")
            return
        rows = []
        text = (
            "🔄 <b>Auto Views</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "Every new post in your channel gets views automatically.\n\n"
            "<b>Your channels</b>\n"
        )
        for channel in channels:
            bucket = channel.get("auto_views") or {}
            state = "🟢 ON" if bucket.get("enabled") else "🔴 OFF"
            per_post = bucket.get("views_per_post") or (cfg.get("auto") or {}).get("views_per_post", 100)
            left = int(bucket.get("posts_left", 0) or 0)
            plan_txt = "unlimited (free tier)" if left == 0 else f"{left} posts left"
            text += (
                f"\n• <b>{esc(str(channel.get('title')))}</b> — {state}\n"
                f"  per post: {fmt_number(per_post)} views • {plan_txt}"
            )
            rows.append([btn(f"⚙️ {channel.get('title')}", self.cb("av_ch", channel["_id"]), PRIMARY)])
        rows.append([btn("🛒 Auto view plans", self.cb("av_plan", 0))])
        rows.append([Button.inline("➕ Add To Channel", self.cb("addch"), SUCCESS)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def show_auto_views_channel(self, event, args) -> None:
        if await self.feature_locked(event, "views", "🔄 <b>Auto Views</b>"):
            return
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return await self.show_auto_views(event)
        channel_id = channel["_id"]
        bucket = channel.get("auto_views") or {}
        accounts = await self.accounts.available_count()
        entitled = await self.users.has_feature(int(event.sender_id), "views")
        text = (
            f"🔄 <b>Auto Views</b> — {esc(str(channel.get('title')))}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {'🟢 On' if bucket.get('enabled') else '🔴 Off'}\n"
            f"Views per post: <b>{fmt_number(bucket.get('views_per_post') or 0 or 'auto')}</b>\n"
            f"Plan posts left: <b>{'unlimited' if int(bucket.get('posts_left', 0) or 0) == 0 else bucket.get('posts_left')}</b>\n"
            f"Accounts available: <b>{accounts}</b>\n"
            f"Plan check: <b>{'✅ active — views run on every new post' if entitled else '🔒 no plan yet (nothing is sent)'}</b>\n"
        )
        rows = [
            [btn("🔴 Turn off" if bucket.get("enabled") else "🟢 Turn on",
                 self.cb("av_toggle", channel_id), DANGER if bucket.get("enabled") else SUCCESS)],
            [btn("🎯 Set views per post", self.cb("av_perpost", channel_id), PRIMARY)],
            [btn("🛒 Buy auto view plan", self.cb("av_plan", channel_id), SUCCESS)],
            [btn("⬅️ Back", self.cb("av"), PRIMARY)],
            self.home_row(),
        ]
        await self.render(event, Screen(text, rows))

    async def toggle_auto_views(self, event, args) -> None:
        if await self.feature_locked(event, "views", "🔄 <b>Auto Views</b>"):
            return
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return
        channel_id = channel["_id"]
        enabled = not bool((channel.get("auto_views") or {}).get("enabled"))
        await self.channels.set_auto_views(channel_id, enabled)
        await self.answer(event, "Auto views " + ("enabled ✅" if enabled else "disabled 🔴"))
        await self.show_auto_views_channel(event, [str(channel_id)])

    async def prompt_auto_views_perpost(self, event, args) -> None:
        if await self.feature_locked(event, "views", "🔄 <b>Auto Views</b>"):
            return
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return
        channel_id = channel["_id"]
        await self.set_input(
            event, "av_perpost", {"panel": "user", "channel_id": int(channel_id)},
            prompt="🎯 Send how many views each new post should get (e.g. 150).",
        )

    async def _set_auto_views_perpost(self, event, data: Dict[str, Any], text: str) -> None:
        channel = await self._channel_or_none(event, data.get("channel_id"))
        if channel is None:
            return
        channel_id = channel["_id"]
        try:
            value = int(str(text).replace(",", "").strip())
        except ValueError:
            raise ValueError("Send a plain number, e.g. 150")
        if value < 1 or value > 100000:
            raise ValueError("Choose a value between 1 and 100000")
        accounts = await self.accounts.available_count()
        await self.channels.set_auto_views(channel_id, True, views_per_post=value)
        note = ""
        if value > accounts:
            note = (
                f"\n\n⚠️ Only <b>{accounts}</b> accounts are in the pool, so per post you'll get "
                f"about <b>{accounts}</b> views (1 view per account). Buy more accounts or ask "
                f"the owner to add some."
            )
        await self.render(event, Screen(
            f"✅ Auto views set to <b>{fmt_number(value)}</b> per post.{note}",
            [[Button.inline("⬅️ Back to Auto Views", self.cb("av_ch", channel_id), PRIMARY)], self.home_row()],
        ))

    async def show_auto_reactions(self, event) -> None:
        cfg = await self.settings.section("reactions")
        channels = await self._channels_for_user(int(event.sender_id))
        if not channels:
            await self._no_channel_screen(event, "🔄 <b>Auto Reactions</b>")
            return
        auto_cfg = cfg.get("auto", {}) or {}
        rows = []
        text = (
            "🔄 <b>Auto Reactions</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Every new post gets <b>random positive reactions</b> automatically.\n"
            f"🎁 Free: <b>{auto_cfg.get('min_accounts', 8)}–"
            f"{cfg.get('free_account_limit')}</b> random accounts on every new post "
            f"(auto reactions are free for everyone).\n"
            f"🕒 Random delay between accounts to look natural.\n\n"
            "<b>Your channels</b>\n"
        )
        for channel in channels:
            bucket = channel.get("auto_reactions") or {}
            state = "🟢 ON" if bucket.get("enabled") else "🔴 OFF"
            left = int(bucket.get("posts_left", 0) or 0)
            plan_txt = "unlimited (free)" if left == 0 else f"{left} paid posts left"
            text += (
                f"\n• <b>{esc(str(channel.get('title')))}</b> — {state}\n"
                f"  accounts/post: {bucket.get('accounts') or 'auto'} • {plan_txt}"
            )
            rows.append([btn(f"⚙️ {channel.get('title')}", self.cb("ar_ch", channel["_id"]), PRIMARY)])
        rows.append([btn("🛒 Auto reaction plans", self.cb("ar_plan", 0), SUCCESS)])
        rows.append([Button.inline("➕ Add To Channel", self.cb("addch"), SUCCESS)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def show_auto_reactions_channel(self, event, args) -> None:
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return await self.show_auto_reactions(event)
        channel_id = channel["_id"]
        cfg = await self.settings.section("reactions")
        bucket = channel.get("auto_reactions") or {}
        limits = await self.users.effective_limits(int(event.sender_id))
        pool = await self.accounts.available_count()
        emojis = bucket.get("emojis") or cfg.get("emojis") or []
        text = (
            f"🔄 <b>Auto Reactions</b> — {esc(str(channel.get('title')))}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {'🟢 On' if bucket.get('enabled') else '🔴 Off'}\n"
            f"Accounts per post: <b>{bucket.get('accounts') or 'auto (' + str(cfg.get('free_account_limit')) + ')'}</b>\n"
            f"Your account cap: <b>{limits.get('reaction_accounts')}</b> • pool: <b>{pool}</b>\n"
            f"Emoji: {' '.join(emojis[:14])}\n"
            f"Paid posts left: <b>{'unlimited (free tier)' if int(bucket.get('posts_left', 0) or 0) == 0 else bucket.get('posts_left')}</b>\n"
        )
        rows = [
            [btn("🔴 Turn off" if bucket.get("enabled") else "🟢 Turn on",
                 self.cb("ar_toggle", channel_id), DANGER if bucket.get("enabled") else SUCCESS)],
            [btn("👥 Set accounts per post", self.cb("ar_accounts", channel_id), PRIMARY)],
            [btn("🛒 Buy auto reaction plan", self.cb("ar_plan", channel_id), SUCCESS)],
            [btn("⬅️ Back", self.cb("ar"), PRIMARY)],
            self.home_row(),
        ]
        await self.render(event, Screen(text, rows))

    async def toggle_auto_reactions(self, event, args) -> None:
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return
        channel_id = channel["_id"]
        enabled = not bool((channel.get("auto_reactions") or {}).get("enabled"))
        if enabled:
            verdict = await self.channels.can_enable_auto(
                channel_id, int(event.sender_id), self.users)
            if not verdict.get("allowed"):
                return await self._show_channel_gate(event, channel, verdict)
        await self.channels.set_auto_reactions(channel_id, enabled)
        await self.answer(event, "Auto reactions " + ("enabled ✅" if enabled else "disabled 🔴"))
        await self.show_auto_reactions_channel(event, [str(channel_id)])

    async def _show_channel_gate(self, event, channel: Dict[str, Any], verdict: Dict[str, Any]) -> None:
        """Free allowance is used up — offer the channel plan instead."""
        slots = verdict.get("slots") or await self.channel_slots()
        text = (
            "📡 <b>Free channel limit reached</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Channel: <b>{esc(str(channel.get('title')))}</b>\n"
            f"🎁 Free channels: <b>{slots.get('free')}</b>\n"
            f"📡 Your limit: <b>{slots.get('total')}</b> • in use: <b>{slots.get('used')}</b>\n\n"
            "Auto reactions are <b>free</b> on your first channels. For this one you need "
            "an extra-channel plan — pick one and the owner activates it for you.\n\n"
            "<i>Channels you added stay registered and can be disabled at any time, "
            "but not removed — that keeps the free allowance fair for everybody.</i>"
        )
        rows = [
            [btn("📡 Get channel plan", self.cb("chplan"), SUCCESS)],
            [btn("💬 Contact owner", self.cb("contact"), PRIMARY)],
            [btn("⬅️ Back", self.cb("ar"), PRIMARY)],
            self.home_row(),
        ]
        await self.render(event, Screen(text, rows))

    async def prompt_auto_reactions_accounts(self, event, args) -> None:
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return
        channel_id = channel["_id"]
        limits = await self.users.effective_limits(int(event.sender_id))
        pool = await self.accounts.available_count()
        await self.set_input(
            event, "ar_accounts", {"panel": "user", "channel_id": channel_id},
            prompt=(
                f"👥 How many accounts should react to each new post?\n\n"
                f"Your cap: <b>{limits.get('reaction_accounts')}</b> • pool size: <b>{pool}</b>\n"
                f"Send a number (e.g. 5)."
            ),
        )

    async def _set_auto_reaction_accounts(self, event, data: Dict[str, Any], text: str) -> None:
        channel = await self._channel_or_none(event, data.get("channel_id"))
        if channel is None:
            return
        channel_id = channel["_id"]
        try:
            value = int(str(text).strip())
        except ValueError:
            raise ValueError("Send a plain number, e.g. 5")
        limits = await self.users.effective_limits(int(event.sender_id))
        cap = int(limits.get("reaction_accounts") or 10)
        pool = await self.accounts.available_count()
        if value < 1:
            raise ValueError("At least 1 account is required")
        if value > cap:
            raise ValueError(
                f"Your plan allows up to {cap} accounts per post "
                f"(free tier: {limits.get('free_reaction_accounts')}). "
                "Ask the owner to raise your limit."
            )
        value = min(value, max(1, pool or value))
        await self.channels.set_auto_reactions(channel_id, True, accounts=value)
        await self.render(event, Screen(
            f"✅ Auto reactions will use <b>{value}</b> accounts per post.",
            [[Button.inline("⬅️ Back", self.cb("ar_ch", channel_id), PRIMARY)], self.home_row()],
        ))

    async def buy_auto_reaction_plan(self, event, args) -> None:
        channel_id = args[0] if args else "0"
        limits = await self.users.effective_limits(int(event.sender_id))
        if not limits.get("premium"):
            # Free tier already covers up to the free account limit; a paid plan
            # only makes sense once the owner activates one for this user.
            await self.render(event, Screen(
                "👑 <b>Auto reaction plans</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"Your free tier already reacts with up to "
                f"<b>{limits.get('free_reaction_accounts')}</b> accounts on every "
                "new post — at no cost.\n\n"
                "Need more accounts per post, or a custom emoji set? Ask the owner "
                "to activate a plan for your account.",
                [[Button.inline("📨 Request activation", self.cb("reqplan", "reactions"), SUCCESS)],
                 [btn("⬅️ Back to Auto Reactions", self.cb("ar"), PRIMARY)],
                 self.home_row()],
            ))
            return
        cfg = await self.settings.section("reactions")
        plans = [p for p in cfg.get("plans", []) if int(p.get("auto_posts") or 0) > 0]
        rows = []
        text = (
            "🛒 <b>Auto Reaction Plans</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            "Paid plans raise the number of accounts per post and give a fixed number of "
            "automated posts.\n"
        )
        for plan in plans:
            text += (
                f"\n• <b>{esc(str(plan.get('title')))}</b> — {fmt_money(plan.get('price'))}\n"
                f"  {esc(str(plan.get('description') or ''))}\n"
                f"  → {plan.get('accounts')} accounts/post for {plan.get('auto_posts')} posts"
            )
            rows.append([btn(f"🛒 {plan.get('title')} • {fmt_money(plan.get('price'))}",
                             self.cb("ar_plan", channel_id, plan.get("id")), SUCCESS)])
        rows.append([btn("⬅️ Back", self.cb("ar" if not channel_id else "ar_ch", channel_id or ""), PRIMARY)])
        rows.append(self.home_row())
        if len(args) > 1:
            await self._confirm_auto_plan(event, channel_id, args[1], plans)
            return
        await self.render(event, Screen(text, rows))

    async def _confirm_auto_plan(self, event, channel_id: str, plan_id: str, plans) -> None:
        plan = next((p for p in plans if str(p.get("id")) == str(plan_id)), None)
        if plan is None:
            await self.answer(event, "Plan not found", alert=True)
            return
        if not channel_id or int(channel_id) == 0:
            channels = await self._channels_for_user(int(event.sender_id))
            if not channels:
                await self._no_channel_screen(event, "➕ Add me to your channel first")
                return
            rows = [[btn(f"⚙️ {c.get('title')}", self.cb("ar_plan_go", c["_id"], plan_id), PRIMARY)]
                    for c in channels]
            rows.append([btn("⬅️ Back", self.cb("ar_plan", 0), PRIMARY)])
            await self.render(event, Screen("👆 Choose the channel for this plan:", rows))
            return
        await self._place_auto_order(event, int(channel_id), plan)

    async def _place_auto_order(self, event, channel_id: int, plan: Dict[str, Any]) -> None:
        user_id = int(event.sender_id)
        payload = {
            "channel_id": channel_id,
            "posts": int(plan.get("auto_posts") or 0),
            "accounts": int(plan.get("accounts") or 0),
        }
        try:
            order = await self.orders.create_order(
                user_id, "auto_reactions", payload, plan={
                    "plan_id": plan.get("id"), "title": plan.get("title"),
                    "quantity": int(plan.get("auto_posts") or 0), "price": plan.get("price"),
                },
            )
        except ValueError as exc:
            await self.answer(event, f"⚠️ {exc}", alert=True)
            return
        self.executor.submit(order)
        await self.render(event, Screen(
            "✅ <b>Auto reactions activated</b>\n\n" + self.order_block(order, detailed=True)
            + "\n\nEvery new post will now get reactions from more accounts.",
            [[Button.inline("🔄 Auto Reactions", self.cb("ar"), PRIMARY)], self.home_row()],
        ))

    async def buy_auto_view_plan(self, event, args) -> None:
        if await self.feature_locked(event, "views", "🔄 <b>Auto Views</b>"):
            return
        channel_id = args[0] if args else "0"
        cfg = await self.settings.section("views")
        plans = cfg.get("plans", [])
        rows = []
        text = "🛒 <b>Auto View Plans</b>\n━━━━━━━━━━━━━━━━━━━━\nPick a package to boost views per post.\n"
        for plan in plans:
            text += f"\n• <b>{esc(str(plan.get('title')))}</b> — {fmt_money(plan.get('price'))}"
            rows.append([btn(f"🛒 {plan.get('title')} • {fmt_money(plan.get('price'))}",
                             self.cb("av_plan", channel_id, plan.get("id")), SUCCESS)])
        if len(args) > 1:
            plan = next((p for p in plans if str(p.get("id")) == str(args[1])), None)
            if plan and channel_id and int(channel_id) != 0:
                payload = {"channel_id": int(channel_id),
                           "views_per_post": int(plan.get("quantity") or 0)}
                try:
                    order = await self.orders.create_order(
                        int(event.sender_id), "auto_views", payload,
                        plan={"plan_id": plan.get("id"), "title": plan.get("title"),
                              "quantity": int(plan.get("quantity") or 0), "price": plan.get("price")},
                    )
                except ValueError as exc:
                    await self.answer(event, f"⚠️ {exc}", alert=True)
                    return
                self.executor.submit(order)
                await self.render(event, Screen(
                    "✅ <b>Auto views activated</b>\n\n" + self.order_block(order, detailed=True),
                    [[Button.inline("🔄 Auto Views", self.cb("av"), PRIMARY)], self.home_row()],
                ))
                return
        rows.append([btn("⬅️ Back", self.cb("av"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    # ==================================================================
    # members
    # ==================================================================
    async def show_members(self, event) -> None:
        if await self.feature_locked(event, "members", "👥 <b>Members</b>"):
            return
        cfg = await self.settings.section("members")
        if not cfg.get("enabled", True):
            await self.render(event, Screen("⛔️ <b>Members</b> service is disabled.", [self.home_row()]))
            return
        channels = await self._channels_for_user(int(event.sender_id))
        pool = await self.accounts.available_count()
        text = (
            "👥 <b>Members</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Accounts available to join: <b>{pool}</b>\n\n"
            "Pick a package — then send your channel's <b>invite link</b> "
            "(or just tap a channel you already added me to).\n"
        )
        rows = []
        for plan in cfg.get("plans", []):
            text += f"\n• <b>{esc(str(plan.get('title')))}</b> — {fmt_money(plan.get('price'))}\n  {esc(str(plan.get('description') or ''))}"
            rows.append([btn(f"🛒 {plan.get('title')} • {fmt_money(plan.get('price'))}",
                             self.cb("buy_members", plan.get("id")), SUCCESS)])
        for channel in channels[:5]:
            rows.append([btn(f"📢 {channel.get('title')}",
                             self.cb("buy_members", cfg.get("plans", [{}])[0].get("id", ""), channel["_id"]) if cfg.get("plans") else self.cb("mb"))])
        rows.append([Button.inline("➕ Add To Channel", self.cb("addch"), SUCCESS)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def buy_members(self, event, args) -> None:
        if await self.feature_locked(event, "members", "👥 <b>Members</b>"):
            return
        plan_id = args[0] if args else ""
        plan = await self.orders.quote_plan("members", plan_id)
        if not plan:
            await self.answer(event, "Plan not found", alert=True)
            return await self.show_members(event)
        data = {"plan": plan}
        if len(args) > 1 and args[1]:
            data["channel_id"] = int(args[1])
        await self.set_input(
            event, "members_invite", {"panel": "user", **data},
            prompt=(
                f"👥 <b>{esc(str(plan.get('title')))}</b> — {fmt_money(plan.get('price'))}\n\n"
                "Send your channel <b>invite link</b> (t.me/+xxxx or @username).\n"
                "Accounts will join through it."
            ),
        )

    async def _create_members_order(self, event, data: Dict[str, Any], text: str,
                                    invite: bool = False) -> None:
        plan = data.get("plan") or {}
        link = (text or "").strip()
        if not link:
            raise ValueError("Send a valid invite link or @username")
        payload = {"channel": link, "members": int(plan.get("quantity") or 0), "invite_link": link}
        if data.get("channel_id"):
            payload["channel_id"] = int(data["channel_id"])
        try:
            order = await self.orders.create_order(
                int(event.sender_id), "members", payload, plan=plan,
                quantity=int(plan.get("quantity") or 0),
            )
        except ValueError as exc:
            await self.answer(event, f"⚠️ {exc}", alert=True)
            await self.show_members(event)
            return
        self.executor.submit(order)
        await self.render(event, Screen(
            "✅ <b>Members order placed</b>\n\n" + self.order_block(order, detailed=True)
            + "\n\n⚙️ Accounts are joining with random delays.",
            [[Button.inline("🧾 My Orders", self.cb("om"), PRIMARY)], self.home_row()],
        ))

    # ==================================================================
    # account info
    # ==================================================================
    async def show_account_info(self, event) -> None:
        user = await self.ensure_user(event)
        limits = await self.users.effective_limits(user["_id"])
        pool = await self.accounts.stats()
        stats = user.get("stats") or {}
        orders = await self.orders.list_for_user(user["_id"], limit=100)
        done = len([o for o in orders if o.get("status") == "completed"])
        plan = limits.get("plan") or {}
        expires = plan.get("expires_at")
        plan_line = "Free plan"
        if limits.get("premium"):
            plan_line = "♾ Unlimited" if plan.get("unlimited") else (
                "👑 " + ", ".join(str(k) for k in (plan.get("kinds") or ["premium"])))
            if expires:
                plan_line += f" (till {fmt_date(expires)})"
        text = (
            "👤 <b>Account Info</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <code>{user['_id']}</code>\n"
            f"👤 {esc(str(user.get('first_name') or '—'))} @{esc(str(user.get('username') or '—'))}\n"
            f"🎫 Plan: <b>{plan_line}</b>\n"
            f"💳 Wallet: <b>{fmt_money(user.get('balance', 0))}</b>\n"
            f"💸 Total spent: <b>{fmt_money(user.get('total_spent', 0))}</b>\n\n"
            "<b>Your limits</b>\n"
            f"• Accounts per reaction order: <b>{limits.get('reaction_accounts')}</b>\n"
            f"• Accounts per comment order: <b>{limits.get('comment_accounts') or 'premium only'}</b>\n"
            f"• Account cap: <b>{limits.get('accounts')}</b>\n\n"
            "<b>Account pool (owner's signed-in accounts)</b>\n"
            f"• Total: <b>{pool.get('total')}</b> • Live: <b>{pool.get('live', 0)}</b> "
            f"• Revoked: <b>{pool.get('revoked', 0)}</b>\n\n"
            "<b>Your activity</b>\n"
            f"• Orders: <b>{len(orders)}</b> (completed: {done})\n"
            f"• Reactions sent: <b>{fmt_number(stats.get('reactions', 0))}</b>\n"
            f"• Views sent: <b>{fmt_number(stats.get('views', 0))}</b>\n"
            f"• Members added: <b>{fmt_number(stats.get('members', 0))}</b>\n"
            f"• Comments: <b>{fmt_number(stats.get('comments', 0))}</b>\n"
        )
        rows = [
            [btn("🧾 My Orders", self.cb("om"), PRIMARY), btn("💳 Wallet", self.cb("wallet"), SUCCESS)],
            self.home_row(),
        ]
        await self.render(event, Screen(text, rows))

    # ==================================================================
    # wallet & top-up
    # ==================================================================
    async def show_wallet(self, event) -> None:
        user = await self.ensure_user(event)
        cfg = await self.settings.section("payments")
        txns = await self.users.transactions(user["_id"], limit=5)
        text = (
            "💳 <b>Wallet</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Balance: <b>{fmt_money(user.get('balance', 0))}</b>\n"
            f"Spent so far: <b>{fmt_money(user.get('total_spent', 0))}</b>\n"
        )
        if txns:
            text += "\n<b>Recent transactions</b>\n"
            for txn in txns:
                sign = "+" if txn.get("type") == "credit" else "−"
                text += (
                    f"• {sign}{fmt_money(txn.get('amount', 0))} — {esc(str(txn.get('reason') or txn.get('kind')))}"
                    f" ({str(txn.get('created_at', ''))[:10]})\n"
                )
        rows = [
            [btn("➕ Add Money", self.cb("topup"), SUCCESS)],
            [btn("👑 Premium plans", self.cb("prem"), PRIMARY)],
            [btn("🧾 Orders", self.cb("om"), PRIMARY)],
            self.home_row(),
        ]
        await self.render(event, Screen(text, rows))

    async def prompt_topup_amount(self, event) -> None:
        cfg = await self.settings.section("payments")
        upi = cfg.get("upi_id") or "—"
        name = cfg.get("upi_name") or ""
        await self.set_input(
            event, "topup_amount", {"panel": "user", "temp": True},
            prompt=(
                "➕ <b>Add Money</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                f"Pay to UPI: <code>{esc(str(upi))}</code>"
                + (f"\nName: {esc(str(name))}" if name else "") +
                f"\n\n{esc(str(cfg.get('instructions') or ''))}\n\n"
                f"Send the <b>amount</b> you want to add (min {fmt_money(cfg.get('min_topup', 0))})."
            ),
        )

    async def _ask_topup_amount(self, event, data: Dict[str, Any], text: str) -> None:
        cfg = await self.settings.section("payments")
        try:
            amount = float(str(text).replace(",", "").strip())
        except ValueError:
            raise ValueError("Send a number, e.g. 100")
        if amount < float(cfg.get("min_topup", 0) or 0):
            raise ValueError(f"Minimum top-up is {fmt_money(cfg.get('min_topup', 0))}")
        await self.set_input(
            event, "topup_utr", {"panel": "user", "amount": amount},
            prompt=(
                f"Amount: <b>{fmt_money(amount)}</b>\n\n"
                f"1️⃣ Pay {fmt_money(amount)} to <code>{esc(str(cfg.get('upi_id') or '—'))}</code>\n"
                "2️⃣ Send the <b>UTR / reference number</b> from your UPI app here.\n\n"
                "Your wallet is credited after the owner verifies the payment."
            ),
        )

    async def _create_topup(self, event, data: Dict[str, Any], text: str) -> None:
        amount = float(data.get("amount") or 0)
        try:
            request = await self.payments.create_topup(int(event.sender_id), amount, text)
        except ValueError as exc:
            await self.answer(event, f"⚠️ {exc}", alert=True)
            return
        await self.notifier.to_admins(
            "💳 <b>New top-up request</b>\n\n"
            f"👤 User: <code>{request['user_id']}</code>\n"
            f"💰 Amount: <b>{fmt_money(request['amount'])}</b>\n"
            f"🔖 UTR: <code>{esc(str(request.get('utr')))}</code>\n"
            f"🆔 <code>{request['_id']}</code>",
            buttons=[[
                Button.inline("✅ Approve", f"a:topup_ok:{request['_id']}", style=SUCCESS),
                Button.inline("❌ Reject", f"a:topup_no:{request['_id']}", style=DANGER),
            ]],
        )
        await self.render(event, Screen(
            "🕓 <b>Top-up submitted</b>\n\n"
            f"Amount: <b>{fmt_money(amount)}</b>\n"
            f"UTR: <code>{esc(str(text))}</code>\n\n"
            "The owner will verify it shortly. You'll be notified here.",
            [[Button.inline("💳 Wallet", self.cb("wallet"), PRIMARY)], self.home_row()],
        ))

    async def show_transactions(self, event) -> None:
        await self.show_wallet(event)

    # ==================================================================
    # premium
    # ==================================================================
    async def show_premium(self, event) -> None:
        """👑 Buy Premium — every plan with details + how to get it.

        This is the screen behind the old "Add Money" button: the user sees all
        plans, can read exactly what each one gives (days, accounts, channels),
        and either asks the owner to activate it (the owner takes the payment and
        grants it) or pays it straight from the wallet.
        """
        cfg = await self.settings.section("premium")
        user = await self.ensure_user(event)
        user_id = user["_id"]
        premium = await self.users.is_premium(user_id)
        plan = await self.users.plan_of(user_id)
        limits = await self.users.effective_limits(user_id)
        slots = await self.channel_slots(int(getattr(event, "sender_id", 0) or 0))
        admin = await self.users.is_admin(user_id)

        text = (
            "👑 <b>BUY PREMIUM</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>{esc(str(user.get('name') or user_id))}</b> • "
            f"{'♾ unlimited' if plan.get('unlimited') else ('👑 premium' if premium else '🎁 free')}\n"
            f"💳 Wallet: <b>{fmt_money(user.get('balance', 0))}</b>\n"
            f"📡 Channels on auto reactions: <b>{slots['used']}/{slots['total']}</b>"
            + (" (unlimited)" if slots["unlimited"] else f" • free {slots['free']}") + "\n"
        )
        if admin:
            text += "🛠 <b>God mode</b> — you are admin/owner: everything is already unlocked.\n"
        if premium and plan.get("expires_at"):
            text += f"🗓 Your plan is valid till <b>{fmt_date(plan['expires_at'])}</b>\n"

        text += "\n<b>What premium gives you</b>\n"
        for benefit in cfg.get("benefits", []):
            text += f"• {esc(str(benefit))}\n"

        text += "\n<b>Available plans</b>\n"
        rows: List[List[Any]] = []
        for plan_item in cfg.get("plans", []):
            pid = plan_item.get("id")
            days = plan_item.get("days")
            price = plan_item.get("price")
            bits = []
            if days:
                bits.append(f"{days}d")
            if plan_item.get("accounts"):
                bits.append(f"{plan_item['accounts']} accounts")
            if plan_item.get("channels"):
                bits.append(f"+{plan_item['channels']} channels")
            text += (
                f"\n▸ <b>{esc(str(plan_item.get('title')))}</b> — {fmt_money(price)}\n"
                f"   {' • '.join(bits) if bits else 'every service'}\n"
                f"   <i>{esc(str(plan_item.get('description') or ''))}</i>\n"
            )
            rows.append([btn(f"👑 {plan_item.get('title')} • {fmt_money(price)}",
                             self.cb("prem_plan", pid), SUCCESS)])
        if not cfg.get("plans"):
            text += "\n<i>No plans published yet — ask the owner.</i>\n"

        text += (
            "\n<b>How to activate</b>\n"
            "1️⃣ Tap a plan to see exactly what it includes\n"
            "2️⃣ Press <b>📨 Request activation</b> — the owner receives your request\n"
            "3️⃣ Pay the owner and the plan goes live on your account 👑\n"
        )
        rows.append([btn("📡 Only extra channels?", self.cb("chplan"), PRIMARY)])
        rows.append([btn("💬 Contact owner", self.cb("contact"), PRIMARY),
                     btn("💳 Wallet", self.cb("wallet"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    def _plan_bits(self, plan_item: Dict[str, Any]) -> str:
        bits = []
        if plan_item.get("days"):
            bits.append(f"🗓 {plan_item['days']} days")
        if plan_item.get("accounts"):
            bits.append(f"👥 {plan_item['accounts']} accounts")
        if plan_item.get("channels"):
            bits.append(f"📡 +{plan_item['channels']} channels")
        kinds = plan_item.get("kinds") or []
        if kinds:
            labels = {"reactions": "auto reactions", "views": "views", "comments": "comments",
                      "members": "members", "channels": "channels", "all": "everything"}
            bits.append("✅ " + ", ".join(labels.get(str(k), str(k)) for k in kinds))
        return " • ".join(bits)

    async def show_premium_plan(self, event, args) -> None:
        """One plan in full — request activation, or pay from the wallet."""
        pid = args[0] if args else ""
        cfg = await self.settings.section("premium")
        plan_item = next((p for p in cfg.get("plans", []) if str(p.get("id")) == str(pid)), None)
        if plan_item is None:
            await self.answer(event, "That plan is no longer available", alert=True)
            return await self.show_premium(event)
        user = await self.ensure_user(event)
        price = float(plan_item.get("price") or 0)
        balance = float(user.get("balance", 0) or 0)
        text = (
            f"👑 <b>{esc(str(plan_item.get('title')))}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{self._plan_bits(plan_item)}\n"
            f"💵 Price: <b>{fmt_money(price)}</b>\n"
            f"💳 Your wallet: <b>{fmt_money(balance)}</b>\n"
        )
        if plan_item.get("description"):
            text += f"\n<i>{esc(str(plan_item['description']))}</i>\n"
        text += (
            "\n<b>How to get it</b>\n"
            "1️⃣ Tap <b>Request activation</b> — the owner gets your request with all "
            "the details and takes the payment from you directly (UPI / whatever you "
            "both use).\n"
            "2️⃣ Or tap <b>Pay from wallet</b> if you already have the balance.\n"
        )
        rows = [
            [btn("📨 Request activation", self.cb("prem_req", pid), SUCCESS)],
        ]
        if price <= 0 or balance >= price:
            rows.append([btn(f"⚡ Pay {fmt_money(price)} from wallet",
                             self.cb("buy_prem", pid), PRIMARY)])
        rows.append([btn("💬 Contact owner", self.cb("contact"), PRIMARY)])
        rows.append([btn("⬅️ Back", self.cb("prem"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def request_premium_plan(self, event, args) -> None:
        """Send the owner a request for this exact plan (payment happens offline)."""
        pid = args[0] if args else ""
        cfg = await self.settings.section("premium")
        plan_item = next((p for p in cfg.get("plans", []) if str(p.get("id")) == str(pid)), None)
        if plan_item is None:
            await self.answer(event, "That plan is no longer available", alert=True)
            return await self.show_premium(event)
        user = await self.ensure_user(event)
        requests = get_plan_request_service(users=self.users)
        await requests.request(
            user["_id"],
            kind="premium",
            plan_id=str(plan_item.get("id")),
            title=str(plan_item.get("title")),
            price=float(plan_item.get("price") or 0),
            days=int(plan_item.get("days") or 0),
            accounts=int(plan_item.get("accounts") or 0),
            channels=int(plan_item.get("channels") or 0),
            username=str(user.get("username") or ""),
            note="requested from Buy Premium",
        )
        support = await self.settings.section("support")
        contact = str(support.get("username") or "").lstrip("@")
        text = (
            "📨 <b>Request sent to the owner</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 Plan: <b>{esc(str(plan_item.get('title')))}</b>\n"
            f"💵 Price: <b>{fmt_money(plan_item.get('price'))}</b>\n"
            f"{self._plan_bits(plan_item)}\n\n"
            "The owner has your request with all the details. They will take the "
            "payment and activate the plan — you'll get a confirmation message here "
            "the moment it goes live.\n"
        )
        rows = []
        if contact:
            rows.append([Button.url("💬 Contact owner now", f"https://t.me/{contact}")])
        rows.append([btn("⬅️ Back to plans", self.cb("prem"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def buy_premium(self, event, args) -> None:
        """⚡ Instant purchase from the wallet (the owner side stays the same)."""
        plan_id = args[0] if args else ""
        try:
            outcome = await self.payments.buy_premium(int(event.sender_id), plan_id)
        except ValueError as exc:
            await self.answer(event, str(exc), alert=True)
            await self.show_premium(event)
            return
        plan = outcome.get("plan") or {}
        await self.render(event, Screen(
            "🎉 <b>Premium activated</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 Plan: <b>{esc(str(plan.get('title')))}</b>\n"
            f"🗓 Days: <b>{plan.get('days')}</b>\n"
            f"📡 Channels: <b>{'+' + str(plan['channels']) if plan.get('channels') else 'unchanged'}</b>\n\n"
            "Comments, views, members and your extra channels are unlocked.",
            [[btn("🏠 Main Menu", self.cb("home"), PRIMARY)]],
        ))

    async def show_contact_owner(self, event) -> None:
        support = await self.settings.section("support")
        username = str(support.get("username") or "").lstrip("@")
        rows = []
        if username:
            rows.append([Button.url("💬 Open chat with the owner", f"https://t.me/{username}")])
        rows.append([btn("👑 Buy Premium", self.cb("prem"), SUCCESS)])
        rows.append(self.home_row())
        if username:
            reach = (f"💬 Telegram: {tg_link(username)}\n"
                     f"🔗 {html_link(f'https://t.me/{username}')}\n")
        else:
            reach = ("💬 The owner has not set a support username yet — you can still "
                     "send a plan request from the Buy Premium screen.\n")
        text = (
            "💬 <b>CONTACT THE OWNER</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            + reach
            + "\n<b>How it works</b>\n"
              "1️⃣ Open <b>👑 Buy Premium</b> and pick a plan\n"
              "2️⃣ Tap <b>📨 Request activation</b> — the owner receives your request "
              "with the plan, price and duration\n"
              "3️⃣ Pay the owner the way they ask (UPI, crypto — whatever the plan note "
              "says)\n"
              "4️⃣ The owner activates it and you get a confirmation with the expiry "
              "date 👑"
        )
        await self.render(event, Screen(text, rows))

    # ------------------------------------------------------------------
    # channel slots (free allowance + extra-channel plans)
    # ------------------------------------------------------------------
    async def channel_slots(self, user_id: Optional[int] = None) -> Dict[str, Any]:
        """Free channels + plan slots for this user (never raises)."""
        try:
            return await self.channels.channel_slots_of(int(user_id or 0), self.users)
        except Exception as exc:  # noqa: BLE001 - the panel must still render
            logger.debug(f"channel slot lookup failed: {exc}")
            return {"free": 2, "total": 2, "used": 0, "left": 2, "unlimited": False}

    async def show_channel_plans(self, event) -> None:
        """📡 Extra channels beyond the free allowance — request or pay."""
        cfg = await self.settings.section("channels")
        user = await self.ensure_user(event)
        slots = await self.channel_slots(user["_id"])
        text = (
            "📡 <b>EXTRA AUTO-REACTION CHANNELS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 Free channels: <b>{slots['free']}</b>\n"
            f"📡 Your limit: <b>{'unlimited' if slots['unlimited'] else slots['total']}</b>"
            f" • in use: <b>{slots['used']}</b>\n\n"
            "Auto reactions are free on your first "
            f"<b>{slots['free']}</b> channel(s). Every extra channel needs a channel "
            "plan — pick one below and the owner will set it up for you.\n"
        )
        rows: List[List[Any]] = []
        for plan_item in cfg.get("plans", []):
            text += (
                f"\n▸ <b>{esc(str(plan_item.get('title')))}</b> — {fmt_money(plan_item.get('price'))}\n"
                f"   +{plan_item.get('slots')} channel(s)"
                + (f" • {plan_item.get('duration_days')} days" if plan_item.get("duration_days") else "")
                + f"\n   <i>{esc(str(plan_item.get('description') or ''))}</i>\n"
            )
            rows.append([btn(f"📡 {plan_item.get('title')} • {fmt_money(plan_item.get('price'))}",
                             self.cb("chplan_req", plan_item.get("id")), SUCCESS)])
        rows.append([btn("✍️ Custom number of channels", self.cb("chplan_custom"), PRIMARY)])
        rows.append([btn("💬 Contact owner", self.cb("contact"), PRIMARY),
                     btn("⬅️ Back", self.cb("prem"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def request_channel_plan(self, event, args) -> None:
        cfg = await self.settings.section("channels")
        pid = args[0] if args else ""
        plan_item = next((p for p in cfg.get("plans", []) if str(p.get("id")) == str(pid)), None)
        if plan_item is None:
            return await self.show_channel_plans(event)
        user = await self.ensure_user(event)
        await get_plan_request_service(users=self.users).request(
            user["_id"], kind="channels", plan_id=str(plan_item.get("id")),
            title=str(plan_item.get("title")), price=float(plan_item.get("price") or 0),
            days=int(plan_item.get("duration_days") or 30),
            channels=int(plan_item.get("slots") or 0),
            username=str(user.get("username") or ""), note="channel slots request",
        )
        await self._request_sent(event, str(plan_item.get("title")),
                                 f"+{plan_item.get('slots')} channel(s)",
                                 fmt_money(plan_item.get("price")))

    async def prompt_custom_channels(self, event) -> None:
        slots = await self.channel_slots(int(getattr(event, "sender_id", 0) or 0))
        await self.set_input(
            event, "chplan_count", {"panel": "user"},
            prompt=("✍️ <b>How many channels do you need?</b>\n\n"
                    f"You already have <b>{slots['free']}</b> free channel(s) "
                    f"(in use: {slots['used']}).\n"
                    "Send the total number of channels you want, e.g. <code>5</code>."),
        )

    async def _set_custom_channels(self, event, data: Dict[str, Any], text: str) -> None:
        try:
            wanted = int(str(text).strip())
        except ValueError:
            raise ValueError("Send a whole number, e.g. 5")
        if wanted < 1 or wanted > 500:
            raise ValueError("Choose between 1 and 500")
        slots = await self.channel_slots(int(getattr(event, "sender_id", 0) or 0))
        extra = max(0, wanted - slots["free"])
        if extra == 0:
            return await self.render(event, Screen(
                f"🎁 Your free allowance already covers <b>{wanted}</b> channel(s).",
                [[btn("⬅️ Back", self.cb("chplan"), PRIMARY)], self.home_row()],
            ))
        user = await self.ensure_user(event)
        await get_plan_request_service(users=self.users).request(
            user["_id"], kind="channels", plan_id="custom",
            title=f"Extra channels ×{extra}", price=0, days=30, channels=extra,
            username=str(user.get("username") or ""),
            note=f"wants {wanted} channels in total ({extra} extra)",
        )
        await self._request_sent(event, f"Custom channel plan ×{extra}",
                                 f"{extra} extra channel(s)", "price on request")

    async def _request_sent(self, event, title: str, detail: str, price: str) -> None:
        support = await self.settings.section("support")
        contact = str(support.get("username") or "").lstrip("@")
        text = (
            "📨 <b>Request sent to the owner</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>{esc(title)}</b>\n"
            f"📦 {esc(detail)}\n"
            f"💵 {esc(price)}\n\n"
            "The owner has the full details and will set it up for you. You'll get a "
            "message here as soon as it is active."
        )
        rows = []
        if contact:
            rows.append([Button.url("💬 Contact owner now", f"https://t.me/{contact}")])
        rows.append([btn("⬅️ Back", self.cb("prem"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    # ==================================================================
    # auto comments (premium, plan based) — random positive comments
    # ==================================================================
    async def show_auto_comments(self, event) -> None:
        """Automatic comments on every new post, from the panel's own texts."""
        entitled = await self.users.has_feature(int(event.sender_id), "comments")
        cfg = await self.settings.section("comments")
        auto_cfg = cfg.get("auto", {}) or {}
        channels = await self._channels_for_user(int(event.sender_id))
        if not channels:
            await self._no_channel_screen(event, "💬 <b>Auto Comments</b>")
            return
        texts = [str(t) for t in (auto_cfg.get("texts") or []) if str(t).strip()]
        text = (
            "💬 <b>Auto Comments</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Every new post gets <b>random positive comments</b> from your "
            "accounts — no two posts look the same.\n"
            f"👥 Accounts per post: <b>{fmt_number(auto_cfg.get('accounts_per_post', 3))}</b>\n"
            f"📝 Comment pool: <b>{len(texts)}</b> ready-made positive comments\n"
            f"🎁 Status: <b>{'unlocked by your plan' if entitled else 'premium (needs a plan)'}</b>\n\n"
            "<b>Your channels</b>\n"
        )
        rows = []
        for channel in channels:
            bucket = channel.get("auto_comments") or {}
            state = "🟢 ON" if bucket.get("enabled") else "🔴 OFF"
            text += f"\n• <b>{esc(str(channel.get('title')))}</b> — {state}\n"
            rows.append([btn(f"⚙️ {channel.get('title')}", self.cb("ac_ch", channel["_id"]), PRIMARY)])
        if not entitled:
            rows.append([btn("👑 Get comments plan", self.cb("prem"), SUCCESS)])
        rows.append([Button.inline("➕ Add To Channel", self.cb("addch"), SUCCESS)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def show_auto_comments_channel(self, event, args) -> None:
        if await self.feature_locked(event, "comments", "💬 <b>Auto Comments</b>"):
            return
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return await self.show_auto_comments(event)
        channel_id = channel["_id"]
        bucket = channel.get("auto_comments") or {}
        cfg = await self.settings.section("comments")
        auto_cfg = cfg.get("auto", {}) or {}
        pool = await self.accounts.available_count()
        limits = await self.users.effective_limits(int(event.sender_id))
        texts = [t for t in (auto_cfg.get("texts") or []) if str(t).strip()]
        preview = "\n".join(f"  • {esc(str(t))}" for t in texts[:3]) or "  • (none configured)"
        text = (
            f"💬 <b>Auto Comments</b> — {esc(str(channel.get('title')))}\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"Status: {'🟢 On' if bucket.get('enabled') else '🔴 Off'}\n"
            f"Accounts per post: <b>{bucket.get('accounts') or auto_cfg.get('accounts_per_post', 3)}</b>\n"
            f"Your comment limit: <b>{limits.get('comment_accounts') or 'premium only'}</b>\n"
            f"Accounts available: <b>{pool}</b>\n\n"
            f"<b>Sample comments</b>\n{preview}\n"
        )
        rows = [
            [btn("🔴 Turn off" if bucket.get("enabled") else "🟢 Turn on",
                 self.cb("ac_toggle", channel_id), DANGER if bucket.get("enabled") else SUCCESS)],
            [btn("👥 Comments per post", self.cb("ac_count", channel_id), PRIMARY)],
            [btn("⬅️ Back", self.cb("ac"), PRIMARY)],
            self.home_row(),
        ]
        await self.render(event, Screen(text, rows))

    async def toggle_auto_comments(self, event, args) -> None:
        if await self.feature_locked(event, "comments", "💬 <b>Auto Comments</b>"):
            return
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return
        channel_id = channel["_id"]
        enabled = not bool((channel.get("auto_comments") or {}).get("enabled"))
        await self.channels.set_auto_comments(channel_id, enabled)
        await self.answer(event, "Auto comments " + ("enabled ✅" if enabled else "disabled 🔴"))
        if enabled:
            # Put the accounts inside right away so post #1 already gets comments.
            try:
                await get_auto_engagement().warm_channel(channel_id)
            except Exception as exc:  # noqa: BLE001 - the toggle already succeeded
                logger.debug(f"warm-up after enabling auto comments failed: {exc}")
        await self.show_auto_comments_channel(event, [str(channel_id)])

    async def prompt_auto_comments_count(self, event, args) -> None:
        if await self.feature_locked(event, "comments", "💬 <b>Auto Comments</b>"):
            return
        channel = await self._channel_or_none(event, args[0] if args else 0)
        if channel is None:
            return
        await self.set_input(
            event, "ac_count", {"panel": "user", "channel_id": int(channel["_id"])},
            prompt="👥 How many accounts should comment on each new post? (e.g. 3)",
        )

    async def _set_auto_comments_count(self, event, data: Dict[str, Any], text: str) -> None:
        channel = await self._channel_or_none(event, data.get("channel_id"))
        if channel is None:
            return
        try:
            value = int(str(text).strip())
        except ValueError:
            raise ValueError("Send a whole number, e.g. 3")
        limits = await self.users.effective_limits(int(event.sender_id))
        cap = int(limits.get("comment_accounts") or 0)
        if cap and value > cap:
            raise ValueError(f"Your plan allows up to {cap} accounts per post")
        if value < 1 or value > 100:
            raise ValueError("Choose between 1 and 100")
        await self.channels.set_auto_comments(channel["_id"], True, accounts=value)
        await self.render(event, Screen(
            f"✅ Auto comments set to <b>{value}</b> account(s) per post.",
            [[Button.inline("⬅️ Back", self.cb("ac_ch", channel["_id"]), PRIMARY)], self.home_row()],
        ))

    # ==================================================================
    # plan-included orders (no second payment)
    # ==================================================================
    def _plan_included_plan(self, title: str, quantity: int, accounts: int,
                            plan: Dict[str, Any]) -> Dict[str, Any]:
        """A zero-price quote for a service the user's plan already covers."""
        return {
            "plan_id": "plan_included",
            "title": title,
            "quantity": int(quantity),
            "accounts": int(accounts),
            "price": 0.0,
            "description": "Included in your active plan",
            "covered_by_plan": True,
            "plan_kinds": list((plan or {}).get("kinds") or []),
        }

    async def use_comment_plan(self, event) -> None:
        """Comments through the plan (no payment, plan covers it)."""
        user_id = int(event.sender_id)
        if not await self.users.has_feature(user_id, "comments"):
            return await self.feature_locked(event, "comments", "💬 <b>Comments</b>")
        limits = await self.users.effective_limits(user_id)
        plan = limits.get("plan") or {}
        cfg = await self.settings.section("comments")
        cap = int(cfg.get("max_accounts", 10) or 10)
        accounts = min(int(limits.get("comment_accounts") or cap) or cap, cap)
        quote = self._plan_included_plan(
            f"Comments • included in your plan", accounts, accounts, plan,
        )
        await self.set_input(
            event, "comment_link", {"panel": "user", "plan": quote},
            prompt=(
                "💬 <b>Comments — covered by your plan</b>\n\n"
                f"Accounts that will comment: <b>{accounts}</b>\n"
                "💸 Price: <b>0 (your plan pays for it)</b>\n\n"
                "Send the <b>post link</b> you want comments on."
            ),
        )

    async def _premium_gate(self, event, section: str) -> bool:
        """Locked until the owner activates that plan kind for this user.

        The old behaviour (any premium plan unlocks everything) still works:
        a plan with ``kinds: ["all"]`` unlocks every service.
        """
        cfg = await self.settings.section(section)
        if not cfg.get("premium_only", True):
            return True
        titles = {"comments": "💬 <b>Comments</b>"}
        locked = await self.feature_locked(event, section, titles.get(section, section.title()))
        return not locked

    async def show_comments(self, event) -> None:
        cfg = await self.settings.section("comments")
        plan = await self.users.plan_of(int(event.sender_id))
        premium = plan.get("type") in ("premium", "unlimited")
        text = "💬 <b>Comments</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        if cfg.get("premium_only", True) and not premium:
            text += (
                "🔒 Comments are a <b>premium feature</b>\n\n"
                "👑 Premium members can post comments from up to "
                f"{cfg.get('max_accounts', 10)} accounts.\n"
            )
            rows = [[btn("👑 Get Premium", self.cb("prem"), SUCCESS)], self.home_row()]
            await self.render(event, Screen(text, rows))
            return
        text += f"Accounts allowed: <b>{cfg.get('max_accounts', 10)}</b>\n"
        if await self.users.has_feature(int(event.sender_id), "comments"):
            text += "✅ <b>Covered by your plan</b> — no extra payment.\n"
        rows = []
        if await self.users.has_feature(int(event.sender_id), "comments"):
            rows.append([btn("🚀 Comment now (included in plan)", self.cb("ac_use"), SUCCESS)])
        for plan in cfg.get("plans", []):
            text += f"\n• <b>{esc(str(plan.get('title')))}</b> — {fmt_money(plan.get('price'))}"
            rows.append([btn(f"🛒 {plan.get('title')} • {fmt_money(plan.get('price'))}",
                             self.cb("buy_comment", plan.get("id")), SUCCESS)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def buy_comment(self, event, args) -> None:
        if not await self._premium_gate(event, "comments"):
            return
        plan = await self.orders.quote_plan("comment", args[0] if args else "")
        if not plan:
            await self.answer(event, "Plan not found", alert=True)
            return
        cfg = await self.settings.section("comments")
        cap = int(cfg.get("max_accounts", 10) or 0)
        quantity = int(plan.get("quantity") or 1)
        if cap and quantity > cap:
            # Only what the account limit allows is sold — never charge for
            # comments the bot is not allowed to post.
            unit = float(plan.get("price", 0) or 0) / max(1, quantity)
            plan["quantity"] = cap
            plan["price"] = round(unit * cap, 2)
            plan["title"] = f"{plan.get('title')} • capped to {cap} accounts"
            await self.answer(
                event,
                f"Limit allows {cap} accounts per order — you are being charged for {cap}.",
            )
            quantity = cap
        plan["accounts"] = min(quantity, cap) if cap else quantity
        await self.set_input(event, "comment_link", {"panel": "user", "plan": plan},
                             prompt="💬 Send the <b>post link</b> you want comments on.")

    async def _comment_link(self, event, data: Dict[str, Any], text: str) -> None:
        await self.set_input(event, "comment_text",
                             {"panel": "user", "plan": data.get("plan"), "link": text},
                             prompt=(
                                 "✍️ Send the <b>comment text</b>.\n\n"
                                 "Tip: separate variants with a new line and each account gets a different one."
                             ))

    async def _create_comment_order(self, event, data: Dict[str, Any], text: str) -> None:
        plan = data.get("plan") or {}
        texts = [line.strip() for line in str(text).split("\n") if line.strip()]
        payload = {"link": data.get("link"), "text": texts[0] if texts else "",
                   "texts": texts, "accounts": int(plan.get("accounts") or plan.get("quantity") or 1)}
        try:
            order = await self.orders.create_order(
                int(event.sender_id), "comment", payload, plan=plan,
                quantity=int(plan.get("quantity") or 0),
            )
        except ValueError as exc:
            await self.answer(event, f"⚠️ {exc}", alert=True)
            return
        self.executor.submit(order)
        await self.render(event, Screen("✅ <b>Comment order placed</b>\n\n" + self.order_block(order, detailed=True),
                                        [[Button.inline("🧾 My Orders", self.cb("om"), PRIMARY)], self.home_row()]))

    # ==================================================================
    # channels (add bot)
    # ==================================================================
    async def _bot_username(self) -> str:
        try:
            from src.runtime import get_bot
            bot = get_bot()
            client = getattr(bot, "tbot", None)
            me = await client.get_me() if client is not None else None
            return getattr(me, "username", None) or ""
        except Exception:
            return ""

    async def show_add_to_chat(self, event, kind: str = "channel") -> None:
        username = await self._bot_username()
        rights = "post_messages+edit_messages+delete_messages+invite_users"
        if kind == "group":
            link = f"https://t.me/{username}?startgroup=true&admin={rights}" if username else ""
            title = "➕ <b>Add To Group</b>"
        else:
            link = f"https://t.me/{username}?startchannel=true&admin={rights}" if username else ""
            title = "➕ <b>Add To Channel</b>"
        text = (
            f"{title}\n━━━━━━━━━━━━━━━━━━━━\n"
            "Tap the button below → choose your channel/group → grant the "
            "permissions Telegram asks for.\n\n"
            "As soon as I'm added:\n"
            "• the channel is linked to <b>your</b> account,\n"
            "• <b>auto reactions</b> start on every new post — free, up to your "
            "plan's account limit,\n"
            "• for private channels my accounts join automatically through a link "
            "I create (keep <b>Invite Users</b> permission on).\n\n"
            "👑 Auto views, comments, members and extra channels are premium — "
            "the owner activates them for you.\n"
        )
        rows = []
        if link:
            rows.append([Button.url("🚀 Add me now", link)])
        else:
            text += "\n⚠️ Could not detect the bot username — use @BotFather's link manually."
        channels = await self._channels_for_user(int(event.sender_id))
        if channels:
            text += "\n<b>Linked channels</b>\n"
            for channel in channels[:10]:
                text += f"• {esc(str(channel.get('title')))} (<code>{channel['_id']}</code>)\n"
        rows.append([btn("📋 My channels", self.cb("ch"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def claim_channel(self, event, args) -> None:
        """Take ownership of a channel whose adder Telegram did not report."""
        try:
            channel_id = int(args[0])
        except (IndexError, TypeError, ValueError):
            await self.answer(event, "Unknown channel", alert=True)
            return
        user_id = int(event.sender_id)
        channel = await self.channels.get(channel_id)
        if channel is None:
            await self.answer(event, "Channel not found", alert=True)
            return
        current = channel.get("owner_id")
        if current and int(current) != user_id:
            if not await self.is_owner(user_id):
                await self.answer(event, "⛔️ This channel belongs to another user", alert=True)
                return
        await self.channels.set_owner(channel_id, user_id)
        try:
            await self.auto.enable_free_plan(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"free plan start failed for {channel_id}: {exc}")
        await self.answer(event, "✅ Channel assigned to you — reactions are ON")
        await self.show_my_channels(event)

    async def show_my_channels(self, event, args=None) -> None:
        """Every channel this user owns — nobody else's is ever listed."""
        page = int(args[0]) if args and str(args[0]).isdigit() else 0
        per_page = 5
        channels = await self._channels_for_user(int(event.sender_id))
        user = await self.ensure_user(event)
        limits = await self.users.effective_limits(user["_id"])
        if not channels:
            await self._no_channel_screen(event, "📋 <b>My Channels</b>")
            return
        pool = await self.accounts.available_count()
        page_channels = channels[page * per_page:(page + 1) * per_page]
        slots = await self.channel_slots(user["_id"])
        text = (
            "📋 <b>MY CHANNELS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Total: <b>{len(channels)}</b> • 👥 accounts in pool: <b>{pool}</b>\n"
            f"📡 Auto-reaction channels: <b>{slots['used']}/{slots['total']}</b>"
            + (" ♾" if slots["unlimited"] else f" (free: {slots['free']})") + "\n"
            f"🎁 Free reactions: up to <b>{limits.get('free_reaction_accounts')}</b> "
            "accounts per post\n"
            "<i>🔒 Locked = added by you, disable allowed, removal only by the owner.</i>\n"
        )
        rows: List[List[Any]] = []
        for channel in page_channels:
            ar = channel.get("auto_reactions") or {}
            av = channel.get("auto_views") or {}
            emojis = ar.get("emojis") or []
            text += (
                f"\n<b>{esc(str(channel.get('title')))}</b>"
                f"{' • 📢' if channel.get('kind') == 'channel' else ' • 👥'}"
                f"{' • 🔒 locked' if channel.get('locked') else ''}\n"
                f"  <code>{channel['_id']}</code> • "
                f"{'public @' + str(channel.get('username')) if channel.get('username') else 'private'}\n"
                f"  reactions: {'🟢 on' if ar.get('enabled') else '🔴 off'}"
                f" • accounts/post: <b>{ar.get('accounts') or 'auto'}</b>\n"
                f"  views: {'🟢 on' if av.get('enabled') else '🔴 off'}"
                f"{' • emoji: ' + ' '.join(emojis[:6]) if emojis else ''}\n"
            )
            if not channel.get("owner_id"):
                # Telegram hid who added the bot — let the operator claim it.
                text += "  <i>unassigned</i>\n"
                rows.append([btn(f"📌 Assign {channel.get('title')} to me",
                                 self.cb("ch_claim", channel["_id"]), SUCCESS)])
            else:
                rows.append([
                    btn(f"❤️ {channel.get('title')}", self.cb("ar_ch", channel["_id"]), PRIMARY),
                    btn("🔄 Views", self.cb("av_ch", channel["_id"]), SUCCESS),
                ])
            rows.append([
                btn("🔴 Stop reactions" if ar.get("enabled") else "🟢 Start reactions",
                    self.cb("ar_toggle", channel["_id"]), DANGER if ar.get("enabled") else SUCCESS),
            ])
        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", self.cb("ch", page - 1)))
        if (page + 1) * per_page < len(channels):
            nav.append(btn("Next ➡️", self.cb("ch", page + 1)))
        if nav:
            rows.append(nav)
        rows.append([btn("📡 Manage channel slots", self.cb("chplan"), PRIMARY)])
        rows.append([Button.inline("➕ Add To Channel", self.cb("addch"), SUCCESS),
                     Button.inline("➕ Add To Group", self.cb("addgr"), SUCCESS)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    # ==================================================================
    # order management
    # ==================================================================
    async def show_order_management(self, event) -> None:
        await self.show_orders(event, ["0"])

    async def show_orders(self, event, args) -> None:
        page = int(args[0]) if args and str(args[0]).isdigit() else 0
        per_page = 5
        orders = await self.orders.list_for_user(int(event.sender_id), limit=200)
        active = self.active_of(orders)
        page_orders = orders[page * per_page:(page + 1) * per_page]
        text = (
            "🧾 <b>Order Management</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"Total orders: <b>{len(orders)}</b> • Active: <b>{len(active)}</b>\n"
        )
        if not orders:
            text += "\nYou haven't placed any order yet."
        rows = []
        for order in page_orders:
            text += f"\n{self.order_block(order)}\n"
            row = [btn(f"{status_line(order.get('status')).split()[0]} {kind_label(order.get('kind'))}",
                       self.cb("order", order["_id"]), PRIMARY)]
            row.append(btn("🚫 Cancel", self.cb("order_cancel", order["_id"]), DANGER))
            rows.append(row)
        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", self.cb("orders", page - 1)))
        if (page + 1) * per_page < len(orders):
            nav.append(btn("Next ➡️", self.cb("orders", page + 1)))
        if nav:
            rows.append(nav)
        rows.append([btn("🔄 Refresh", self.cb("orders", page), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def show_order_detail(self, event, args) -> None:
        order_id = args[0] if args else ""
        order = await self.orders.get(order_id)
        if not order or order.get("user_id") != int(event.sender_id):
            await self.answer(event, "Order not found", alert=True)
            return await self.show_orders(event, ["0"])
        text = "🧾 <b>Order details</b>\n━━━━━━━━━━━━━━━━━━━━\n" + self.order_block(order, detailed=True)
        payload = order.get("payload") or {}
        if payload.get("link") or payload.get("channel"):
            text += f"\n🔗 <code>{esc(str(payload.get('link') or payload.get('channel')))}</code>"
        rows = []
        if order.get("status") in ACTIVE_STATUSES:
            rows.append([btn("🚫 Cancel & refund", self.cb("order_cancel", order_id), DANGER)])
        rows.append([btn("⬅️ My Orders", self.cb("om"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def cancel_order(self, event, args) -> None:
        order_id = args[0] if args else ""
        order = await self.orders.get(order_id)
        if not order or order.get("user_id") != int(event.sender_id):
            await self.answer(event, "Order not found", alert=True)
            return
        try:
            await self.orders.cancel(order_id, by_user=True)
            await self.answer(event, "Order cancelled & refunded 💸")
        except ValueError as exc:
            await self.answer(event, str(exc), alert=True)
        await self.show_orders(event, ["0"])

    # ==================================================================
    # support
    # ==================================================================
    async def show_support(self, event) -> None:
        cfg = await self.settings.section("support")
        username = (cfg.get("username") or getattr(app_config, "SUPPORT_USERNAME", "") or "").lstrip("@")
        text = (
            "🆘 <b>Support</b>\n━━━━━━━━━━━━━━━━━━━━\n"
            f"{esc(str(cfg.get('text') or 'Need help? Message us.'))}\n"
        )
        if username:
            text += f"\n📩 Contact: @{esc(username)}"
        else:
            text += "\n📩 Support contact is not configured yet — the owner can set it in the admin panel."
        rows = []
        if username:
            rows.append([Button.url("💬 Message support", f"https://t.me/{username}")])
        rows.append([btn("👑 Premium", self.cb("prem"), PRIMARY)])
        rows.append(self.home_row())
        await self.render(event, Screen(text, rows))

    async def _unused(self, event) -> None:  # pragma: no cover - keeps linters honest
        await self.clear_input(getattr(event, "chat_id", 0) or 0)
