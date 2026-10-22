"""
Auto engagement: react to / view every new post published in a user's channel.

The bot is an admin in the channel, so it receives every channel post. This
service decides what to do with it:

* free tier  — every user gets automatic positive reactions from up to
  ``reactions.free_account_limit`` accounts (10 by default, or fewer if the
  account pool is smaller). No payment involved.
* paid plans — more accounts per post, and/or a fixed number of posts
  (``posts_left`` credits bought from the user panel).
* auto views — same idea for view counts.

Owner/admin can change every number from the admin panel; nothing here is
hard-coded.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, Optional

from telethon import Button

from html import escape as esc

from src.services.channels import ChannelService, get_channel_service
from src.services.engagement import looks_like_invite_problem as _looks_like_invite_problem
from src.services.engagement import EngagementService, OperationResult, get_engagement_service
from src.services.notify import get_notifier
from src.services.orders import OrderService, get_order_service
from src.services.settings import get_settings_service
from src.services.users import UserService, get_user_service

logger = logging.getLogger(__name__)


def _short_error(text: str, limit: int = 160) -> str:
    """Trim an error for a notification without cutting mid-word."""
    text = " ".join(str(text or "").split())
    text = text.replace("(caused by ", "(").replace("SendReactionRequest)", "SendReaction)")
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut + " …"


def _safe_batch_error(errors) -> str:
    """Summarise a batch failure without exposing account/session names."""
    if not errors:
        return "Telegram request failure"
    text = " ".join(str(errors[0] or "").split())
    lowered = text.lower()
    if "no signed-in accounts" in lowered:
        return "no signed-in accounts"
    if "timed out" in lowered:
        return "account timeout"
    if _looks_like_invite_problem([text]):
        return "channel access or invite failure"
    # _run_batch formats errors as ``account-name: ErrorType: details``. Only
    # keep the exception class; the account/session label and server details
    # are not useful in an owner notification.
    tail = text.split(": ", 1)[1] if ": " in text else text
    category = tail.split(": ", 1)[0].strip()
    if category and len(category) <= 48 and category.replace("_", "").isalnum():
        return category
    return "Telegram request failure"


class AutoEngagement:
    def __init__(
        self,
        engagement: Optional[EngagementService] = None,
        channels: Optional[ChannelService] = None,
        users: Optional[UserService] = None,
        settings=None,
        orders: Optional[OrderService] = None,
    ):
        self._engagement = engagement
        self._channels = channels
        self._users = users
        self._settings = settings
        self._orders = orders
        self._no_account_notice: Dict[str, float] = {}
        self._notifier = None

    # -- deps --------------------------------------------------------------
    @property
    def engagement(self):
        if self._engagement is None:
            self._engagement = get_engagement_service()
        return self._engagement

    @property
    def channels(self):
        if self._channels is None:
            self._channels = get_channel_service()
        return self._channels

    @property
    def users(self):
        if self._users is None:
            self._users = get_user_service()
        return self._users

    @property
    def settings(self):
        if self._settings is None:
            self._settings = get_settings_service()
        return self._settings

    @property
    def notifier(self):
        if self._notifier is None:
            self._notifier = get_notifier()
        return self._notifier

    @property
    def orders(self):
        if self._orders is None:
            self._orders = get_order_service()
        return self._orders

    @staticmethod
    def _channel_user_ids(channel: Dict[str, Any]) -> list[int]:
        """Unique, valid users linked to a channel; never inspect unrelated users."""
        values = [channel.get("owner_id"), channel.get("added_by")]
        values.extend(channel.get("sponsors") or [])
        ids: list[int] = []
        for value in values:
            try:
                user_id = int(value)
            except (TypeError, ValueError):
                continue
            if user_id > 0 and user_id not in ids:
                ids.append(user_id)
        return ids

    async def _limits_for_channel(self, channel: Dict[str, Any]) -> list[Dict[str, Any]]:
        """Plan limits of the owner, original adder, and explicit sponsors."""
        limits = []
        for user_id in self._channel_user_ids(channel):
            try:
                limits.append(await self.users.effective_limits(user_id))
            except Exception as exc:  # noqa: BLE001 - automation uses safe defaults
                logger.debug(f"channel limits lookup failed: {type(exc).__name__}")
        return limits

    @staticmethod
    def _channel_limit(limits: list[Dict[str, Any]], key: str, default: int) -> int:
        values = []
        for limits_for_user in limits:
            try:
                values.append(int(limits_for_user.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return max(values, default=int(default))

    # ------------------------------------------------------------------
    async def accounts_for_post(self, channel: Dict[str, Any], kind: str = "reactions") -> int:
        """How many accounts to use for one post.

        Reactions (free tier):
            8-10 random accounts by default, capped by the signed-in pool and by
            the owner's plan (``reaction_accounts``). Every user gets this for
            free — the plan only raises the cap.

        Views (premium only):
            ``views_per_post`` capped by the pool.
        """
        limits = await self._limits_for_channel(channel)
        pool = 0
        try:
            pool = await self.engagement.registry.available_count()
        except Exception as exc:
            logger.debug(f"pool size unavailable: {type(exc).__name__}")

        if kind == "reactions":
            cfg = await self.settings.section("reactions")
            auto_cfg = cfg.get("auto", {}) or {}
            bucket = channel.get("auto_reactions") or {}
            desired = int(bucket.get("accounts") or 0) or int(
                auto_cfg.get("accounts_per_post", cfg.get("free_account_limit", 10))
            )
            free_cap = int(cfg.get("free_account_limit", 10) or 10)
            cap = self._channel_limit(limits, "reaction_accounts", free_cap) or free_cap
            upper = max(1, min(desired, cap, pool or desired))
            floor = int(auto_cfg.get("min_accounts", 8))
            lower = max(1, min(floor, upper))
            return random.randint(lower, upper)

        cfg = await self.settings.section("views")
        bucket = channel.get("auto_views") or {}
        desired = int(bucket.get("views_per_post") or 0) or int(
            (cfg.get("auto") or {}).get("views_per_post", 100)
        )
        cap = self._channel_limit(limits, "auto_view_accounts", desired) or desired
        return max(1, min(desired, cap, pool or desired))

    async def handle_new_post(
        self,
        channel_id: int,
        message_id: int,
        *,
        channel_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Entry point called for every new channel post."""
        channel_id = int(channel_id)
        channel = await self.channels.get(channel_id)
        if channel is None and channel_meta:
            channel = await self.channels.register(
                channel_id,
                channel_meta.get("owner_id"),
                title=channel_meta.get("title"),
                username=channel_meta.get("username"),
                kind=channel_meta.get("kind", "channel"),
                added_by=channel_meta.get("added_by"),
            )
        if channel is None:
            return {"skipped": "channel not registered"}

        # Both the channel-post handler and the generic message handler may see
        # the same update (and Telegram re-sends recent updates after a restart):
        # only the first one may react, otherwise every post got two reactions.
        if not await self.channels.mark_post_seen(channel_id, message_id):
            return {"skipped": "post already handled", "channel_id": channel_id,
                    "message_id": int(message_id)}

        await self.channels.bump_stat(channel_id, "posts_seen")
        outcome: Dict[str, Any] = {"channel_id": channel_id, "message_id": message_id}

        reactions = channel.get("auto_reactions") or {}
        if reactions.get("enabled"):
            outcome["reactions"] = await self._react(channel, message_id, reactions)

        views = channel.get("auto_views") or {}
        if views.get("enabled"):
            # Auto views are a paid service: a channel whose owner has no active
            # plan must not silently spend the pool. The old code switched the
            # flag *off* forever, so the service stayed dead even after a plan
            # was granted later — now it is only skipped, with one clear notice.
            if await self._entitled(channel, "views"):
                outcome["views"] = await self._view(channel, message_id, views)
            else:
                await self._notify_not_entitled(channel, "views")

        comments = channel.get("auto_comments") or {}
        if comments.get("enabled"):
            if await self._entitled(channel, "comments"):
                outcome["comments"] = await self._comment(channel, message_id, comments)
            else:
                await self._notify_not_entitled(channel, "comments")

        return outcome

    async def _entitled(self, channel: Dict[str, Any], feature: str) -> bool:
        """Is *feature* covered for this channel?

        Everybody who manages the channel counts: the recorded owner, the person
        who added the bot and any sponsor a paid order registered. That is what
        makes "one granted plan unlocks everything" true in production — the log
        showed a channel attributed to one id while the paying user had another,
        so the plan of the paying user has to be checked as well.
        """
        candidates = self._channel_user_ids(channel)
        for candidate in candidates:
            try:
                if await self.users.has_feature(candidate, feature):
                    return True
            except Exception as exc:  # noqa: BLE001 - never break automation
                logger.debug(f"{feature} entitlement lookup failed: {type(exc).__name__}")
        if not candidates:
            logger.debug(f"channel {channel.get('_id')} has no linked manager to check {feature} for")
        return False

    async def _views_entitled(self, channel: Dict[str, Any]) -> bool:
        """Kept for compatibility: does anybody behind this channel have views?"""
        return await self._entitled(channel, "views")

    async def _notify_not_entitled(self, channel: Dict[str, Any], feature: str) -> None:
        """Say once why a premium service is not running (instead of killing it)."""
        key = f"{channel.get('_id')}:{feature}:plan"
        import time
        if time.time() - self._no_account_notice.get(key, 0.0) < 1800:
            return
        self._no_account_notice[key] = time.time()
        label = {"views": "auto views", "comments": "auto comments"}.get(feature, feature)
        logger.info(
            f"{label} skipped for {channel.get('_id')} — no active {feature} plan for "
            f"its linked owner/adder/sponsors (the service stays enabled)"
        )
        text = (
            f"🔒 <b>{label.title()} are a premium service</b>\n\n"
            f"📢 {channel.get('title') or channel.get('_id')}\n\n"
            f"No user currently linked to this channel has an active plan that covers "
            f"<b>{feature}</b>, so nothing is being sent for now. The setting stays on.\n\n"
            "Check that the plan includes this service and that its recipient is linked "
            "to this channel as owner, original adder, or sponsor. An operator can correct "
            "the channel assignment in Admin Dashboard → Channels. Once both match, "
            "automation will run on the next new post."
        )
        try:
            recipients = self._channel_user_ids(channel)
            if recipients:
                for recipient in recipients:
                    await self.notifier.to_user(recipient, text)
            else:
                await self.notifier.to_admins(text)
        except Exception as exc:  # noqa: BLE001 - notice only
            logger.debug(f"{feature} entitlement notice failed: {type(exc).__name__}")

    async def _react(self, channel: Dict[str, Any], message_id: int, bucket: Dict[str, Any]) -> Dict[str, Any]:
        accounts = await self.accounts_for_post(channel, "reactions")
        emojis = bucket.get("emojis") or None
        username = channel.get("username") or None
        invite_link = channel.get("invite_link") or await self._ensure_invite(channel, username)
        reaction_cfg = await self.settings.section("reactions")
        auto_cfg = reaction_cfg.get("auto", {}) or {}
        try:
            result = await self.engagement.auto_react(
                channel_id=channel["_id"], message_id=int(message_id),
                account_count=accounts, emojis=emojis,
                invite_link=invite_link,
                # Public channels need no membership at all; private ones join.
                join_if_needed=bool(username is None and auto_cfg.get("join_private_channels", True)),
                username=username,
            )
        except Exception as exc:
            logger.error(f"Auto reactions failed for {channel['_id']}/{message_id}: {exc}")
            return {"ok": 0, "failed": 1, "errors": [str(exc)[:120]]}

        if (result.ok == 0 and username is None
                and _looks_like_invite_problem(result.errors)):
            # The stored link is dead (or was never created). Export a fresh one
            # and give this very post a second chance before giving up.
            fresh = await self._ensure_invite(channel, None, force=True)
            if fresh and fresh != invite_link:
                logger.info(f"Retrying reactions on {channel['_id']}/{message_id} with a fresh invite link")
                try:
                    result = await self.engagement.auto_react(
                        channel_id=channel["_id"], message_id=int(message_id),
                        account_count=accounts, emojis=emojis, invite_link=fresh,
                        join_if_needed=bool(auto_cfg.get("join_private_channels", True)),
                        username=None,
                    )
                except Exception as exc:
                    logger.error(f"Auto reactions retry failed for {channel['_id']}/{message_id}: {exc}")

        await self.channels.bump_stat(channel["_id"], "reactions_sent", result.ok)
        if result.ok == 0:
            # Nothing was applied — say why instead of reporting a silent success.
            await self._notify_no_result(channel, result, accounts, username)
        left = await self.channels.consume_auto_quota(channel["_id"], "reactions")
        if left == 0:
            await self._notify_exhausted(channel, "auto reactions")
        logger.info(
            f"Auto reactions on {channel['_id']}/{message_id}: {result.ok} ok, "
            f"{result.failed} failed of {accounts} accounts "
            f"(credits left: {left})"
            + (f" • {result.errors[0]}" if result.errors and result.ok == 0 else "")
        )
        return {"ok": result.ok, "failed": result.failed, "accounts": accounts,
                "posts_left": left}

    async def _ensure_invite(self, channel: Dict[str, Any], username: Optional[str],
                             force: bool = False):
        """Ask the bot for an invite link when the stored channel has none.

        Private channels are unusable without one: pool accounts cannot join, so
        reactions silently fail. This retries the export on every post until it
        works (permission granted later, temporary error …). ``force=True``
        exports a fresh link even when one is stored — used when the old link
        turned out to be revoked/expired.
        """
        if username:
            return None
        if not force and channel.get("invite_link"):
            return channel["invite_link"]
        try:
            from src.runtime import get_bot
            bot = get_bot()
            exporter = getattr(bot, "_export_invite", None)
            if exporter is None:
                return None
            link = await exporter(channel["_id"])
        except Exception as exc:
            logger.debug(f"invite export failed for {channel['_id']}: {exc}")
            return None
        if link:
            await self.channels.register(channel["_id"], invite_link=link)
            channel["invite_link"] = link
            logger.info(f"Invite link ready for {channel['_id']}")
        return link


    async def _comment(self, channel: Dict[str, Any], message_id: int,
                       bucket: Dict[str, Any]) -> Dict[str, Any]:
        """Post random positive comments on a new post (premium, plan based)."""
        cfg = await self.settings.section("comments")
        auto_cfg = cfg.get("auto", {}) or {}
        texts = [t.strip() for t in (bucket.get("texts") or auto_cfg.get("texts") or []) if str(t).strip()]
        if not texts:
            from src.services.settings import DEFAULT_POSITIVE_COMMENTS
            texts = list(DEFAULT_POSITIVE_COMMENTS)
        limits = await self._limits_for_channel(channel)
        pool = 0
        try:
            pool = await self.engagement.registry.available_count()
        except Exception:
            pool = 0
        desired = int(bucket.get("accounts") or auto_cfg.get("accounts_per_post", 3) or 3)
        default_cap = int(cfg.get("max_accounts", 10) or 10)
        cap = self._channel_limit(limits, "comment_accounts", default_cap) or default_cap
        accounts = max(1, min(desired, cap, pool or desired))
        username = channel.get("username") or None
        invite_link = channel.get("invite_link") or await self._ensure_invite(channel, username)
        try:
            result = await self.engagement.auto_comments(
                channel_id=channel["_id"], message_id=int(message_id), texts=texts,
                account_count=accounts, username=username,
                invite_link=None if username else invite_link,
            )
        except Exception as exc:
            logger.error(f"Auto comments failed for {channel['_id']}/{message_id}: {exc}")
            return {"ok": 0, "failed": 1, "errors": [str(exc)[:120]]}

        await self.channels.bump_stat(channel["_id"], "comments_sent", result.ok)
        logger.info(
            f"Auto comments on {channel['_id']}/{message_id}: {result.ok} ok, "
            f"{result.failed} failed of {accounts} accounts"
            + (f" • {result.errors[0]}" if result.ok == 0 and result.errors else "")
        )
        if result.ok == 0 and result.failed:
            await self._notify_no_result(channel, result, accounts, username)
        return {"ok": result.ok, "failed": result.failed, "accounts": accounts}

    async def _view(self, channel: Dict[str, Any], message_id: int, bucket: Dict[str, Any]) -> Dict[str, Any]:
        per_post = await self.accounts_for_post(channel, "views")
        username = channel.get("username") or None
        invite_link = channel.get("invite_link") or await self._ensure_invite(channel, username)
        try:
            result = await self.engagement.auto_views(
                channel_id=channel["_id"], message_id=int(message_id),
                views_per_post=per_post, invite_link=invite_link, username=username,
            )
        except Exception as exc:
            logger.error(
                f"Auto views failed for {channel['_id']}/{message_id}: {type(exc).__name__}"
            )
            result = OperationResult(failed=1, errors=[type(exc).__name__])

        # For private channels, a revoked/expired invite is recoverable. Export a
        # fresh one and retry the post once, matching the reaction path.
        if (result.ok == 0 and username is None
                and _looks_like_invite_problem(result.errors)):
            fresh = await self._ensure_invite(channel, None, force=True)
            if fresh and fresh != invite_link:
                logger.info(f"Retrying auto views on {channel['_id']}/{message_id} with a fresh invite")
                try:
                    result = await self.engagement.auto_views(
                        channel_id=channel["_id"], message_id=int(message_id),
                        views_per_post=per_post, invite_link=fresh, username=None,
                    )
                except Exception as exc:
                    logger.error(
                        f"Auto views retry failed for {channel['_id']}/{message_id}: "
                        f"{type(exc).__name__}"
                    )
                    result = OperationResult(failed=1, errors=[type(exc).__name__])

        await self.channels.bump_stat(channel["_id"], "views_sent", result.ok)
        previous_left = int(bucket.get("posts_left", 0) or 0)
        left = previous_left
        if result.ok > 0:
            left = await self.channels.consume_auto_quota(channel["_id"], "views")
        details = result.details or {}
        requested = int(details.get("requested") or per_post)
        used = int(details.get("accounts_used", per_post) or 0)
        summary = (
            f"Auto view requests on {channel['_id']}/{message_id}: "
            f"{result.ok} accepted, {result.failed} failed of {used} accounts used "
            f"({requested} requested)"
        )
        if result.ok:
            logger.info(f"{summary} (posts left: {left})")
        else:
            logger.warning(
                f"{summary} (reason: {_safe_batch_error(result.errors)})"
            )
            await self._notify_no_result(
                channel, result, used, username, kind="views"
            )
        # A zero credit balance is the unlimited sentinel unless it just reached
        # zero from a finite positive subscription. Failed delivery never spends
        # a post credit.
        if result.ok > 0 and previous_left > 0 and left == 0:
            await self._notify_exhausted(channel, "auto views")
        return {
            "ok": result.ok, "failed": result.failed, "accounts": requested,
            "accounts_used": used, "posts_left": left,
        }

    async def _notify_no_result(self, channel: Dict[str, Any], result, accounts: int,
                                username: Optional[str], kind: str = "reactions") -> None:
        """Explain why a post got no reactions/views/comments (pool empty / access)."""
        if accounts <= 0:
            await self._notify_no_accounts(channel, kind)
            return
        errors = list(getattr(result, "errors", None) or [])
        raw_reason = errors[0] if errors else ""
        if "no signed-in accounts available" in str(raw_reason).lower():
            await self._notify_no_accounts(channel, kind)
            return
        key = f"{channel['_id']}:{kind}:failure"
        import time
        last = self._no_account_notice.get(key, 0.0)
        if time.time() - last < 1800:
            return
        self._no_account_notice[key] = time.time()
        label = {
            "reactions": "auto reactions", "comments": "auto comments",
            "views": "auto views",
        }.get(kind, kind)
        reason_text = _safe_batch_error(errors) if kind == "views" else str(raw_reason or "unknown")
        logger.warning(
            f"Auto {label} produced nothing on {channel['_id']} "
            f"({len(errors)} error(s)): {reason_text[:80]}"
        )
        advice = self._advice_for(reason_text, username, channel, kind=kind)
        text = (
            f"⚠️ <b>{label.capitalize()} could not be applied</b>\n\n"
            f"📢 {channel.get('title') or channel['_id']}\n"
            f"👥 accounts picked: <b>{accounts}</b>\n"
            f"📝 reason: <code>{esc(_short_error(reason_text))}</code>\n\n"
            f"💡 {advice}"
        )
        await self._send_owner(channel, text, "u:ch")

    @staticmethod
    def _advice_for(reason: str, username: Optional[str], channel: Dict[str, Any],
                    *, kind: str = "reactions") -> str:
        """What should the owner actually do about this failure?"""
        lowered = (reason or "").lower()
        if kind == "views":
            if "timeout" in lowered or "flood" in lowered:
                return "Telegram slowed or timed out a view account. This post's view credit was preserved; the next post will retry."
            if _looks_like_invite_problem([reason]):
                if username:
                    return "The accounts could not resolve this public channel. Check its username and that the post is still available."
                return "The accounts could not access this private channel. Keep a valid invite link and grant the bot Invite Users permission so accounts can join."
            return "Telegram refused the view request. Check that the post still exists, the channel is accessible to the accounts, and no account is restricted."
        if "invalid reaction" in lowered or "reactioninvalid" in lowered:
            return (
                "The channel refused the emoji — Telegram only allows its own reaction "
                "set. Unsupported emojis are now skipped automatically; review the list "
                "in <b>Admin panel → ❤️ Reactions → Emojis</b>."
            )
        if "flood" in lowered:
            return "Telegram asked us to slow down. The next post will be reacted to again."
        if "deactivated" in lowered or "revoked" in lowered or "unauthorized" in lowered:
            return "One of the signed-in accounts is no longer valid — remove it in Admin panel → 👤 Accounts."
        if "approv" in lowered:
            return (
                "This channel approves members manually. Approve the pending join "
                "requests (or turn on auto-approve) so the accounts can react."
            )
        if _looks_like_invite_problem([reason]):
            if username:
                return "The accounts could not resolve this public channel — check that it still exists and allow reactions in the channel settings."
            return (
                "This is a private channel: the bot needs <b>Invite Users</b> permission "
                "so the accounts can join. Re-add me with all permissions, or send me a "
                "fresh invite link."
            )
        return (
            "Nothing was applied. The channel may have reactions disabled — open the "
            "channel settings and allow reactions, or send me a fresh post link."
        )

    async def _send_owner(self, channel: Dict[str, Any], text: str, back: str) -> None:
        try:
            buttons = [[Button.inline("📋 My Channels", back)]]
        except Exception:  # pragma: no cover - defensive
            buttons = None
        try:
            recipients = self._channel_user_ids(channel)
            if recipients:
                for recipient in recipients:
                    await self.notifier.to_user(recipient, text, buttons=buttons)
            else:
                await self.notifier.to_admins(text, buttons=buttons)
        except Exception as exc:
            logger.debug(f"notification failed: {type(exc).__name__}")

    async def warm_channel(self, channel_id: int, *, count: Optional[int] = None) -> Dict[str, Any]:
        """Join pool accounts into a private channel right after the bot is added.

        Public channels are skipped (no membership needed to react). This means
        the very first post already gets its reactions.
        """
        channel = await self.channels.get(channel_id)
        if channel is None:
            return {"skipped": "unknown channel"}
        if channel.get("username"):
            return {"skipped": "public channel"}
        invite_link = channel.get("invite_link") or await self._ensure_invite(channel, None)
        if not invite_link:
            return {"skipped": "no invite link yet"}
        # Join the *whole* allowed pool (free cap, or the owner's plan): each post
        # then picks a random slice of it. Using the per-post pick here would
        # leave accounts outside and the pool would never fill up.
        cap = await self._reaction_cap(channel)
        accounts = max(1, min(int(count or cap), cap))
        try:
            result = await self.engagement.ensure_members(
                channel_id=channel_id, account_count=accounts, invite_link=invite_link,
            )
        except Exception as exc:
            logger.warning(f"member warm-up failed for {channel_id}: {exc}")
            return {"ok": 0, "failed": 1, "error": str(exc)[:120]}

        # A revoked/expired link is a common cause: export a brand new one and
        # try again, so the first post still gets its reactions.
        if result.ok == 0 and result.failed and _looks_like_invite_problem(result.errors):
            fresh = await self._ensure_invite(channel, None, force=True)
            if fresh and fresh != invite_link:
                logger.info(f"Retrying the warm-up for {channel_id} with a fresh invite link")
                retry = await self.engagement.ensure_members(
                    channel_id=channel_id, account_count=accounts, invite_link=fresh,
                )
                result = retry

        logger.info(
            f"Member warm-up for {channel_id}: {result.ok} joined, {result.failed} failed"
            + (f" • {result.errors[0]}" if result.ok == 0 and result.errors else "")
        )
        return {"ok": result.ok, "failed": result.failed, "accounts": accounts,
                "errors": result.errors[:2]}

    # ------------------------------------------------------------------
    # pool lifecycle: new accounts join, removed bots leave
    # ------------------------------------------------------------------
    async def _reaction_cap(self, channel: Dict[str, Any]) -> int:
        """How many accounts may join (free cap or the highest linked plan)."""
        try:
            cfg = await self.settings.section("reactions")
        except Exception:
            cfg = {}
        free_cap = int(cfg.get("free_account_limit", 10) or 10)
        try:
            limits = await self._limits_for_channel(channel)
            cap = self._channel_limit(limits, "reaction_accounts", free_cap) or free_cap
        except Exception as exc:  # noqa: BLE001 - fall back to the free cap
            logger.debug(f"reaction cap lookup failed: {type(exc).__name__}")
            cap = free_cap
        pool = 0
        try:
            pool = await self.engagement.registry.available_count()
        except Exception:
            pool = 0
        return max(1, min(cap, pool or cap))

    async def warm_all_channels(self, *, reason: str = "new account",
                                notify: bool = False) -> Dict[str, Any]:
        """Join the pool into every channel that needs members.

        Called after a new account signs in: the channels added earlier only had
        the old pool, so the newcomer must join them too — otherwise the free
        8-10 account tier is never reached on those channels.
        """
        try:
            channels = await self.channels.list_all(limit=200)
        except Exception as exc:  # noqa: BLE001 - never break the sign-in flow
            logger.warning(f"warm_all_channels could not list channels: {exc}")
            return {"channels": 0, "joined": 0, "failed": 0, "errors": [str(exc)[:120]]}

        total_joined = total_failed = 0
        touched: List[Dict[str, Any]] = []
        for channel in channels:
            bucket = channel.get("auto_reactions") or {}
            views = channel.get("auto_views") or {}
            if not bucket.get("enabled") and not views.get("enabled"):
                continue                        # nothing automated here
            if channel.get("username"):
                continue                        # public: no membership needed
            cap = await self._reaction_cap(channel)
            outcome = await self.warm_channel(channel["_id"], count=cap)
            if outcome.get("skipped"):
                continue
            total_joined += int(outcome.get("ok") or 0)
            total_failed += int(outcome.get("failed") or 0)
            if outcome.get("ok"):
                touched.append({"channel_id": channel["_id"],
                                "title": channel.get("title"),
                                "joined": int(outcome.get("ok") or 0)})
        if total_joined:
            logger.info(
                f"Account warm-up ({reason}): {total_joined} new membership(s) across "
                f"{len(touched)} channel(s), {total_failed} failed"
            )
        if total_joined:
            await self._notify_warmup(touched, total_joined)
        return {"channels": len(touched), "joined": total_joined,
                "failed": total_failed, "details": touched}

    async def _notify_warmup(self, touched: List[Dict[str, Any]], joined: int) -> None:
        """Record the auto-join result in the **logs only**.

        The operator asked for the join successes to stop appearing in the
        Telegram chat: they are bookkeeping, not something he has to act on.
        ``notify=True`` is still honoured for anyone who wants the old behaviour.
        """
        logger.info(
            f"Auto-join finished: {joined} membership(s) in {len(touched)} channel(s) "
            f"[{', '.join(str(item.get('title') or item.get('channel_id')) for item in touched[:8])}]"
        )

    async def leave_channel(self, channel_id: int, *,
                            channel: Optional[Dict[str, Any]] = None,
                            notify: bool = True) -> Dict[str, Any]:
        """Remove the channel and make every pool account leave it.

        The owner removing the bot means "stop": the accounts that joined for
        reactions (or that a plan put there) must not stay in their channel.
        """
        channel_id = int(channel_id)
        doc = channel or await self.channels.get(channel_id) or {}
        username = doc.get("username") or None
        invite_link = doc.get("invite_link") or None

        outcome: Dict[str, Any] = {"left": 0, "failed": 0, "skipped": 0}
        if not username:
            try:
                result = await self.engagement.leave_channel(
                    channel_id=channel_id, username=username,
                    invite_link=invite_link,
                    reason="bot removed from the channel",
                )
                outcome = {"left": result.ok, "failed": result.failed,
                           "skipped": result.skipped, "errors": result.errors[:2]}
            except Exception as exc:  # noqa: BLE001 - removal must still happen
                logger.warning(f"accounts could not leave {channel_id}: {exc}")
                outcome = {"left": 0, "failed": 1, "errors": [str(exc)[:120]]}

        # The bot itself was removed from the chat: that is Telegram's own
        # event, not a user gaming the free allowance, so the record goes
        # even when the channel is locked.
        await self.channels.remove(channel_id, force=True)
        logger.info(
            f"Channel {channel_id} removed — {outcome['left']} account(s) left it, "
            f"{outcome.get('failed', 0)} could not"
        )
        if notify and (outcome["left"] or outcome.get("failed")):
            await self._notify_left(doc, outcome)
        return outcome

    async def _notify_left(self, channel: Dict[str, Any], outcome: Dict[str, Any]) -> None:
        text = (
            "🚪 <b>The bot was removed from a channel</b>\n\n"
            f"📢 {channel.get('title') or channel.get('_id')}\n"
            f"👥 accounts that left: <b>{outcome.get('left', 0)}</b>\n"
        )
        if outcome.get("failed"):
            text += f"⚠️ could not leave: {outcome['failed']} (they will retry on the next removal)\n"
        text += "\nAutomatic reactions are off for this channel."
        owner_id = channel.get("owner_id")
        try:
            if owner_id:
                await self.notifier.to_user(int(owner_id), text)
            else:
                await self.notifier.to_admins(text)
        except Exception as exc:  # noqa: BLE001 - notice only
            logger.debug(f"leave notice failed: {exc}")

    async def _notify_no_accounts(self, channel: Dict[str, Any], kind: str) -> None:
        """Tell the channel owner once that automation has no accounts to use."""
        key = f"{channel['_id']}:{kind}"
        last = self._no_account_notice.get(key, 0.0)
        import time
        if time.time() - last < 1800:      # at most one reminder per 30 minutes
            return
        self._no_account_notice[key] = time.time()
        logger.warning(
            f"Automation for {channel['_id']} ran with an empty account pool "
            f"({kind}) — nobody is signed in yet"
        )
        label = {
            "reactions": "auto reactions", "comments": "auto comments",
            "views": "auto views",
        }.get(kind, kind)
        text = (
            f"⚠️ <b>{label.capitalize()} could not run</b>\n\n"
            f"📢 {channel.get('title') or channel['_id']}\n\n"
            "No accounts are signed in or available right now, so nothing was sent.\n"
            "Add or restore accounts from the panel (Account Management) — "
            f"{label} can then run on the next new post."
        )
        try:
            buttons = [[Button.inline("👤 Account Info", "u:ai")]]
        except Exception:  # pragma: no cover - defensive
            buttons = None
        try:
            recipients = self._channel_user_ids(channel)
            if recipients:
                for recipient in recipients:
                    await self.notifier.to_user(recipient, text, buttons=buttons)
            else:
                await self.notifier.to_admins(text, buttons=buttons)
        except Exception as exc:
            logger.debug(f"No-accounts notice failed: {type(exc).__name__}")

    async def _notify_exhausted(self, channel: Dict[str, Any], what: str) -> None:
        recipients = self._channel_user_ids(channel)
        if not recipients:
            return
        try:
            text = (
                f"⏳ Your {what} plan has run out for "
                f"<b>{channel.get('title', channel['_id'])}</b>.\n\n"
                "Renew it from the panel to continue automatic service."
            )
            for recipient in recipients:
                await self.notifier.to_user(recipient, text)
        except Exception as exc:
            logger.debug(f"exhausted notification failed: {type(exc).__name__}")

    async def enable_free_plan(self, channel_id: int) -> Dict[str, Any]:
        """Start automation after the bot is added to a channel/group.

        Auto reactions are **free for everyone** (that is the whole free tier).
        Auto views are a premium feature, so they only switch on for a user who
        actually has the ``views`` capability — otherwise the panel would be
        offering something the user cannot use.
        """
        outcome: Dict[str, Any] = {}
        cfg = await self.settings.section("reactions")
        channel_cfg = await self.settings.section("channels")
        auto_cfg = cfg.get("auto", {}) or {}
        channel = await self.channels.get(channel_id) or {}
        owner_id = int(channel.get("owner_id") or 0)
        if auto_cfg.get("enabled", True):
            # Free auto reactions are limited to the free channel allowance; a
            # user who wants more channels buys/requests a channel plan instead.
            allowance = {"allowed": True}
            if owner_id:
                try:
                    allowance = await self.channels.can_enable_auto(
                        channel_id, owner_id, self.users)
                except Exception as exc:  # noqa: BLE001 - never block the add flow
                    logger.debug(f"channel allowance check failed: {exc}")
            if channel_cfg.get("auto_enable", True) and allowance.get("allowed", True):
                await self.channels.set_auto_reactions(channel_id, True, posts_left=-1)
                outcome["reactions"] = "on (free)"
            else:
                slots = allowance.get("slots") or {}
                outcome["reactions"] = "needs a channel plan"
                logger.info(
                    f"Auto reactions not enabled for {channel_id}: "
                    f"{allowance.get('reason') or 'auto start disabled'} "
                    f"(slots {slots.get('used')}/{slots.get('total')})"
                )
                await self._notify_channel_limit(channel, owner_id, slots)
        try:
            views_cfg = await self.settings.section("views")
            entitled = (await self._entitled(channel, "views")
                        and (views_cfg.get("auto") or {}).get("enabled", True))
            if entitled:
                await self.channels.set_auto_views(channel_id, True, posts_left=-1)
                outcome["views"] = "on (premium)"
            else:
                # Nothing to switch off: a linked plan can be granted later and
                # the service starts on the next post once that user is matched.
                outcome["views"] = "premium only"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"auto-view switch failed for {channel_id}: {type(exc).__name__}")
        try:
            comments_cfg = await self.settings.section("comments")
            if ((comments_cfg.get("auto") or {}).get("enabled", True)
                    and await self._entitled(channel, "comments")):
                await self.channels.set_auto_comments(channel_id, True)
                outcome["comments"] = "on (premium)"
            elif (channel.get("auto_comments") or {}).get("enabled"):
                outcome["comments"] = "premium only"
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"auto-comment switch failed for {channel_id}: {type(exc).__name__}")
        # Private channel? get the accounts inside now so post #1 is covered.
        try:
            outcome["warmup"] = await self.warm_channel(channel_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"warm-up skipped for {channel_id}: {exc}")
        return outcome


    async def _notify_channel_limit(self, channel: Dict[str, Any], owner_id: int,
                                    slots: Dict[str, Any]) -> None:
        """Tell the owner their free channel allowance is used up."""
        if not owner_id:
            return
        try:
            from src.services.notify import get_notifier

            await get_notifier().to_user(
                int(owner_id),
                "📡 <b>Channel limit reached</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"🏷 Channel: <b>{channel.get('title') or channel.get('_id')}</b>\n"
                f"🎁 Free channels with auto reactions: <b>{slots.get('free', slots.get('total', 2))}</b>"
                f" • in use: <b>{slots.get('used', '?')}/{slots.get('total', '?')}</b>\n\n"
                "Auto reactions are free on your first channels only. This channel is "
                "registered and safe — to run reactions on it, request an extra-channel "
                "plan and the owner will activate it.\n\n"
                "👉 Open the bot ▸ <b>👑 Buy Premium ▸ 📡 Only extra channels?</b>",
            )
        except Exception as exc:  # noqa: BLE001 - notice only
            logger.debug(f"channel-limit notice failed: {exc}")

    # ------------------------------------------------------------------
    # plan hooks
    # ------------------------------------------------------------------
    async def refresh_automation_for_user(self, user_id: int) -> Dict[str, Any]:
        """Re-check every channel of *user_id* after a plan was granted.

        This closes the loop on "I granted the plan but nothing runs": auto views
        and auto comments are switched on for the channels the user owns, added
        or sponsored, and the owner gets one short message.
        """
        user_id = int(user_id)
        outcome: Dict[str, Any] = {"channels": 0, "views": 0, "comments": 0}
        try:
            channels = await self.channels.list_all(limit=500)
        except Exception as exc:  # noqa: BLE001 - hook must never break a grant
            logger.warning(f"plan hook could not list channels: {exc}")
            return outcome
        views_cfg = await self.settings.section("views")
        comments_cfg = await self.settings.section("comments")
        for channel in channels:
            if user_id not in self._channel_user_ids(channel):
                continue
            outcome["channels"] += 1
            try:
                if ((views_cfg.get("auto") or {}).get("enabled", True)
                        and await self._entitled(channel, "views")):
                    if not (channel.get("auto_views") or {}).get("enabled"):
                        await self.channels.set_auto_views(channel["_id"], True, posts_left=-1)
                    outcome["views"] += 1
                if ((comments_cfg.get("auto") or {}).get("enabled", True)
                        and await self._entitled(channel, "comments")):
                    if not (channel.get("auto_comments") or {}).get("enabled"):
                        await self.channels.set_auto_comments(channel["_id"], True)
                    outcome["comments"] += 1
            except Exception as exc:  # noqa: BLE001 - one channel must not stop the rest
                logger.debug(f"plan hook failed for {channel.get('_id')}: {exc}")
        if outcome["channels"]:
            logger.info(
                f"Plan hook: {outcome['views']} linked channel(s) with auto views, "
                f"{outcome['comments']} with auto comments"
            )
        else:
            logger.info(
                "Plan hook: no channel is linked to this plan holder; automation settings "
                "were not changed. Check the owner/adder/sponsor assignment."
            )
        return outcome


_auto: Optional[AutoEngagement] = None


def get_auto_engagement(engagement=None, channels=None, users=None, settings=None,
                        orders=None) -> AutoEngagement:
    """Process-wide auto-engagement service. Passing any dependency rebuilds it."""
    global _auto
    deps = (engagement, channels, users, settings, orders)
    if _auto is None or any(d is not None for d in deps):
        _auto = AutoEngagement(engagement, channels, users, settings, orders)
    return _auto


def reset_auto_engagement() -> None:
    global _auto
    _auto = None
