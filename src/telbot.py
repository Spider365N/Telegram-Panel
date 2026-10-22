import logging
import time
from typing import List, Optional
from telethon import TelegramClient, events, Button
from telethon.errors import FloodWaitError
import asyncio
from src.config import API_ID, API_HASH, BOT_TOKEN, ADMIN_ID, ADMIN_IDS, OWNER_ID
from src.config import ConfigManager, validate_env_file
from src.logger import setup_logging
from src.handlers import MessageHandler, CallbackHandler, CommandHandler, AccountHandler
from src.client import SessionManager
from src.monitor import Monitor
from src import config as app_config
from src.commands import register_commands
from src.panels import get_panel_router
from src.runtime import set_bot
from src.services.accounts import get_account_registry
from src.services.autoengage import get_auto_engagement
from src.services.channels import get_channel_service

setup_logging()
logger = logging.getLogger(__name__)

def _credentials_hint(exc: BaseException) -> "Optional[str]":
    """Return an actionable message for credential errors, else None."""
    name = type(exc).__name__
    if name == "ApiIdInvalidError":
        return ("API_ID / API_HASH were rejected by Telegram — check them against "
                "https://my.telegram.org/apps and restart.")
    if name in ("AuthKeyUnregisteredError", "AuthKeyDuplicatedError",
                "UserDeactivatedBanError", "AccessTokenInvalidError"):
        return ("BOT_TOKEN was rejected by Telegram — create a new token with "
                "@BotFather and update .env, then restart.")
    message = str(exc)
    if "two different IP addresses" in message:
        return ("Another instance is using the same session file. Stop the duplicate "
                "process (or use a different BOT_SESSION_NAME) and restart.")
    return None


