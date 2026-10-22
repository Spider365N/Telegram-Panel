"""
User accounts, roles, wallet balance, plans and daily free-tier quotas.

One MongoDB document per Telegram user (``users/_id=<user_id>``) so nothing is
lost on restart:

    {
      _id: 800000001,
      username: "kaka",
      first_name: "Kaka",
      role: "user" | "admin" | "owner",
      balance: 250.0,            # wallet credit
      total_spent: 149.0,
      plan: {"type": "free"|"premium", "expires_at": ts, ...},
      usage: {"day": "2026-10-15", "orders": {...}, "counters": {...}},
      banned: false,
      created_at / updated_at / last_seen
    }
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src import config as app_config
from src.db.store import COLL_TRANSACTIONS, COLL_USERS, get_store_sync

# Panel-added owners/admins — several owners are supported.
COLL_OPERATORS = "operators"

logger = logging.getLogger(__name__)

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"
ROLE_USER = "user"

VALID_ROLES = (ROLE_OWNER, ROLE_ADMIN, ROLE_USER)


def _owner_id() -> int:
    """Read the owner id at call time so env/reload changes always apply."""
    try:
        return int(getattr(app_config, "OWNER_ID", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _admin_ids() -> set:
    try:
        return {int(a) for a in getattr(app_config, "ADMIN_IDS", [])}
    except (TypeError, ValueError):
        return set()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class UserService:
    """Everything user related: records, roles, wallet, quotas."""

    def __init__(self, store=None, settings=None):
        self._store = store
        self._settings = settings

    @property
    def store(self):
        return self._store or get_store_sync()

    @property
    def settings(self):
        if self._settings is None:
            from src.services.settings import get_settings_service
            self._settings = get_settings_service()
        return self._settings

    # ------------------------------------------------------------------
    # Records & roles
    # ------------------------------------------------------------------
    async def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_USERS, {"_id": int(user_id)})

    async def get_or_create(
        self,
        user_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        user_id = int(user_id)
        user = await self.get_user(user_id)
        role = ROLE_OWNER if user_id == _owner_id() else (
            ROLE_ADMIN if user_id in _admin_ids() else ROLE_USER
        )
        if user is None:
            user = {
                "_id": user_id,
                "username": username or "",
                "first_name": first_name or "",
                "role": role,
                "balance": 0.0,
                "total_spent": 0.0,
                "plan": {"type": "free"},
                "usage": {"day": _today(), "counters": {}},
                "stats": {"orders": 0, "reactions": 0, "views": 0, "members": 0,
                          "comments": 0, "channels": 0},
                "banned": False,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "last_seen": time.time(),
            }
            await self.store.insert_one(COLL_USERS, user)
            logger.info(f"New panel user registered: {user_id} ({username})")
        else:
            updates: Dict[str, Any] = {"last_seen": time.time(), "updated_at": _now_iso()}
            if username is not None and username != user.get("username"):
                updates["username"] = username
            if first_name is not None and first_name != user.get("first_name"):
                updates["first_name"] = first_name
            # Owners/admins configured in .env always get their elevated role.
            if role != ROLE_USER and user.get("role") != role:
                updates["role"] = role
            try:
                await self.store.update_one(COLL_USERS, {"_id": user_id}, {"$set": updates})
            except Exception as exc:
                logger.debug(f"user touch failed: {exc}")
            user.update(updates)
        return user

    async def set_role(self, user_id: int, role: str) -> bool:
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role {role!r}")
        await self.get_or_create(user_id)
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)}, {"$set": {"role": role, "updated_at": _now_iso()}}
        )
        return True

    async def operator_of(self, user_id: int) -> Optional[Dict[str, Any]]:
        """The ``operators`` record of a panel-added owner/admin (or ``None``).

        Several owners are supported: the owner id in the environment is the
        built-in one, and every extra owner/admin the dashboard adds lands in
        the ``operators`` collection. This is what makes the *same* rights apply
        in the bot, the panels and the web dashboard.
        """
        try:
            doc = await self.store.find_one(COLL_OPERATORS, {"_id": int(user_id)})
        except Exception as exc:  # noqa: BLE001 - never break the access check
            logger.debug(f"operator lookup failed for {user_id}: {exc}")
            return None
        return doc or None

    async def operator_role(self, user_id: int) -> str:
        doc = await self.operator_of(user_id)
        role = str((doc or {}).get("role") or "").lower()
        return role if role in (ROLE_OWNER, ROLE_ADMIN) else ""

    async def role_of(self, user_id: int) -> str:
        user_id = int(user_id)
        env_owner = _owner_id()
        if env_owner and user_id == env_owner:
            return ROLE_OWNER
        panel_role = await self.operator_role(user_id)
        if panel_role:
            return panel_role
        user = await self.get_user(user_id)
        if user is None:
            return ROLE_ADMIN if user_id in _admin_ids() else ROLE_USER
        return user.get("role", ROLE_USER)

    async def is_owner(self, user_id: int) -> bool:
        user_id = int(user_id)
        env_owner = _owner_id()
        if env_owner and user_id == env_owner:
            return True
        if await self.operator_role(user_id) == ROLE_OWNER:
            return True
        return await self.role_of(user_id) == ROLE_OWNER

    async def is_admin(self, user_id: int) -> bool:
        """Owner or admin (admin panel access)."""
        if await self.is_owner(user_id):
            return True
        if int(user_id) in _admin_ids():
            return True
        if await self.operator_role(user_id) == ROLE_ADMIN:
            return True
        return (await self.role_of(user_id)) in (ROLE_OWNER, ROLE_ADMIN)

    async def is_banned(self, user_id: int) -> bool:
        user = await self.get_user(user_id)
        return bool(user and user.get("banned"))

    async def list_users(self, limit: int = 20, skip: int = 0) -> List[Dict[str, Any]]:
        """Newest users first, paged.

        (The subscript used to sit on the coroutine — ``await find(...)[skip:]``
        evaluates the slice *before* awaiting, so the admin Users page raised
        "'coroutine' object is not subscriptable" and rendered nothing.)
        """
        rows = await self.store.find(
            COLL_USERS, {}, sort=[("created_at", -1)], limit=limit + skip
        )
        return list(rows)[skip:]

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self.get_or_create(user_id)
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)},
            {"$set": {"banned": bool(banned), "updated_at": _now_iso()}},
        )

    async def count_users(self) -> int:
        return await self.store.count(COLL_USERS)

    # ------------------------------------------------------------------
    # Wallet
    # ------------------------------------------------------------------
    async def balance(self, user_id: int) -> float:
        user = await self.get_user(user_id)
        return float((user or {}).get("balance") or 0.0)

    async def credit(
        self,
        user_id: int,
        amount: float,
        reason: str,
        reference: Optional[str] = None,
        count_as_topup: bool = True,
    ) -> float:
        """Add money to a wallet and write a transaction row."""
        amount = round(float(amount), 2)
        if amount <= 0:
            raise ValueError("credit amount must be positive")
        await self.get_or_create(user_id)
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)}, {"$inc": {"balance": amount}}
        )
        await self.store.insert_one(COLL_TRANSACTIONS, {
            "user_id": int(user_id),
            "type": "credit",
            "amount": amount,
            "reason": reason,
            "reference": reference,
            "kind": "topup" if count_as_topup else "adjustment",
            "created_at": _now_iso(),
        })
        new_balance = await self.balance(user_id)
        logger.info(f"Wallet credit {amount} for {user_id} ({reason}); balance={new_balance}")
        return new_balance

    async def debit(
        self,
        user_id: int,
        amount: float,
        reason: str,
        reference: Optional[str] = None,
        count_as_spend: bool = True,
    ) -> bool:
        """Atomically remove money from a wallet. False when funds are short."""
        amount = round(float(amount), 2)
        if amount < 0:
            raise ValueError("debit amount must be positive")
        if amount == 0:
            return True
        await self.get_or_create(user_id)
        updated = None
        try:
            from src.db.store import MongoStore
            if isinstance(self.store, MongoStore):
                updated = await self.store.atomic_inc(
                    COLL_USERS, {"_id": int(user_id)}, {"balance": -amount},
                    min_values={"balance": amount},
                )
            else:
                current = await self.balance(user_id)
                if current + 1e-9 < amount:
                    updated = None
                else:
                    await self.store.update_one(
                        COLL_USERS, {"_id": int(user_id)}, {"$inc": {"balance": -amount}}
                    )
                    updated = {"balance": current - amount}
        except Exception as exc:
            logger.error(f"debit failed for {user_id}: {exc}")
            return False
        if updated is None:
            logger.info(
                f"User {user_id} tried to spend {amount} with an empty wallet — "
                f"shown the top-up screen"
            )
            return False
        await self.store.insert_one(COLL_TRANSACTIONS, {
            "user_id": int(user_id),
            "type": "debit",
            "amount": amount,
            "reason": reason,
            "reference": reference,
            "kind": "order" if count_as_spend else "adjustment",
            "created_at": _now_iso(),
        })
        if count_as_spend:
            await self.store.update_one(
                COLL_USERS, {"_id": int(user_id)}, {"$inc": {"total_spent": amount}}
            )
        return True

    async def transactions(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        return await self.store.find(
            COLL_TRANSACTIONS, {"user_id": int(user_id)},
            sort=[("created_at", -1)], limit=limit,
        )

    # ------------------------------------------------------------------
    # Plans & limits
    # ------------------------------------------------------------------
    async def plan_of(self, user_id: int) -> Dict[str, Any]:
        """The user's active plan (expired plans fall back to free)."""
        user = await self.get_user(user_id) or {}
        plan = dict(user.get("plan") or {"type": "free"})
        expires_at = plan.get("expires_at")
        if expires_at:
            try:
                if float(expires_at) < time.time():
                    expired = plan.get("type")
                    plan = {"type": "free", "expired_from": expired,
                            "kinds": [], "accounts": 0}
            except (TypeError, ValueError):
                pass
        if plan.get("type") != "free":
            plan.setdefault("kinds", list(self.PLAN_KINDS))
        if await self.is_owner(user_id) or await self.is_admin(user_id):
            plan = {**plan, "type": "unlimited", "accounts": 10 ** 6,
                    "kinds": list(self.PLAN_KINDS) + ["all"],
                    "unlimited": True}
        return plan

    async def has_feature(self, user_id: int, feature: str) -> bool:
        """Does this user have *feature* (reactions / views / comments / channels)?

        Owner and admins always do. ``unlimited`` grants everything. Otherwise
        the plan's ``kinds`` list decides — an expired plan grants nothing.
        """
        if await self.is_admin(user_id):
            return True
        plan = await self.plan_of(user_id)
        if plan.get("unlimited") or plan.get("type") == "unlimited":
            return True
        if plan.get("type") == "free":
            return False
        kinds = {str(k).lower() for k in (plan.get("kinds") or [])}
        return "all" in kinds or feature.lower() in kinds

    async def features_of(self, user_id: int) -> Dict[str, bool]:
        """{feature: allowed} for every gateable service."""
        return {name: await self.has_feature(user_id, name)
                for name in self.PLAN_KINDS}

    async def is_premium(self, user_id: int) -> bool:
        if await self.is_admin(user_id):
            return True
        plan = await self.plan_of(user_id)
        return plan.get("type") in ("premium", "unlimited")

    # Every plan type the owner can hand out. ``unlimited`` is the special
    # no-limits grant (owner-only) that the spec asked for.
    PLAN_KINDS = ("reactions", "views", "comments", "members", "channels")

    def normalise_kinds(self, kinds) -> List[str]:
        """Accept 'views', 'views,comments', ['views'] or 'all'."""
        if kinds is None:
            return list(self.PLAN_KINDS)
        if isinstance(kinds, str):
            parts = [k.strip().lower() for k in kinds.replace("+", ",").split(",") if k.strip()]
        else:
            parts = [str(k).strip().lower() for k in kinds if str(k).strip()]
        if not parts or "all" in parts or "unlimited" in parts:
            return list(self.PLAN_KINDS)
        return [k for k in parts if k in self.PLAN_KINDS] or list(self.PLAN_KINDS)

    async def plan_summary(self, plan: Dict[str, Any]) -> str:
        """Human readable plan block: what it covers and how long it runs."""
        labels = {}
        try:
            premium = await self.settings.section("premium")
            labels = premium.get("feature_labels") or {}
        except Exception:  # noqa: BLE001 - labels are cosmetic
            labels = {}
        kinds = list(plan.get("kinds") or [])
        pretty = ", ".join(str(labels.get(k, k)) for k in kinds) or "every service"
        if plan.get("unlimited") or plan.get("type") == "unlimited":
            return (
                "♾ <b>Unlimited plan</b>\n"
                f"Services: <b>{pretty}</b>\n"
                "Duration: <b>no expiry</b>\n"
                "Accounts: <b>unlimited</b>"
            )
        days = int(plan.get("days") or 0)
        expires_at = plan.get("expires_at")
        until = ""
        if expires_at:
            try:
                until = datetime.fromtimestamp(float(expires_at), timezone.utc).strftime("%d %b %Y")
            except (TypeError, ValueError, OSError):
                until = ""
        accounts = plan.get("accounts")
        lines = [
            f"Services: <b>{pretty}</b>",
            f"Duration: <b>{days} day(s)</b>" + (f" — active till <b>{until}</b>" if until else ""),
        ]
        if accounts:
            lines.append(f"Accounts allowed: <b>{int(accounts)}</b>")
        return "\n".join(lines)

    async def _notify_plan_activated(
        self, user_id: int, plan: Dict[str, Any], *, linked_channels: Optional[int] = None,
    ) -> None:
        """Tell the user their plan is live and whether automation is linked."""
        try:
            from src.services.notify import get_notifier
            from telethon import Button

            summary = await self.plan_summary(plan)
            kinds = {str(kind).lower() for kind in (plan.get("kinds") or [])}
            if plan.get("unlimited") or plan.get("type") == "unlimited":
                kinds.add("all")
            automations = [
                label for kind, label in (("views", "auto views"), ("comments", "auto comments"))
                if "all" in kinds or kind in kinds
            ]
            text = (
                "🎉 <b>Your plan is now active!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 <code>{int(user_id)}</code>\n{summary}\n\n"
                "Every service your plan covers is unlocked — covered orders run "
                "without an extra payment."
            )
            if automations and linked_channels == 0:
                text += (
                    "\n\n⚠️ No channel is linked to this account yet. Automatic "
                    f"{', '.join(automations)} only run on channels linked to the "
                    "plan holder as owner, original adder, or sponsor. Ask an operator "
                    "to check the channel assignment in Admin Dashboard → Channels."
                )
            elif automations:
                text += (
                    f"\n\nAutomatic {', '.join(automations)} will run on the next "
                    "new post in channels linked to your account."
                )
            buttons = [[Button.inline("🏠 Open panel", "u:home", style="success")]]
            await get_notifier().to_user(int(user_id), text, buttons=buttons)
        except Exception as exc:  # noqa: BLE001 - a notice must never break a grant
            logger.debug(f"plan notice failed for {user_id}: {type(exc).__name__}")

    async def _after_plan_change(self, user_id: int) -> Dict[str, Any]:
        """Re-enable plan-based automation for the channels of this user."""
        try:
            from src.services.autoengage import get_auto_engagement
            return await get_auto_engagement().refresh_automation_for_user(int(user_id))
        except Exception as exc:  # noqa: BLE001 - hook only
            logger.debug(f"plan automation hook failed for {user_id}: {type(exc).__name__}")
            return {"channels": None, "views": 0, "comments": 0}

    async def grant_plan(
        self,
        user_id: int,
        *,
        kinds=None,
        days: int = 30,
        accounts: Optional[int] = None,
        channels: Optional[int] = None,
        unlimited: bool = False,
        note: str = "",
        notify: bool = True,
    ) -> Dict[str, Any]:
        """Give a user a plan for *kinds* (auto reactions / views / comments …).

        ``unlimited=True`` creates the special no-limit plan: every feature,
        no account cap, no expiry (owner-only — enforced by the caller).
        """
        await self.get_or_create(user_id)
        wanted = self.normalise_kinds(kinds)
        if unlimited:
            plan = {
                "type": "unlimited", "unlimited": True,
                "kinds": list(self.PLAN_KINDS), "accounts": 10 ** 6,
                "days": 0, "note": note or "unlimited grant",
                "granted_at": _now_iso(),
            }
        else:
            days = max(1, int(days or 30))
            plan = {
                "type": "premium",
                "expires_at": time.time() + days * 86400,
                "days": days,
                "kinds": wanted,
                "accounts": int(accounts) if accounts else None,
                "channels": int(channels) if channels else None,
                "note": note,
                "granted_at": _now_iso(),
            }
            if plan["accounts"] is None:
                plan.pop("accounts")
            if plan["channels"] is None:
                plan.pop("channels")
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)},
            {"$set": {"plan": plan, "updated_at": _now_iso()}},
        )
        logger.info(
            f"Plan granted to {user_id}: "
            f"{'unlimited' if unlimited else '+'.join(wanted) + f' for {days}d'}"
        )
        # Re-check channel links before notifying the user. A plan holder can
        # only unlock channels explicitly linked to that same account; never
        # guess across tenants when Telegram hid who added the bot.
        outcome = await self._after_plan_change(user_id)
        if notify:
            await self._notify_plan_activated(
                user_id, dict(plan), linked_channels=outcome.get("channels")
            )
        return plan

    async def grant_premium(
        self,
        user_id: int,
        days: int = 30,
        accounts: Optional[int] = None,
        note: str = "",
        kinds=None,
    ) -> Dict[str, Any]:
        """Backwards-compatible helper: premium plan for the given kinds."""
        return await self.grant_plan(
            user_id, kinds=kinds, days=days, accounts=accounts, note=note
        )

    async def revoke_premium(self, user_id: int) -> None:
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)},
            {"$set": {"plan": {"type": "free", "kinds": [], "accounts": 0},
                      "updated_at": _now_iso()}},
        )

    async def effective_limits(self, user_id: int) -> Dict[str, Any]:
        """Merge global settings with this user's plan into one limits dict."""
        accounts_cfg = await self.settings.section("accounts")
        reactions_cfg = await self.settings.section("reactions")
        comments_cfg = await self.settings.section("comments")
        channels_cfg = await self.settings.section("channels")
        premium = await self.is_premium(user_id)
        plan = await self.plan_of(user_id)

        features = await self.features_of(user_id)
        plans_cfg = await self.settings.section("premium")
        premium_accounts = int(plans_cfg.get("default_accounts", accounts_cfg.get("premium_limit", 25)))

        # Free reactions are free for everybody (that was the whole point).
        free_reaction_cap = int(reactions_cfg.get("free_account_limit", 10))
        if plan.get("unlimited"):
            account_cap = 10 ** 6
            reaction_cap = 10 ** 6
        elif features.get("reactions"):
            account_cap = int(plan.get("accounts") or premium_accounts)
            reaction_cap = int(plan.get("accounts") or reactions_cfg.get("premium_account_limit", 25))
        else:
            account_cap = int(accounts_cfg.get("free_limit", 3))
            reaction_cap = free_reaction_cap

        # Channel slots: the free allowance is always available; a plan adds its
        # own ``channels`` count on top (that is the "diffent plan" the owner
        # asked for — extra channels beyond the free allowance).
        free_channels = int(channels_cfg.get("free_limit", 2) or 0)
        if plan.get("unlimited"):
            channel_slots = 10 ** 6
        else:
            plan_slots = plan.get("channels")
            if plan_slots is None and features.get("channels"):
                plan_slots = int(plans_cfg.get("default_channels", 5) or 5)
            channel_slots = free_channels + int(plan_slots or 0)

        return {
            "premium": premium,
            "plan": plan,
            "features": features,
            "unlimited": bool(plan.get("unlimited")),
            "accounts": account_cap,
            "reaction_accounts": max(1, reaction_cap),
            "comment_accounts": (
                10 ** 6 if plan.get("unlimited")
                else (int(comments_cfg.get("max_accounts", 10)) if features.get("comments") else 0)
            ),
            "free_reaction_accounts": free_reaction_cap,
            "channels": channel_slots,
            "free_channels": free_channels,
            "auto_view_accounts": (
                10 ** 6 if plan.get("unlimited")
                else (int(accounts_cfg.get("premium_limit", 25)) if features.get("views") else 0)
            ),
        }

    # ------------------------------------------------------------------
    # Free-tier daily quotas
    # ------------------------------------------------------------------
    async def _usage(self, user_id: int) -> Dict[str, Any]:
        user = await self.get_user(user_id) or {}
        usage = user.get("usage") or {}
        if usage.get("day") != _today():
            usage = {"day": _today(), "counters": {}}
            await self.store.update_one(
                COLL_USERS, {"_id": int(user_id)}, {"$set": {"usage": usage}}
            )
        return usage

    async def get_counter(self, user_id: int, name: str) -> int:
        usage = await self._usage(user_id)
        return int((usage.get("counters") or {}).get(name, 0))

    async def bump_counter(self, user_id: int, name: str, amount: int = 1) -> int:
        usage = await self._usage(user_id)
        counters = usage.get("counters") or {}
        counters[name] = int(counters.get(name, 0)) + amount
        usage["counters"] = counters
        usage["day"] = _today()
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)}, {"$set": {"usage": usage}}
        )
        return counters[name]

    async def check_daily_quota(self, user_id: int, name: str, limit: int) -> Dict[str, Any]:
        """Returns {"ok": bool, "used": int, "limit": int, "remaining": int}."""
        if await self.is_admin(user_id) or limit < 0:
            return {"ok": True, "used": 0, "limit": -1, "remaining": -1}
        used = await self.get_counter(user_id, name)
        return {
            "ok": used < limit,
            "used": used,
            "limit": limit,
            "remaining": max(0, limit - used),
        }

    async def mark_action_time(self, user_id: int, name: str) -> None:
        """Remember when a (free) action last happened, for cooldown checks."""
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)}, {"$set": {f"times.{name}": time.time()}}
        )

    async def seconds_since(self, user_id: int, name: str) -> Optional[float]:
        user = await self.get_user(user_id) or {}
        stamp = (user.get("times") or {}).get(name)
        if not stamp:
            return None
        try:
            return max(0.0, time.time() - float(stamp))
        except (TypeError, ValueError):
            return None

    async def cooldown_remaining(self, user_id: int, name: str, seconds: int) -> int:
        """Seconds the user still has to wait (0 when the cooldown is over)."""
        if seconds <= 0 or await self.is_admin(user_id):
            return 0
        elapsed = await self.seconds_since(user_id, name)
        if elapsed is None:
            return 0
        return max(0, int(round(seconds - elapsed)))

    async def bump_stat(self, user_id: int, field: str, amount: int = 1) -> None:
        await self.store.update_one(
            COLL_USERS, {"_id": int(user_id)}, {"$inc": {f"stats.{field}": amount}}
        )

    async def summary(self, user_id: int) -> Dict[str, Any]:
        user = await self.get_user(user_id) or {}
        limits = await self.effective_limits(user_id)
        return {"user": user, "limits": limits}

    async def daily_signups(self, days: int = 7) -> int:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return await self.store.count(COLL_USERS, {"created_at": {"$gte": since}})


_user_service: Optional[UserService] = None


def get_user_service(store=None, settings=None) -> UserService:
    global _user_service
    if _user_service is None or store is not None or settings is not None:
        _user_service = UserService(store, settings)
    return _user_service


def reset_user_service() -> None:
    global _user_service
    _user_service = None
