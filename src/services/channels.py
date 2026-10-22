"""
Channels/groups the bot is installed in, and their auto-engagement settings.

When a user taps "➕ Add To Channel" / "➕ Add To Group" they receive a deep link
that promotes the bot with the needed permissions. The moment the bot is added,
Telegram sends an update which lands here: the channel is registered in MongoDB
against the user who added it, and the free auto-reaction/auto-view plan starts
working immediately (if the owner enables it).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.db.store import COLL_CHANNELS, get_store_sync
from src.services.settings import get_settings_service

logger = logging.getLogger(__name__)

# Permissions the bot needs to react/view/comment on posts
REQUIRED_BOT_RIGHTS = ("post_messages", "edit_messages", "delete_messages", "invite_users")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChannelService:
    def __init__(self, store=None, settings=None):
        self._store = store
        self._settings = settings

    @property
    def store(self):
        return self._store or get_store_sync()

    @property
    def settings(self):
        if self._settings is None:
            self._settings = get_settings_service()
        return self._settings

    # ------------------------------------------------------------------
    async def register(
        self,
        channel_id: int,
        owner_id: Optional[int] = None,
        *,
        title: Optional[str] = None,
        username: Optional[str] = None,
        invite_link: Optional[str] = None,
        kind: str = "channel",
        added_by: Optional[int] = None,
        reassign: bool = False,
        owner_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert a channel record (idempotent — safe to call on every update)."""
        channel_id = int(channel_id)
        existing = await self.store.find_one(COLL_CHANNELS, {"_id": channel_id})
        defaults = {
            "title": title or (existing or {}).get("title") or str(channel_id),
            "username": username,
            "invite_link": invite_link,
            "kind": kind,
            "added_by": added_by,
            "auto_reactions": {
                "enabled": False,
                "accounts": 0,          # 0 = use plan default
                "posts_left": 0,        # subscription credits
                "emojis": [],
                "per_post_accounts": None,
            },
            "auto_views": {"enabled": False, "views_per_post": 0, "posts_left": 0},
            "auto_comments": {"enabled": False, "accounts": 0, "texts": []},
            # Everybody allowed to unlock premium automation here: the owner, the
            # person who added the bot and anyone a paid order registered.
            # A channel a user added is *locked*: it can be disabled but never
            # deleted by that user, so the free allowance cannot be farmed by
            # removing a channel and adding another one instead.
            "locked": True,
            "added_by_user": bool(owner_id),
            "sponsors": [],
            "stats": {"posts_seen": 0, "reactions_sent": 0, "views_sent": 0,
                      "comments_sent": 0},
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }
        if owner_id is not None:
            defaults["owner_id"] = int(owner_id)
            defaults["owner_source"] = owner_source or "added_by"
        if existing is None:
            doc = {"_id": channel_id, **defaults}
            await self.store.insert_one(COLL_CHANNELS, doc)
            logger.info(f"Channel registered: {channel_id} ({title}) owner={owner_id}")
            return doc

        updates: Dict[str, Any] = {"updated_at": _now_iso()}
        for key in ("title", "username", "invite_link", "kind"):
            value = locals().get(key)
            if value:
                updates[key] = value
        # Who owns this channel? The first attribution sticks — unless the
        # stored owner was only guessed (Telegram hid the adder) and the real
        # adder shows up later, or an operator explicitly reassigns it. That is
        # the production case where a channel stayed on the wrong owner and its
        # paid plan never applied.
        stored_owner = existing.get("owner_id")
        stored_source = str(existing.get("owner_source") or "")
        guessed = stored_source in ("creator_guess", "only_admin", "creator", "guess", "")
        if owner_id is not None:
            new_owner = int(owner_id)
            if not stored_owner:
                updates["owner_id"] = new_owner
                updates["owner_source"] = owner_source or "added_by"
            elif int(stored_owner) != new_owner and (reassign or guessed):
                updates["owner_id"] = new_owner
                updates["owner_source"] = owner_source or "reassign"
                logger.info(
                    f"Channel {channel_id} re-attributed: {stored_owner} → {new_owner} "
                    f"({updates['owner_source']})"
                )
        await self.store.update_one(COLL_CHANNELS, {"_id": channel_id}, {"$set": updates})
        existing.update(updates)
        logger.info(f"Channel updated: {channel_id} ({updates.get('title')})")
        return existing

    async def mark_post_seen(self, channel_id: int, message_id: int) -> bool:
        """Claim a channel post for automation. False = already handled.

        Telegram delivers the same post through more than one update (and
        re-delivers it after a restart), which made every new post react twice.
        The highest message id seen per channel is stored, so an older or
        repeated id is skipped.
        """
        channel_id = int(channel_id)
        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return True
        doc = await self.get(channel_id)
        if doc is None:
            return True
        seen = int(doc.get("last_post_id") or 0)
        if message_id <= seen:
            logger.info(
                f"Skipping post {message_id} on {channel_id} — already handled "
                f"(last seen {seen})"
            )
            return False
        await self.store.update_one(
            COLL_CHANNELS, {"_id": channel_id},
            {"$set": {"last_post_id": message_id, "updated_at": _now_iso()}},
        )
        return True

    async def get(self, channel_id: int) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_CHANNELS, {"_id": int(channel_id)})

    async def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        return await self.store.find_one(COLL_CHANNELS, {"username": username.lstrip("@")})

    async def list_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        return await self.store.find(COLL_CHANNELS, {"owner_id": int(user_id)},
                                     sort=[("created_at", -1)])

    async def list_unowned(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Channels where the bot was added but the actor could not be detected.

        Filtered in Python so every backend (Mongo, JSON, memory) behaves the
        same — a couple of unowned channels is not worth a server-side query.
        """
        rows = await self.store.find(COLL_CHANNELS, {}, limit=limit,
                                     sort=[("created_at", -1)])
        return [row for row in rows if not row.get("owner_id")]

    async def list_all(self, limit: int = 50) -> List[Dict[str, Any]]:
        return await self.store.find(COLL_CHANNELS, {}, sort=[("created_at", -1)], limit=limit)

    async def set_locked(self, channel_id: int, locked: bool) -> Dict[str, Any]:
        """Lock/unlock a channel (admin only from the panels)."""
        await self.store.update_one(
            COLL_CHANNELS, {"_id": int(channel_id)},
            {"$set": {"locked": bool(locked), "updated_at": _now_iso()}},
        )
        return await self.get(channel_id) or {}

    async def auto_channel_count(self, user_id: int, feature: str = "auto_reactions") -> int:
        """How many channels of this user already have *feature* enabled."""
        user_id = int(user_id)
        count = 0
        for doc in await self.list_all(limit=10000):
            owner = doc.get("owner_id")
            sponsors = [int(s) for s in (doc.get("sponsors") or []) if str(s).lstrip("-").isdigit()]
            if int(owner or 0) != user_id and user_id not in sponsors:
                continue
            bucket = doc.get(feature) or {}
            if bucket.get("enabled"):
                count += 1
        return count

    async def channel_slots_of(self, user_id: int, users=None) -> Dict[str, Any]:
        """Free allowance + plan slots for *user_id* (used by the panels/gates)."""
        cfg = await self.settings.section("channels")
        free = int(cfg.get("free_limit", 2) or 0)
        if users is None:
            from src.services.users import get_user_service
            users = get_user_service()
        limits = await users.effective_limits(int(user_id))
        total = int(limits.get("channels") or free)
        used = await self.auto_channel_count(int(user_id))
        return {"free": free, "total": total, "used": used,
                "left": max(0, total - used), "unlimited": bool(limits.get("unlimited"))}

    async def can_enable_auto(self, channel_id: int, user_id: int, users=None,
                              feature: str = "auto_reactions") -> Dict[str, Any]:
        """May this user switch *feature* on for this channel?"""
        doc = await self.get(channel_id)
        if doc is None:
            return {"allowed": False, "reason": "unknown channel"}
        slots = await self.channel_slots_of(user_id, users)
        if slots["unlimited"]:
            return {"allowed": True, "slots": slots}
        bucket = doc.get(feature) or {}
        if bucket.get("enabled"):
            return {"allowed": True, "slots": slots}      # already on
        if slots["used"] < slots["total"]:
            return {"allowed": True, "slots": slots}
        return {"allowed": False, "slots": slots,
                "reason": (f"Free plan allows {slots['total']} channel(s) with auto "
                           f"reactions and all of them are in use")}

    async def set_owner(self, channel_id: int, owner_id: int, *,
                        source: str = "manual",
                        keep_sponsors: bool = False) -> Dict[str, Any]:
        """Assign (or hand over) a channel to a user.

        A handover *moves* the entitlement with the channel: the new owner is the
        recorded owner, the added-by and the only sponsor, so the previous
        operator does not keep premium access to somebody else's channel by
        accident. Paid buyers are re-registered as sponsors by the next completed
        order (``executor._register_sponsor``).
        """
        channel_id = int(channel_id)
        owner_id = int(owner_id)
        existing = await self.get(channel_id) or {}
        updates: Dict[str, Any] = {
            "owner_id": owner_id, "owner_source": source, "updated_at": _now_iso(),
        }
        if not keep_sponsors:
            updates["added_by"] = owner_id
            updates["sponsors"] = [owner_id]
            if existing.get("owner_id") and int(existing["owner_id"]) != owner_id:
                updates["previous_owner"] = int(existing["owner_id"])
        await self.store.update_one(COLL_CHANNELS, {"_id": channel_id}, {"$set": updates})
        try:
            await self.add_sponsor(channel_id, owner_id)
        except Exception as exc:  # noqa: BLE001 - the assignment already landed
            logger.debug(f"could not add {owner_id} as sponsor of {channel_id}: {exc}")
        logger.info(f"Channel {channel_id} assigned to {owner_id} ({source})")
        return await self.get(channel_id) or {}

    async def remove(self, channel_id: int, *, force: bool = False) -> int:
        """Delete a channel record.

        A locked channel (added by a user) is only removable by an admin/owner —
        ``force=True`` is what the admin panel passes. That is what stops a user
        from deleting a channel just to add another one for free.
        """
        channel_id = int(channel_id)
        if not force:
            doc = await self.get(channel_id) or {}
            if doc.get("locked"):
                logger.info(f"Channel {channel_id} is locked — removal refused")
                return 0
        return await self.store.delete_one(COLL_CHANNELS, {"_id": channel_id})

    async def count(self) -> int:
        return await self.store.count(COLL_CHANNELS, {})

    # ------------------------------------------------------------------
    async def set_auto_reactions(
        self,
        channel_id: int,
        enabled: bool,
        *,
        accounts: Optional[int] = None,
        posts_left: Optional[int] = None,
        emojis: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        updates: Dict[str, Any] = {
            "auto_reactions.enabled": bool(enabled),
            "updated_at": _now_iso(),
        }
        if accounts is not None:
            updates["auto_reactions.accounts"] = int(accounts)
        if posts_left is not None:
            updates["auto_reactions.posts_left"] = int(posts_left)
        if emojis is not None:
            updates["auto_reactions.emojis"] = list(emojis)
        await self.store.update_one(COLL_CHANNELS, {"_id": int(channel_id)}, {"$set": updates})
        return await self.get(channel_id) or {}

    async def set_auto_views(
        self, channel_id: int, enabled: bool, *,
        views_per_post: Optional[int] = None, posts_left: Optional[int] = None,
    ) -> Dict[str, Any]:
        updates: Dict[str, Any] = {"auto_views.enabled": bool(enabled), "updated_at": _now_iso()}
        if views_per_post is not None:
            updates["auto_views.views_per_post"] = int(views_per_post)
        if posts_left is not None:
            updates["auto_views.posts_left"] = int(posts_left)
        await self.store.update_one(COLL_CHANNELS, {"_id": int(channel_id)}, {"$set": updates})
        return await self.get(channel_id) or {}

    async def doc_for_link(self, link: str) -> Optional[Dict[str, Any]]:
        """The stored channel behind a post/invite link (any id spelling)."""
        try:
            from src.peers import PeerResolver, parse_link

            info = parse_link(link or "")
            if info.username:
                doc = await self.get_by_username(info.username)
                if doc:
                    return doc
            if info.chat_id is None:
                return None
            internal = PeerResolver.to_internal_channel_id(info.chat_id)
            for candidate in (int(info.chat_id), int(f"-100{internal}"), internal):
                doc = await self.get(candidate)
                if doc:
                    return doc
        except Exception as exc:  # noqa: BLE001 - lookup helper only
            logger.debug(f"doc_for_link failed for {link}: {exc}")
        return None

    async def set_auto_comments(
        self, channel_id: int, enabled: bool, *,
        accounts: Optional[int] = None, texts: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Switch automatic positive comments on/off for a channel."""
        updates: Dict[str, Any] = {
            "auto_comments.enabled": bool(enabled), "updated_at": _now_iso(),
        }
        if accounts is not None:
            updates["auto_comments.accounts"] = int(accounts)
        if texts is not None:
            updates["auto_comments.texts"] = [str(t).strip() for t in texts if str(t).strip()]
        await self.store.update_one(COLL_CHANNELS, {"_id": int(channel_id)}, {"$set": updates})
        return await self.get(channel_id) or {}

    async def add_sponsor(self, channel_id: int, user_id: int) -> Dict[str, Any]:
        """Remember who paid for / manages this channel (plan entitlement)."""
        doc = await self.get(channel_id) or {}
        sponsors = [int(s) for s in (doc.get("sponsors") or []) if str(s).lstrip("-").isdigit()]
        if int(user_id) in sponsors:
            return doc
        sponsors.append(int(user_id))
        await self.store.update_one(
            COLL_CHANNELS, {"_id": int(channel_id)},
            {"$set": {"sponsors": sponsors, "updated_at": _now_iso()}},
        )
        logger.info(f"Channel {channel_id}: {user_id} registered as a sponsor")
        return await self.get(channel_id) or {}

    async def channels_for_user(self, user_id: int) -> List[Dict[str, Any]]:
        """Every channel a user owns, added or sponsors (panel listing)."""
        user_id = int(user_id)
        rows = await self.store.find(COLL_CHANNELS, {}, limit=500)
        out = []
        for row in rows:
            members = {row.get("owner_id"), row.get("added_by"), *(row.get("sponsors") or [])}
            if any(str(m).lstrip("-").isdigit() and int(m) == user_id for m in members if m is not None):
                out.append(row)
        return out

    async def consume_auto_quota(self, channel_id: int, kind: str = "reactions",
                                 posts: int = 1) -> int:
        """Use up subscription credits. Returns remaining posts (or -1 unlimited)."""
        doc = await self.get(channel_id)
        if doc is None:
            return 0
        key = "auto_reactions" if kind == "reactions" else "auto_views"
        bucket = doc.get(key) or {}
        left = int(bucket.get("posts_left", 0) or 0)
        if left <= 0:
            return left
        left = max(0, left - posts)
        await self.store.update_one(
            COLL_CHANNELS, {"_id": int(channel_id)}, {"$set": {f"{key}.posts_left": left}}
        )
        if left == 0:
            # subscription exhausted → switch automation off, keep the config
            await self.store.update_one(
                COLL_CHANNELS, {"_id": int(channel_id)},
                {"$set": {f"{key}.enabled": False, "updated_at": _now_iso()}},
            )
        return left

    async def bump_stat(self, channel_id: int, field: str, amount: int = 1) -> None:
        await self.store.update_one(
            COLL_CHANNELS, {"_id": int(channel_id)}, {"$inc": {f"stats.{field}": amount}}
        )

    async def stats(self) -> Dict[str, Any]:
        docs = await self.store.find(COLL_CHANNELS, {})
        auto_react = sum(1 for d in docs if (d.get("auto_reactions") or {}).get("enabled"))
        auto_view = sum(1 for d in docs if (d.get("auto_views") or {}).get("enabled"))
        auto_comments = sum(1 for d in docs if (d.get("auto_comments") or {}).get("enabled"))
        posts = sum(int((d.get("stats") or {}).get("posts_seen", 0)) for d in docs)
        return {"total": len(docs), "auto_reactions": auto_react,
                "auto_views": auto_view, "auto_comments": auto_comments,
                "posts_seen": posts}


_channel_service: Optional[ChannelService] = None


def get_channel_service(store=None, settings=None) -> ChannelService:
    global _channel_service
    if _channel_service is None or store is not None or settings is not None:
        _channel_service = ChannelService(store, settings)
    return _channel_service


def reset_channel_service() -> None:
    global _channel_service
    _channel_service = None