class TelegramBot:
    def __init__(self):
        """
        Initialize the TelegramBot class with necessary components.
        """
        try:
            self.config_manager = ConfigManager('clients.json')
            self.config = self.config_manager.load_config()
            self.tbot = None
            self.active_clients = {}
            self.active_clients_lock = None
            self.handlers = {}
            self._conversations = {}
            self._conversations_lock = None
            self.client_manager = None
            self.account_handler = None
            self.monitor = None
            self._handlers_registered = False

            # New stack (storage, panels, engagement) — wired in start()
            self.store = None
            self.panel_router = None
            self.account_registry = None
            self.health_server = None
            self.services_ready = False
            self._pool_supervisor_task = None
            self.started_at = time.time()   # used by the web admin dashboard

            logger.info("Bot initialized successfully")
        except Exception as e:
            logger.critical(f"Error during bot initialization: {e}")
            raise

    async def start(self):
        """
        Start the bot and initialize all components.
        """
        try:
            # Validate environment configuration before starting. A config error
            # can never be fixed by retrying, so it stops the process (the
            # platform's restart policy then shows the same clear message).
            try:
                validate_env_file()
            except ValueError as e:
                logger.critical(str(e))
                raise SystemExit(1) from None

            # Storage + services must be up before anything reads state
            await self._init_stack()
            
            # Create TelegramClient now that we're in an async context
            if self.tbot is None or not self.tbot.is_connected():
                if self.tbot is None:
                    self.tbot = TelegramClient('bot2', API_ID, API_HASH)
                    logger.info("TelegramClient created")
                elif not self.tbot.is_connected():
                    logger.info("Reconnecting TelegramClient...")
            
            # Initialize async components now that we're in an async context
            if self.active_clients_lock is None:
                self.active_clients_lock = asyncio.Lock()
            if self._conversations_lock is None:
                self._conversations_lock = asyncio.Lock()
            
            # Initialize managers now that we have locks and client
            if self.client_manager is None:
                # SessionManager needs the bot wrapper (for active_clients_lock,
                # monitor, config_manager and the .tbot client), not the raw client.
                self.client_manager = SessionManager(self.config, self.active_clients, self)
            if self.account_handler is None:
                self.account_handler = AccountHandler(self)
            if self.monitor is None:
                self.monitor = Monitor(self)
            
            logger.info("Starting bot connection...")
            try:
                await self.tbot.start(bot_token=BOT_TOKEN)
                logger.info("Bot connected to Telegram")
            except FloodWaitError as e:
                wait_time = e.seconds
                logger.warning(f"FloodWaitError: Telegram requires waiting {wait_time} seconds ({wait_time/60:.1f} minutes) before bot can start.")
                logger.info(f"Waiting {wait_time} seconds...")
                await asyncio.sleep(wait_time)
                logger.info("Retrying bot connection...")
                await self.tbot.start(bot_token=BOT_TOKEN)
                logger.info("Bot connected to Telegram after waiting")
            
            await self.init_handlers()
            logger.info("Handlers initialized")

            # Make sure Telegram shows the up-to-date command menu
            try:
                await register_commands(
                    self.tbot,
                    owner_id=getattr(app_config, "OWNER_ID", OWNER_ID),
                    admin_ids=getattr(app_config, "ADMIN_IDS", ADMIN_IDS),
                )
            except Exception as e:
                logger.warning(f"Command registration skipped: {e}")

            await self.client_manager.start_saved_clients()
            logger.info("Saved clients started")

            # Optional HTTP status endpoint (Railway/Render/Fly set $PORT).
            await self._start_health_server()

            # Accounts: mirror live clients into the database, then rebuild any
            # account that exists in the database but not in memory (crash-safe).
            try:
                if self.account_registry is not None:
                    await self.account_registry.sync_from_live_clients()
                    restored = await self.account_registry.restore_missing_clients()
                    stats = await self.account_registry.stats()
                    logger.info(
                        f"Account pool — total={stats['total']} live={stats.get('live', 0)} "
                        f"revoked={stats['revoked']} restored_this_boot={restored}"
                    )
            except Exception as e:
                logger.error(f"Account recovery failed: {e}", exc_info=True)

            # The pool may be bigger (or brand new) since the channels were added:
            # put every account that is allowed to react into every channel, so the
            # free 8-10 account tier is actually available on already-added ones.
            # Background task: joining uses human-like delays and must never hold
            # up the boot (the admin notification above is sent immediately).
            try:
                asyncio.create_task(self._warm_channels_after_boot())
            except Exception as e:  # noqa: BLE001 - boot must continue
                logger.warning(f"Channel warm-up after boot could not start: {e}")

            # Self-healing pool: every minute, reconnect anything that dropped
            # and rebuild database-only accounts. Accounts are only dropped on a
            # ban/block/revoked session — never because of a connection error.
            try:
                if self.account_registry is not None:
                    self._pool_supervisor_task = asyncio.create_task(
                        self._pool_supervisor_loop()
                    )
            except Exception as e:  # noqa: BLE001 - boot must continue
                logger.warning(f"Pool supervisor could not start: {e}")

            try:
                pool = await self.account_registry.stats() if self.account_registry else {}
                sent = await self.notify_admin(
                    "✅ <b>Panel is online</b>\n\n"
                    f"👤 Accounts: {pool.get('live', 0)} live / {pool.get('total', 0)} total\n"
                    f"🗄 Storage: {getattr(self.store, 'name', 'n/a')}\n\n"
                    "Send /start — users get the service panel, you get the admin panel."
                )
                if sent:
                    logger.info("Admin notification sent")
                else:
                    logger.info("Admin notification not delivered (admin may need to /start the bot first)")
            except Exception as e:
                logger.warning(f"Failed to send admin notification: {e}")
            
            logger.info("Bot started successfully")
        except Exception as e:
            logger.error(f"Error during bot start: {e}", exc_info=True)
            raise

    async def init_handlers(self):
        """
        Initialize all event handlers for the bot.
        """
        # The bot client is reused across reconnections, so registering again
        # would stack duplicate handlers and process every event multiple times.
        if self._handlers_registered:
            logger.info("Handlers already registered, skipping re-registration")
            return
        try:
            # /start now routes everyone: admins → admin panel, users → user panel
            self.tbot.add_event_handler(self._start_router, events.NewMessage(pattern=r'^/start(@\w+)?$'))
            logger.info("Registered /start router (admin + user panels)")

            # Panel commands (/panel /menu /views /orders /admin ...)
            self.tbot.add_event_handler(self._panel_command, events.NewMessage(pattern=r'^/[a-zA-Z]+(@\w+)?$'))

            # Register callback handler for button clicks (routes panel callbacks first)
            callback_handler = CallbackHandler(self)
            self.tbot.add_event_handler(
                callback_handler.callback_handler,
                events.CallbackQuery()
            )
            logger.info("Registered callback query handler")

            # Bot added to / removed from a channel or group
            self.tbot.add_event_handler(self._chat_action_handler, events.ChatAction())
            logger.info("Registered chat action handler (add-to-channel/group)")

            # New channel posts → auto reactions / auto views
            self.tbot.add_event_handler(
                self._channel_post_handler,
                events.NewMessage(func=lambda e: bool(
                    getattr(e, 'is_channel', False)
                    and getattr(getattr(e, 'message', None), 'post', False)
                )),
            )
            logger.info("Registered channel post handler (auto engagement)")

            # Register generic message handler last (for conversation states)
            # This should NOT catch /start because it's already handled above
            message_handler = MessageHandler(self)
            self.tbot.add_event_handler(
                message_handler.message_handler,
                events.NewMessage()
            )
            logger.info("Registered generic message handler")

            self._handlers_registered = True
            logger.info("Handlers initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing handlers: {e}")
            raise

    # ------------------------------------------------------------------
    # New stack: storage, services, panels
    # ------------------------------------------------------------------
    async def _init_stack(self) -> None:
        """Bring up persistence and the service layer (idempotent)."""
        if not self.services_ready:
            from src.db.store import init_store
            set_bot(self)
            try:
                self.store = await init_store()
            except Exception as exc:
                logger.critical(f"Could not initialise storage: {exc}", exc_info=True)
                raise
            self.account_registry = get_account_registry().bind(self)
            self.panel_router = get_panel_router(self)
            # The notifier must always talk through *this* bot client, otherwise
            # a restart or a second instance would keep sending into a stale one.
            try:
                from src.services.notify import get_notifier
                if self.tbot is not None:
                    get_notifier().bind(self.tbot)
            except Exception as exc:
                logger.debug(f"Notifier bind skipped: {exc}")
            # A stored emoji list may contain something Telegram refuses (the
            # production "✅" case) — clean it before the first post needs it.
            try:
                from src.services.settings import get_settings_service
                await get_settings_service().repair_reaction_emojis()
            except Exception as exc:  # noqa: BLE001 - never block the boot
                logger.debug(f"emoji list repair skipped: {exc}")
            self.services_ready = True
            logger.info(
                f"Service stack ready — storage={self.store.name}, "
                f"panels={'on' if self.panel_router else 'off'}"
            )

    async def _start_router(self, event):
        """Route /start: admins land in the admin panel, everyone else in the user panel."""
        await self._init_stack()
        if self.panel_router is None:
            await event.respond("Panel is not available right now. Please try again shortly.")
            return
        await self.panel_router.start_for(event)

    async def _panel_command(self, event):
        """Handle the additional commands advertised in the bot menu."""
        raw = (getattr(event, "raw_text", "") or "").strip()
        command = raw.split()[0].lstrip("/").split("@")[0].lower() if raw else ""
        if command in ("start",):
            return  # handled by the /start router
        await self._init_stack()
        router = self.panel_router
        if router is None:
            await event.respond("Panel is not available right now.")
            return
        user_id = int(getattr(event, "sender_id", 0) or 0)
        is_admin = await router.users.is_admin(user_id)
        try:
            if command in ("panel", "menu", "help"):
                if command == "help":
                    await router.user_panel.show_home(event)
                    return
                await router.start_for(event)
                return
            if command in ("admin", "stats", "pricing", "limits", "users", "aorders",
                           "topups", "channels", "accounts", "broadcast", "settings", "sendpv"):
                if not is_admin:
                    await router.user_panel.show_home(event)
                    return
                await router.admin_panel.dispatch({
                    "admin": "home", "stats": "home", "pricing": "pricing",
                    "limits": "limits", "users": "users", "aorders": "orders",
                    "topups": "topups", "channels": "channels", "accounts": "accounts",
                    "broadcast": "broadcast", "settings": "settings", "sendpv": "sendpv",
                }.get(command, "home"), [], event)
                return
            if command in ("grant", "revoke", "unlimited"):
                await self._plan_command(event, router, command)
                return
            # user-side shortcuts
            await router.user_panel.dispatch({
                "views": "ov", "reactions": "or", "autoviews": "av", "autoreactions": "ar",
                "members": "mb", "orders": "om", "wallet": "wallet", "premium": "prem",
                "account": "ai", "support": "sp", "addchannel": "addch", "addgroup": "addgr",
                "channels": "ch", "mychannels": "ch",
            }.get(command, "home"), [], event)
        except Exception as exc:
            logger.error(f"/{command} failed: {exc}", exc_info=True)
            await event.respond("⚠️ Command failed. Send /start to reset.")

    async def _plan_command(self, event, router, command: str) -> None:
        """Owner-only plan management from chat.

        /grant <user_id> [reactions|views|comments|members|channels|all] [days] [accounts]
        /unlimited <user_id>            → the special no-limits plan
        /revoke <user_id>               → back to the free tier
        """
        sender = int(getattr(event, "sender_id", 0) or 0)
        if not await router.users.is_owner(sender):
            await event.respond("⛔️ Only the owner can manage plans.")
            return
        words = (getattr(event, "raw_text", "") or "").split()[1:]
        if not words or not words[0].lstrip("-").isdigit():
            await event.respond(
                "Usage:\n"
                "<code>/grant 12345 reactions 30</code> — one service\n"
                "<code>/grant 12345 all 30 25</code> — everything, 30 days, 25 accounts\n"
                "<code>/unlimited 12345</code> — no limits, no expiry\n"
                "<code>/revoke 12345</code> — back to free",
                parse_mode="html",
            )
            return
        user_id = int(words[0])
        users = router.users
        await users.get_or_create(user_id)
        if command == "revoke":
            await users.revoke_premium(user_id)
            await self._notify_plan(user_id, "ℹ️ Your plan was removed by the owner. "
                                             "Free auto reactions stay on.")
            await event.respond(f"✅ Plan revoked for <code>{user_id}</code>.", parse_mode="html")
            return
        if command == "unlimited":
            await users.grant_plan(user_id, unlimited=True, note="owner /unlimited")
            await self._notify_plan(user_id, "♾ <b>Unlimited plan activated</b> by the owner — "
                                             "every service unlocked, no limits.")
            await event.respond(f"♾ Unlimited plan granted to <code>{user_id}</code>.",
                                parse_mode="html")
            return
        kind = words[1] if len(words) > 1 else "all"
        if kind.lower() in ("unlimited", "infinity", "infinite"):
            await users.grant_plan(user_id, unlimited=True, note="owner /grant unlimited")
            await self._notify_plan(user_id, "♾ <b>Unlimited plan activated</b> by the owner.")
            await event.respond(f"♾ Unlimited plan granted to <code>{user_id}</code>.",
                                parse_mode="html")
            return
        days = int(words[2]) if len(words) > 2 and words[2].isdigit() else 30
        accounts = int(words[3]) if len(words) > 3 and words[3].isdigit() else None
        kinds = users.normalise_kinds(kind)
        await users.grant_plan(user_id, kinds=kinds, days=days, accounts=accounts,
                               note=f"owner /grant {kind}")
        from src.services.settings import get_settings_service
        labels = (await get_settings_service().section("premium")).get("feature_labels", {})
        pretty = ", ".join(str(labels.get(k, k)) for k in kinds)
        await self._notify_plan(
            user_id,
            f"🎉 <b>Plan activated</b> by the owner!\n\nServices: <b>{pretty}</b>\n"
            f"Duration: <b>{days} days</b>"
            + (f"\nAccount cap: <b>{accounts}</b>" if accounts else ""),
        )
        await event.respond(
            f"✅ <code>{user_id}</code> → <b>{pretty}</b> for {days} days"
            + (f", {accounts} accounts" if accounts else ""),
            parse_mode="html",
        )

    async def _notify_plan(self, user_id: int, text: str) -> None:
        try:
            from src.services.notify import get_notifier
            await get_notifier().to_user(user_id, text)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"plan notice failed for {user_id}: {exc}")

    @staticmethod
    def _event_actor_id(event, bot_id: int = 0) -> int:
        """Best-effort id of the user who performed a chat action.

        Telegram reports channel service messages in several shapes: sometimes
        ``sender_id`` is None and the actor only exists on ``added_by`` or on the
        action message's ``from_id``. When the actor cannot be determined the
        channel is still registered (and the owner is notified), it just stays
        unowned until someone claims it from the panel.
        """
        candidates = []
        for attr in ("added_by", "kicked_by", "sender_id"):
            try:
                candidates.append(getattr(event, attr, None))
            except Exception:
                pass
        action_message = (getattr(event, "action_message", None)
                          or getattr(event, "message", None))
        if action_message is not None:
            candidates.append(getattr(action_message, "from_id", None))
            candidates.append(getattr(action_message, "sender_id", None))
        update = getattr(event, "original_update", None)
        message = getattr(update, "message", None)
        if message is not None:
            candidates.append(getattr(message, "from_id", None))
            candidates.append(getattr(getattr(message, "action", None), "from_id", None))
        # last resort: the pending user_id, but never the bot itself
        candidates.append(getattr(event, "user_id", None))

        for candidate in candidates:
            value = getattr(candidate, "user_id", candidate)
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number and number != int(bot_id or 0):
                return number
        return 0

    async def _chat_action_handler(self, event):
        """Register the channel when the bot is added, unregister when removed."""
        try:
            me = await self.tbot.get_me()
            if getattr(event, "user_added", False) and getattr(event, "user_id", None) == me.id:
                chat = await event.get_chat()
                channel_id = getattr(chat, "id", None)
                if channel_id is None:
                    return
                internal_id = int(f"-100{channel_id}")
                actor_id = self._event_actor_id(event, getattr(me, "id", 0))
                owner_id = actor_id or None
                owner_source = "added_by" if owner_id else ""
                if not owner_id:
                    # Telegram hid the actor (this is what left the channel
                    # unowned in production). The bot is an admin itself, so it
                    # may simply ask who the channel's admins are. This is a
                    # guess: a later real "added by" replaces it.
                    owner_id = await self._guess_channel_owner(chat)
                    owner_source = "creator_guess"
                invite_link = await self._export_invite(channel_id)
                service = get_channel_service()
                await service.register(
                    internal_id,
                    owner_id,
                    title=getattr(chat, "title", None),
                    username=getattr(chat, "username", None),
                    invite_link=invite_link,
                    kind="channel" if getattr(chat, "broadcast", False) else "group",
                    added_by=owner_id,
                    owner_source=owner_source or None,
                )
                logger.info(
                    f"Bot added to chat {internal_id} by {owner_id or 'unknown'}"
                )
                from src.services.notify import get_notifier
                pool = 0
                try:
                    from src.services.accounts import get_account_registry
                    pool = await get_account_registry().available_count()
                except Exception as exc:
                    logger.debug(f"Account pool size unavailable: {exc}")
                title = getattr(chat, "title", internal_id)
                outcome = {}
                try:
                    outcome = await get_auto_engagement().enable_free_plan(internal_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"free plan start failed for {internal_id}: {exc}")
                logger.info(f"Free plan for {internal_id}: {outcome}")
                if pool:
                    account_line = (
                        "🔄 <b>Auto reactions are ON for every new post</b> "
                        "(free — random positive emojis, 1-3s apart).\n"
                        f"👥 {pool} account(s) in the pool will handle it."
                    )
                    if outcome.get("views") == "on (premium)":
                        account_line += "\n📈 Auto views are <b>ON</b> (premium plan)."
                    else:
                        account_line += ("\n📈 Auto views stay off — they need a premium "
                                         "plan from the owner.")
                    if outcome.get("warmup", {}).get("ok"):
                        account_line += (f"\n✅ {outcome['warmup']['ok']} account(s) already "
                                         "joined for the first post.")
                else:
                    account_line = (
                        "⚠️ <b>No accounts are signed in yet</b>, so nothing can be "
                        "reacted to. Add accounts (Account Management → Add) and the "
                        "reactions start on the next post automatically."
                    )
                body = (
                    "🎉 <b>Channel linked!</b>\n\n"
                    f"📢 {title}\n\n"
                    f"{account_line}\n\n"
                    "Open the panel to change settings or order more."
                )
                notifier = get_notifier()
                if owner_id:
                    await notifier.to_user(
                        owner_id, body,
                        buttons=[[Button.inline("📋 My Channels", "u:ch")]],
                    )
                else:
                    # The actor is unknown (Telegram hides it for some channel
                    # service messages) — tell the admins instead of going silent.
                    logger.warning(
                        f"Could not identify who added the bot to {internal_id}; "
                        f"notifying the admins"
                    )
                    await notifier.to_admins(
                        body + "\n\n<i>I could not tell who added me here — open "
                               "Channels in the admin panel to assign it.</i>",
                        buttons=[[Button.inline("📋 Channels", "a:channels")]],
                    )
            elif getattr(event, "user_kicked", False) and getattr(event, "user_id", None) == me.id:
                chat = await event.get_chat()
                channel_id = getattr(chat, "id", None)
                if channel_id is not None:
                    internal_id = int(f"-100{channel_id}")
                    # The accounts that joined for reactions (or that a plan put
                    # there) must leave the channel again — the owner removed the
                    # bot, so nothing of ours may stay behind.
                    outcome = await get_auto_engagement().leave_channel(internal_id)
                    logger.info(
                        f"Bot removed from chat {channel_id}; channel unregistered "
                        f"({outcome.get('left', 0)} account(s) left)"
                    )
        except Exception as exc:
            logger.warning(f"chat action handler failed: {exc}")

    async def _guess_channel_owner(self, chat) -> Optional[int]:
        """Find the human owner of a channel when the adder is unknown.

        The bot was just added as an admin, so it is allowed to read the admin
        list. The creator wins; otherwise a single human admin is used. Anything
        ambiguous (several admins) stays unowned for a human to assign.
        """
        try:
            from telethon.tl.functions.channels import GetParticipantsRequest
            from telethon.tl.types import Channel, ChannelParticipantsAdmins

            if not isinstance(chat, Channel):
                return None
            result = await self.tbot(GetParticipantsRequest(
                channel=chat, filter=ChannelParticipantsAdmins(),
                offset=0, limit=50, hash=0,
            ))
        except Exception as exc:
            logger.debug(f"Admin list unavailable for {getattr(chat, 'id', '?')}: {exc}")
            return None

        users = {int(getattr(u, "id", 0)): u for u in (getattr(result, "users", None) or [])}
        humans: List[int] = []
        creator: Optional[int] = None
        for participant in getattr(result, "participants", None) or []:
            user_id = int(getattr(participant, "user_id", 0) or 0)
            user = users.get(user_id)
            if user is None or getattr(user, "bot", False):
                continue
            if type(participant).__name__ == "ChannelParticipantCreator":
                creator = user_id
            humans.append(user_id)
        chosen = creator or (humans[0] if len(humans) == 1 else None)
        if chosen:
            logger.info(
                f"Added-by was hidden; attributing the channel to its "
                f"{'creator' if creator else 'only admin'} {chosen}"
            )
        return chosen

    async def _export_invite(self, channel_id: int):
        """Create a fresh invite link for a channel the bot administrates.

        Accepts either the raw channel id (``3852827894``) or the marked form
        (``-1003852827894``) — both appear depending on where the caller got it.
        """
        if self.tbot is None:
            logger.warning(f"Cannot export an invite for {channel_id} yet — bot client not ready")
            return None
        try:
            from telethon.tl.functions.messages import ExportChatInviteRequest
            from telethon.tl.types import InputChannel, Channel
            raw = int(channel_id)
            if raw < 0:
                text = str(raw)
                raw = int(text[4:]) if text.startswith("-100") else abs(raw)
            entity = await self.tbot.get_entity(int(f"-100{raw}"))
            if isinstance(entity, Channel) and not getattr(entity, "username", None):
                link = await self.tbot(ExportChatInviteRequest(peer=entity))
                return getattr(link, "link", None)
        except Exception as exc:
            # Worth a warning: without a link, accounts that are not already
            # members cannot join a private channel, so automation would fail.
            logger.warning(
                f"Could not create an invite link for {channel_id}: {exc}. "
                f"Private-channel reactions need invite permission for the bot."
            )
        return None

    async def _refresh_invite_if_missing(self, internal_id: int, chat) -> None:
        """Create the invite link on a later post if it failed when added.

        Private channels need it so accounts can join before reacting; the first
        attempt can fail (permission not granted yet, temporary error), and
        without this retry the channel would stay broken for good.
        """
        try:
            service = get_channel_service()
            doc = await service.get(internal_id)
            if doc is None or doc.get("invite_link"):
                return
            if getattr(chat, "username", None):
                return                      # public channel: a username is enough
            raw_id = getattr(chat, "id", None)
            if raw_id is None:
                return
            raw_id = int(raw_id)
            if raw_id < 0:                      # -100… form
                text = str(raw_id)
                raw_id = int(text[4:]) if text.startswith("-100") else abs(raw_id)
            link = await self._export_invite(raw_id)
            if link:
                await service.register(internal_id, title=getattr(chat, "title", None),
                                       invite_link=link)
                logger.info(f"Invite link created for {internal_id}: {link[:24]}…")
        except Exception as exc:
            logger.debug(f"Invite refresh skipped for {internal_id}: {exc}")

    async def handle_channel_post(self, event):
        """Public entry point used by the generic message handler."""
        await self._channel_post_handler(event)

    async def _channel_post_handler(self, event):
        """Trigger auto reactions / auto views for a newly published post."""
        try:
            chat = await event.get_chat()
            channel_id = getattr(chat, "id", None)
            if channel_id is None:
                return
            internal_id = int(f"-100{channel_id}")
            await self._refresh_invite_if_missing(internal_id, chat)
            await get_auto_engagement().handle_new_post(
                internal_id, int(event.id),
            )
        except Exception as exc:
            logger.error(f"auto engagement failed for a new post: {exc}", exc_info=True)

    def admin_only(self, handler):
        """
        Decorator to restrict access to admin-only handlers.

        :param handler: The handler function to wrap.
        """
        async def wrapper(event):
            try:
                sender_id = event.sender_id
                logger.debug(f"admin_only check: sender_id={sender_id}, ADMIN_ID={ADMIN_ID}")
                
                # Validate ADMIN_ID before comparison
                try:
                    from src.utils import validate_admin_id
                    validated_admin_id = validate_admin_id(ADMIN_ID)
                except ValueError as e:
                    logger.error(f"Invalid ADMIN_ID configuration: {e}")
                    await event.respond("Bot configuration error. Please contact administrator.")
                    return
                
                if sender_id == validated_admin_id:
                    await handler(event)
                else:
                    logger.warning(f"Unauthorized access attempt from user {sender_id}")
                    await event.respond("You are not the admin")
            except Exception as e:
                logger.error(f"Error in admin_only wrapper: {e}", exc_info=True)
                try:
                    await event.respond("An error occurred. Please try again.")
                except Exception as e:
                    logger.debug(f"Failed to notify user of error: {e}")
        return wrapper

    async def run(self):
        """
        Run the bot and monitor clients with automatic reconnection.
        """
        max_retries = 5
        retry_delay = 30  # seconds
        
        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    logger.info(f"Attempting to restart bot (attempt {attempt + 1}/{max_retries})...")
                    # Reset message processing flags for reconnection and remove
                    # the previously registered handlers so they don't stack up
                    # (which would forward each message multiple times).
                    async with self.active_clients_lock:
                        for client in self.active_clients.values():
                            if self.monitor:
                                self.monitor.cleanup_client_handlers(client)
                            if hasattr(client, '_message_processing_set'):
                                delattr(client, '_message_processing_set')
                    await asyncio.sleep(retry_delay)
                
                await self.start()
                logger.info("Bot is running...")
                logger.info("=" * 60)
                logger.info("✅ Bot started successfully and is ready to receive commands!")
                logger.info("=" * 60)

                # Start monitoring messages for all active clients (only once per client)
                async with self.active_clients_lock:
                    for client in self.active_clients.values():
                        if not hasattr(client, '_message_processing_set'):
                            # Start monitoring tasks in background (don't await them)
                            asyncio.create_task(self.monitor.process_messages_for_client(client))
                            client._message_processing_set = True
                
                # Keep the bot running until disconnected
                logger.info("Bot is now running and waiting for messages...")
                logger.info("Send /start to your bot in Telegram to begin.")
                await self.tbot.run_until_disconnected()
                
                # If we reach here, the bot disconnected
                logger.warning("Bot disconnected. Will attempt to reconnect...")
                
            except asyncio.CancelledError:
                logger.warning("Bot run was cancelled")
                break
            except KeyboardInterrupt:
                logger.info("Bot stopped by user")
                break
            except SystemExit:
                # configuration error — already logged with instructions
                logger.error("Start-up aborted: fix the configuration above and redeploy.")
                break
            except Exception as e:
                hint = _credentials_hint(e)
                if hint:
                    # Retrying will never fix a wrong API id / bot token, so stop
                    # with a message the operator can act on.
                    logger.critical(f"{hint} ({e})")
                    try:
                        await self.notify_admin(f"❌ {hint}")
                    except Exception:
                        pass
                    break
                logger.error(f"Error running bot (attempt {attempt + 1}/{max_retries}): {e}", exc_info=True)
                try:
                    await self.notify_admin(f"❌ Bot encountered an error: {str(e)[:200]}")
                except Exception:
                    logger.error("Failed to notify admin about error")
                
                if attempt < max_retries - 1:
                    logger.info(f"Waiting {retry_delay} seconds before retry...")
                    await asyncio.sleep(retry_delay)
                else:
                    logger.critical(f"Max retries ({max_retries}) reached. Shutting down bot.")
                    break
            finally:
                if attempt < max_retries - 1:
                    # Don't fully shutdown on retry, just disconnect
                    try:
                        logger.info("Disconnecting for retry...")
                        if self.tbot and self.tbot.is_connected():
                            await self.tbot.disconnect()
                    except Exception as e:
                        logger.error(f"Error disconnecting: {e}")
                else:
                    # Final shutdown
                    await self.shutdown()

    async def _pool_supervisor_loop(self) -> None:
        """Keep every signed-in account connected, forever.

        Runs until shutdown; each pass reconnects dropped clients and rebuilds
        accounts that only exist in the database. The interval is admin-editable
        (``accounts.supervise_seconds``, default 60).
        """
        from src.services.settings import get_settings_service

        while True:
            try:
                interval = 60
                try:
                    cfg = await get_settings_service().section("accounts")
                    interval = max(20, int(cfg.get("supervise_seconds", 60) or 60))
                except Exception:  # noqa: BLE001 - keep the default
                    interval = 60
                await asyncio.sleep(interval)
                registry = self.account_registry
                if registry is None:
                    continue
                stats = await registry.revive_disconnected()
                live = len(registry.live_clients())
                if stats.get("reconnected") or stats.get("restored") or stats.get("revoked"):
                    logger.info(f"Pool after supervision: {live} live account(s)")
            except asyncio.CancelledError:
                logger.info("Pool supervisor stopped")
                raise
            except Exception as exc:  # noqa: BLE001 - never kill the loop
                logger.warning(f"Pool supervision pass failed: {exc}")
                await asyncio.sleep(30)

    async def _warm_channels_after_boot(self) -> None:
        """Place the restored pool into every channel that needs members."""
        try:
            warm = await get_auto_engagement().warm_all_channels(reason="boot", notify=False)
            if warm.get("joined"):
                logger.info(
                    f"Boot warm-up: {warm['joined']} membership(s) added across "
                    f"{warm.get('channels', 0)} channel(s)"
                )
        except Exception as exc:  # noqa: BLE001 - never break the running bot
            logger.warning(f"Channel warm-up after boot failed: {exc}")

    async def notify_admin(self, message, buttons=None, parse_mode="html") -> bool:
        """
        Send a notification message to the owner (and every admin).

        :param message: The message text to send.
        :param buttons: Optional inline keyboard.
        :return: True if at least one message went through.
        """
        from src.utils import validate_admin_id
        try:
            admin_id = validate_admin_id(ADMIN_ID)
        except ValueError as e:
            logger.error(f"Invalid ADMIN_ID configuration: {e}")
            admin_id = 0
        targets = sorted({
            int(getattr(app_config, "OWNER_ID", OWNER_ID) or 0), admin_id,
            *[int(a) for a in (getattr(app_config, "ADMIN_IDS", ADMIN_IDS) or [])],
        } - {0})
        delivered = False
        for target in targets:
            try:
                from src.utils import detect_parse_mode
                await self.tbot.send_message(
                    target, message, buttons=buttons,
                    parse_mode=detect_parse_mode(message, parse_mode),
                )
                delivered = True
            except Exception as e:
                # A send failure usually just means the admin never started the
                # bot, so Telegram refuses the message.
                logger.warning(f"Could not notify admin {target}: {e}")
        if delivered:
            logger.info("Notification sent to admin.")
        return delivered

    async def _start_health_server(self) -> None:
        """Bring up the optional health endpoint when the platform asks for it."""
        try:
            from src.health import HealthServer, health_port_from_env
            port = health_port_from_env()
            if not port:
                return
            self.health_server = HealthServer(port, bot=self)
            await self.health_server.start()
        except Exception as exc:
            logger.warning(f"Health endpoint disabled: {exc}")

    async def shutdown(self):
        """
        Clean up resources and shut down the bot gracefully.
        """
        try:
            logger.info("Shutting down bot...")
            supervisor = getattr(self, "_pool_supervisor_task", None)
            if supervisor is not None and not supervisor.done():
                supervisor.cancel()
                try:
                    await supervisor
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self.health_server is not None:
                await self.health_server.stop()
            if self.client_manager:
                await self.client_manager.disconnect_all_clients()
            if self.tbot and self.tbot.is_connected():
                await self.tbot.disconnect()
            if self.store is not None:
                try:
                    await self.store.close()
                except Exception as e:
                    logger.debug(f"Error closing storage: {e}")
            logger.info("Bot shut down successfully")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")
