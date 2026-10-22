"""
Dynamic, admin-editable settings (prices, quotas and free limits).

Everything an owner/admin can change from the admin panel lives in a single
MongoDB document ``settings/_id="global"``. Defaults are defined here so a fresh
deployment works out of the box, and reads deep-merge the stored document over
the defaults — which means new keys added in a later release are picked up
without needing a migration.

Nothing in this module ever mutates the defaults.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Optional

from src import config as app_config
from src.db.store import COLL_SETTINGS, get_store, get_store_sync

logger = logging.getLogger(__name__)

SETTINGS_DOC_ID = "global"

# ---------------------------------------------------------------------------
# Reaction emojis
# ---------------------------------------------------------------------------
# Telegram's ``SendReactionRequest`` accepts **only** its own reaction set — any
# other emoji comes back as ``ReactionInvalidError`` ("Invalid reaction
# provided (only emoji are allowed)"). The production log caught the pool
# picking "✅", which is *not* part of that set, so a whole post lost its
# reactions. This list is the server-side set; everything we send is checked
# against it (variation selectors like "❤️"/"❤" are treated as the same emoji).
ALLOWED_REACTION_EMOJIS: List[str] = [
    "👍", "👎", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🤯", "😱", "🤬", "😢", "🎉", "🤩",
    "🤮", "💩", "🙏", "👌", "🕊", "🤡", "🥱", "🥴", "😍", "🐳", "❤️‍🔥", "🌚", "🌭",
    "💯", "🤣", "⚡", "🍌", "🏆", "💔", "🤨", "😐", "🍓", "🍾", "💋", "🖕", "😈", "😴",
    "😭", "🤓", "👻", "👨‍💻", "👀", "🎃", "🙈", "😇", "😨", "🤝", "✍", "🤗", "🫡",
    "🎅", "🎄", "☃", "💅", "🤪", "🗿", "🆒", "💘", "🙉", "🦄", "😘", "💊", "🙊", "😎",
    "👾", "🤷‍♂️", "🤷", "🤷‍♀️", "😡",
]

# Emojis the free tier uses by default — all of them inside Telegram's set.
DEFAULT_POSITIVE_EMOJIS: List[str] = [
    "👍", "❤️", "🔥", "🎉", "🥰", "👏", "😁", "🤩", "💯", "🙏", "⚡", "🏆", "👌",
    "😍", "🤝", "🕊",
]


def normalise_emoji(value: Any) -> str:
    """Compare emojis ignoring the optional VS16 (``❤️`` == ``❤``) and spaces."""
    if not isinstance(value, str):
        return ""
    return value.replace("\ufe0f", "").replace("\u200d\ufe0f", "\u200d").strip()


_ALLOWED_REACTION_LOOKUP = {normalise_emoji(e): e for e in ALLOWED_REACTION_EMOJIS}


def is_allowed_reaction_emoji(value: Any) -> bool:
    """Would Telegram accept this emoji as a reaction?"""
    return normalise_emoji(value) in _ALLOWED_REACTION_LOOKUP


def sanitize_reaction_emojis(values: Any) -> "tuple[List[str], List[str]]":
    """Split a configured emoji list into (kept, dropped-unsupported).

    Keeps the author's spelling (``❤️`` stays ``❤️``), drops anything Telegram
    would reject and de-duplicates.
    """
    kept: List[str] = []
    dropped: List[str] = []
    seen = set()
    if isinstance(values, str):
        values = [values]
    for raw in values or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        emoji = raw.strip()
        key = normalise_emoji(emoji)
        if key not in _ALLOWED_REACTION_LOOKUP:
            if emoji not in dropped:
                dropped.append(emoji)
            continue
        if key in seen:
            continue
        seen.add(key)
        kept.append(emoji)
    return kept, dropped

# Positive comments the bot posts by itself on new posts (auto comments).
# Randomly picked per account; the whole list is editable in the admin panel.
DEFAULT_POSITIVE_COMMENTS: List[str] = [
    "Bahut badhiya post 🔥",
    "Very nice 👏",
    "Great content, keep it up 💯",
    "Superb work 🙌",
    "Nice one 👍",
    "Amazing 👌",
    "Keep posting such things ❤️",
    "Beautiful 😍",
    "Excellent post 🎉",
    "Really useful, thanks 🙏",
    "Best channel in this niche 🏆",
    "Waiting for the next one ⚡",
]

# The onboarding copy shown the first time a user opens the panel. Fully
# editable from the admin panel (Settings → Welcome message).
DEFAULT_WELCOME_TEXT = (
    "<b>🔥 Welcome To Multi Reaction Bot!</b>\n\n"
    "<b>⚠️ Automatically react to every new post in your channel with emojis.</b>\n\n"
    "🤔 <b>How To Use:</b>\n"
    "1 ➔ Use <b>Add Me As Admin</b> in your channel\n"
    "2 ➔ Give me <b>Message Manage Sent</b> permission\n\n"
    "🥳 <b>Tap below to get started:</b>"
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "version": 1,
    # ------------------------------------------------------------------
    # Reactions (free tier + paid plans + auto reactions on new posts)
    # ------------------------------------------------------------------
    "reactions": {
        "enabled": True,
        # Free-of-charge reaction orders for every user:
        "free_account_limit": 10,      # max accounts used per free order
        "free_min_accounts": 1,        # if fewer accounts exist, use what we have
        "free_daily_orders": 3,        # free orders per user per day
        "free_cooldown_seconds": 60,   # min gap between two free orders per user
        "emojis": list(DEFAULT_POSITIVE_EMOJIS),
        "random_emoji": True,          # pick randomly from the list above
        "delay_min": 3.0,              # per-account delay between reactions (s)
        "delay_max": 12.0,
        "auto": {
            "enabled": True,           # auto-react to every new channel post (free)
            # Free tier uses a random number of accounts between min_accounts and
            # accounts_per_post (8-10 by default), capped by the pool size.
            "accounts_per_post": 10,
            "min_accounts": 8,
            "delay_min": 1.0,          # random 1-3 s between accounts, looks natural
            "delay_max": 3.0,
            "join_private_channels": True,   # auto-join via invite link if needed
            "only_for_admins_of_channel": True,
        },
        "plans": [
            {"id": "react_50", "title": "50 Reactions", "quantity": 50, "price": 49,
             "description": "50 reactions spread over your chosen accounts",
             "accounts": 5, "auto_posts": 0, "badge": ""},
            {"id": "react_200", "title": "200 Reactions", "quantity": 200, "price": 149,
             "description": "200 reactions • up to 10 accounts", "accounts": 10,
             "auto_posts": 25, "badge": "🔥 Popular"},
            {"id": "react_500_auto", "title": "500 + Auto 100 posts", "quantity": 500,
             "price": 349, "description": "500 reactions and auto-reactions on 100 posts",
             "accounts": 20, "auto_posts": 100, "badge": "👑 Best value"},
        ],
    },
    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    "views": {
        "enabled": True,
        "free_daily_orders": 1,
        "free_quantity": 100,
        "plans": [
            {"id": "views_1k", "title": "1,000 Views", "quantity": 1000, "price": 79,
             "description": "1,000 views on a single post", "badge": ""},
            {"id": "views_5k", "title": "5,000 Views", "quantity": 5000, "price": 299,
             "description": "5,000 views on a single post", "badge": "🔥 Popular"},
            {"id": "views_10k", "title": "10,000 Views", "quantity": 10000, "price": 499,
             "description": "10,000 views on a single post", "badge": "👑 Best value"},
        ],
        "auto": {
            "enabled": True,
            "views_per_post": 100,
            "delay_min": 10.0,
            "delay_max": 60.0,
        },
    },
    # ------------------------------------------------------------------
    # Members (account driven joins into a channel)
    # ------------------------------------------------------------------
    "members": {
        "enabled": True,
        "delay_min": 2.0,
        "delay_max": 8.0,
        "plans": [
            {"id": "members_100", "title": "100 Members", "quantity": 100, "price": 199,
             "description": "100 accounts join your channel", "badge": ""},
            {"id": "members_500", "title": "500 Members", "quantity": 500, "price": 799,
             "description": "500 accounts join your channel", "badge": "🔥 Popular"},
        ],
    },
    # ------------------------------------------------------------------
    # Comments (premium only by default) + automatic positive comments
    # ------------------------------------------------------------------
    # Auto-reaction channels. Free users get ``free_limit`` channels; every
    # extra channel needs a channel plan (or a premium plan that carries slots).
    # ------------------------------------------------------------------
    "channels": {
        "free_limit": 2,
        "auto_enable": True,          # turn auto reactions on for a new channel
        "lock_added": True,           # a channel a user added can only be disabled
        "delay_min": 1.0,
        "delay_max": 3.0,
        "plans": [
            {"id": "ch_1", "title": "+1 Channel", "slots": 1, "price": 99,
             "duration_days": 30, "description": "1 extra channel for auto reactions", "badge": ""},
            {"id": "ch_3", "title": "+3 Channels", "slots": 3, "price": 249,
             "duration_days": 30, "description": "3 extra channels for auto reactions", "badge": ""},
            {"id": "ch_10", "title": "+10 Channels", "slots": 10, "price": 699,
             "duration_days": 30, "description": "10 extra channels for auto reactions", "badge": ""},
        ],
    },
    # ------------------------------------------------------------------
    "comments": {
        "enabled": True,
        "premium_only": True,
        "delay_min": 4.0,
        "delay_max": 15.0,
        "max_accounts": 10,          # how many accounts one user may use
        "plans": [
            {"id": "comment_25", "title": "25 Comments", "quantity": 25, "price": 199,
             "description": "25 comments on your post", "badge": ""},
        ],
        # Automatic comments on every new channel post (premium, plan based).
        # The bot picks a random text from ``texts`` for every account, so the
        # comment section looks natural instead of copy-pasted.
        "auto": {
            "enabled": True,
            "accounts_per_post": 3,
            "delay_min": 4.0,
            "delay_max": 12.0,
            "texts": list(DEFAULT_POSITIVE_COMMENTS),
        },
    },
    # ------------------------------------------------------------------
    # Per-user account allowances
    # ------------------------------------------------------------------
    "accounts": {
        "free_limit": 3,             # signed-in accounts a free user may add
        "premium_limit": 25,
        "owner_unlimited": True,
        # How often the pool supervisor checks that every signed-in account is
        # still connected (and reconnects the ones that dropped). Accounts are
        # only removed on a ban/block/revoked session.
        "supervise_seconds": 60,
    },
    # ------------------------------------------------------------------
    # Premium membership (unlocks views, comments, channel slots, higher limits)
    # ------------------------------------------------------------------
    "premium": {
        "enabled": True,
        # Which buttons a user gets. Auto reactions are FREE for everyone up to
        # the free limit — a premium plan is needed to go beyond it, or to use
        # any other service (views / comments / members / channel slots).
        "free_features": ["reactions"],
        "feature_labels": {
            "reactions": "🔄 Auto Reactions (free up to the free-channel limit)",
            "views": "🔄 Auto Views + 📈 Order Views",
            "comments": "💬 Auto Comments",
            "members": "👥 Members",
            "channels": "📡 Extra Auto-Reaction Channels",
        },
        "benefits": [
            "📡 Extra channels for auto reactions",
            "💬 Comments unlocked",
            "👥 More accounts per order",
            "🚀 Priority execution queue",
        ],
        # A granted plan already paid for its services: while it is active, the
        # orders of the covered kinds run without asking for money again (this is
        # what the operator reported as "plan ke baad bhi paise maang raha hai").
        # Set ``plan_orders_included`` to false to charge per order as before.
        "plan_orders_included": True,
        # 0 = no daily limit for plan covered orders.
        "plan_orders_per_day": 0,
        # How many extra channels a plan with the "channels" feature grants when
        # it carries no explicit number of slots.
        "default_channels": 5,
        "plans": [
            {"id": "premium_30", "title": "Premium • 30 days", "days": 30,
             "price": 199, "accounts": 15, "channels": 5,
             "description": "All premium features + 5 extra channels for a month",
             "kinds": ["all"], "badge": "🔥 Popular"},
            {"id": "premium_90", "title": "Premium • 90 days", "days": 90,
             "price": 499, "accounts": 25, "channels": 15,
             "description": "3 months, best value — 15 extra channels",
             "kinds": ["all"], "badge": "👑 Best value"},
            {"id": "plan_reactions", "title": "Auto Reactions • 30 days", "days": 30,
             "price": 149, "accounts": 20, "channels": 2,
             "description": "More accounts + 2 extra channels",
             "kinds": ["reactions"], "badge": ""},
            {"id": "plan_views", "title": "Auto Views • 30 days", "days": 30,
             "price": 199, "accounts": 20, "description": "Automatic views on every new post",
             "kinds": ["views"], "badge": ""},
            {"id": "plan_comments", "title": "Auto Comments • 30 days", "days": 30,
             "price": 249, "accounts": 15, "description": "Comments on your posts, premium only",
             "kinds": ["comments"], "badge": ""},
        ],
    },
    # ------------------------------------------------------------------
    # Payments (manual UPI by default — no gateway keys needed)
    # ------------------------------------------------------------------
    "payments": {
        "upi_id": getattr(app_config, "PAYMENT_UPI_ID", ""),
        "upi_name": getattr(app_config, "PAYMENT_UPI_NAME", ""),
        "min_topup": 50,
        "instructions": (
            "Pay the exact amount to the UPI ID below, then send the 12 digit "
            "UTR / reference number here."
        ),
        "auto_approve_topups": False,
        "telegram_stars_enabled": False,
    },
    # ------------------------------------------------------------------
    # Force subscribe (join gate) — owner-only, edited in the admin panel
    # ------------------------------------------------------------------
    "force_sub": {
        "enabled": False,
        "cache_seconds": 60,
        "title": "📢 Join our channels to continue",
        "message": ("To use this bot you must join the channel(s) below first.\n"
                    "Join, then tap <b>✅ I joined — verify</b>."),
        "channels": [],
    },
    # ------------------------------------------------------------------
    # Support / misc
    # ------------------------------------------------------------------
    "support": {
        "username": getattr(app_config, "SUPPORT_USERNAME", ""),
        "text": "Need help? Message the support account below.",
    },
    "panel": {
        "user_panel_enabled": True,
        "welcome": DEFAULT_WELCOME_TEXT,
        "announcement": "",
        "show_free_tier": True,
    },
    "orders": {
        "refund_on_failure": True,
        "max_active_per_user": 5,
        "auto_retry": 1,
    },
    "logging": {
        "level": "INFO",
    },
}

# Keys that must stay numerically sane when edited from the panel.
NUMERIC_PATHS = {
    "reactions.free_account_limit": int,
    "reactions.free_min_accounts": int,
    "reactions.free_daily_orders": int,
    "reactions.free_cooldown_seconds": int,
    "force_sub.cache_seconds": int,
    "reactions.delay_min": float,
    "reactions.delay_max": float,
    "reactions.auto.accounts_per_post": int,
    "reactions.auto.min_accounts": int,
    "reactions.auto.delay_min": float,
    "reactions.auto.delay_max": float,
    "views.auto.views_per_post": int,
    "views.free_daily_orders": int,
    "views.free_quantity": int,
    "views.auto.views_per_post": int,
    "views.auto.delay_min": float,
    "views.auto.delay_max": float,
    "comments.max_accounts": int,
    "comments.auto.accounts_per_post": int,
    "comments.auto.delay_min": float,
    "comments.auto.delay_max": float,
    "accounts.supervise_seconds": int,
    "premium.plan_orders_per_day": int,
    "accounts.free_limit": int,
    "accounts.premium_limit": int,
    "payments.min_topup": int,
    "orders.max_active_per_user": int,
    "orders.auto_retry": int,
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Return base merged with override (override wins, dicts merge deeply)."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class SettingsService:
    """Read/write facade over the ``settings`` collection."""

    CACHE_TTL = 5.0  # seconds — hot paths call get() a lot

    def __init__(self, store=None):
        self._store = store
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_time = 0.0

    @property
    def store(self):
        return self._store or get_store_sync()

    # -- read --------------------------------------------------------------
    async def get(self, refresh: bool = False) -> Dict[str, Any]:
        import time
        if not refresh and self._cache is not None and (time.time() - self._cache_time) < self.CACHE_TTL:
            return self._cache
        raw = None
        try:
            raw = await self.store.find_one(COLL_SETTINGS, {"_id": SETTINGS_DOC_ID})
        except Exception as exc:
            logger.error(f"Could not read settings from store: {exc}")
        merged = _deep_merge(DEFAULT_SETTINGS, raw.get("data", {}) if raw else {})
        self._cache = merged
        self._cache_time = time.time()
        return merged

    async def section(self, name: str) -> Dict[str, Any]:
        return (await self.get()).get(name, {})

    # -- write -------------------------------------------------------------
    async def set_path(self, path: str, value: Any) -> Dict[str, Any]:
        """Set a nested key (dotted path) and persist the whole settings doc."""
        current = await self.get(refresh=True)
        updated = copy.deepcopy(current)
        node = updated
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        # Only the delta vs defaults is stored so future default additions flow in.
        delta = _diff_delta(DEFAULT_SETTINGS, updated)
        try:
            await self.store.update_one(
                COLL_SETTINGS, {"_id": SETTINGS_DOC_ID}, {"$set": {"data": delta}}, upsert=True
            )
        except Exception as exc:
            logger.error(f"Failed to persist settings: {exc}")
            raise
        self._cache = None
        logger.info(f"Settings updated: {path} = {value!r}")
        return await self.get(refresh=True)

    async def update_section(self, section: str, values: Dict[str, Any]) -> Dict[str, Any]:
        current = await self.get(refresh=True)
        updated = copy.deepcopy(current)
        updated.setdefault(section, {}).update(values)
        delta = _diff_delta(DEFAULT_SETTINGS, updated)
        await self.store.update_one(
            COLL_SETTINGS, {"_id": SETTINGS_DOC_ID}, {"$set": {"data": delta}}, upsert=True
        )
        self._cache = None
        return await self.get(refresh=True)

    async def replace_list(self, path: str, items: List[Any]) -> Dict[str, Any]:
        """Replace a list (e.g. plans) wholesale."""
        return await self.set_path(path, items)

    # -- plan helpers ------------------------------------------------------
    async def get_plan(self, section: str, plan_id: str) -> Optional[Dict[str, Any]]:
        section_data = await self.section(section)
        for plan in section_data.get("plans", []):
            if str(plan.get("id")) == str(plan_id):
                return plan
        return None

    async def plans(self, section: str) -> List[Dict[str, Any]]:
        return list((await self.section(section)).get("plans", []))

    async def emojis(self) -> List[str]:
        """The reaction pool, guaranteed to hold only emojis Telegram accepts."""
        reaction_cfg = await self.section("reactions")
        kept, dropped = sanitize_reaction_emojis(reaction_cfg.get("emojis") or [])
        if dropped:
            logger.warning(
                f"Dropping reaction emoji(s) Telegram does not accept: "
                f"{', '.join(dropped)} — using {len(kept) or 'the default'} instead"
            )
        return kept or list(DEFAULT_POSITIVE_EMOJIS)

    async def repair_reaction_emojis(self) -> List[str]:
        """Write the cleaned emoji list back to the store (called on boot).

        An installation that stored "✅" (or any emoji outside Telegram's
        reaction set) keeps failing on every post until the list is fixed — this
        does that automatically and reports what was removed.
        """
        reaction_cfg = await self.section("reactions")
        stored = list(reaction_cfg.get("emojis") or [])
        kept, dropped = sanitize_reaction_emojis(stored)
        if kept == stored:
            return kept
        final = kept or list(DEFAULT_POSITIVE_EMOJIS)
        await self.set_path("reactions.emojis", final)
        logger.warning(
            f"Reaction emoji list repaired — removed {dropped or 'duplicates'}, "
            f"kept {len(final)}"
        )
        return final

    async def free_reaction_accounts(self) -> int:
        cfg = await self.section("reactions")
        try:
            return max(0, int(cfg.get("free_account_limit", 10)))
        except (TypeError, ValueError):
            return 10


def _diff(defaults: Any, current: Any) -> Any:
    """Return the minimal structure of *current* that differs from *defaults*."""
    if isinstance(defaults, dict) and isinstance(current, dict):
        out: Dict[str, Any] = {}
        for key, value in current.items():
            if key not in defaults:
                out[key] = value
            else:
                sub = _diff(defaults[key], value)
                if sub is not _SAME:
                    out[key] = sub
        return out
    if isinstance(defaults, list) and isinstance(current, list):
        if defaults == current:
            return _SAME
        return copy.deepcopy(current)
    if defaults == current:
        return _SAME
    return copy.deepcopy(current)


class _Same:
    """Sentinel: child value equals its default."""

    def __repr__(self) -> str:  # pragma: no cover
        return "<SAME>"

    def __reduce__(self):  # pragma: no cover - pickling guard
        return (_Same, ())


_SAME = _Same()


def _diff_delta(defaults: Any, current: Any) -> Any:
    """``_diff`` but always returns a dict ({}) when nothing changed."""
    result = _diff(defaults, current)
    return {} if result is _SAME else result


_settings_service: Optional[SettingsService] = None


def get_settings_service(store=None) -> SettingsService:
    """Process-wide settings service (safe to call from anywhere)."""
    global _settings_service
    if _settings_service is None or store is not None:
        _settings_service = SettingsService(store)
    return _settings_service


def reset_settings_service() -> None:
    global _settings_service
    _settings_service = None
