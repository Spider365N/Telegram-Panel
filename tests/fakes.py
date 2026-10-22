"""
Test doubles for the panel: fake Telegram clients, events and accounts.

These let the whole stack (router → panels → services → engagement → clients) run
end-to-end inside pytest, with no network and no real Telegram credentials.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from telethon.tl.types import Channel, InputChannel, PeerChannel, User


class FakeSession:
    """Mimics a Telethon session object, with the fields StringSession.save needs."""

    def __init__(self, name: str = "fake_session"):
        from telethon.crypto import AuthKey
        self.filename = name
        self.auth_key = AuthKey(bytes(range(256)))
        self.dc_id = 2
        self.server_address = "149.154.167.51"
        self.port = 443
        self._saved = 0

    def save(self):
        self._saved += 1
        return "fake-session-string"


class FakeUser:
    def __init__(self, user_id: int, username: Optional[str] = None, phone: Optional[str] = None):
        self.id = user_id
        self.username = username
        self.phone = phone
        self.first_name = f"User{user_id}"
        self.last_name = ""
        self.access_hash = user_id * 7


class FakeMessage:
    def __init__(self, message_id: int, text: str = "", media=None, post: bool = False,
                 reply_markup=None):
        self.id = message_id
        self.text = text
        self.message = text
        self.media = media
        self.post = post
        self.reply_markup = reply_markup
        self.reactions = []


class FakeTelegramClient:
    """A TelegramClient stand-in that records every request it is asked to send.

    Behaviour knobs let tests reproduce the real-world failure from the crash
    log: ``known_entities=False`` makes ``get_entity`` raise the Telethon
    "Could not find the input entity" error until an access hash is learned.
    """

    def __init__(
        self,
        name: str = "client",
        user_id: int = 1000,
        *,
        known_entities: Optional[set] = None,
        channels: Optional[Dict[int, Channel]] = None,
        member_of: Optional[set] = None,
        fail_join: bool = False,
        request_approval: bool = False,
        stale_hashes: Optional[set] = None,
        reaction_allowlist: Optional[set] = None,
        authorized: bool = True,
        connected: bool = True,
    ):
        self.session = FakeSession(name)
        self.name = name
        self._me = FakeUser(user_id)
        # Channels the account is a member of (GetChannels works for those) —
        # by default every known channel, unless the test says otherwise.
        self.known_entities = set(known_entities or ())
        self.channels = dict(channels or {})
        self.member_of = set(member_of) if member_of is not None else set(self.channels.keys())
        self.fail_join = fail_join
        self.request_approval = request_approval
        # invite hashes this account refuses (revoked/expired links)
        self.stale_hashes = set(stale_hashes or ())
        # who the (fake) server reports as admins/creator of a channel
        self.admin_ids: list = []
        self.creator_id = None
        # reactions the (fake) server accepts; None = Telegram's real set.
        # Anything else raises ReactionInvalidError, exactly like Telegram does
        # for emojis outside its own reaction set (the production "✅" failure).
        self.reaction_allowlist = set(reaction_allowlist) if reaction_allowlist else None
        # chats this account deliberately left again (LeaveChannelRequest)
        self.left: List[Any] = []
        # reactions the server accepted (refused ones are not listed)
        self.applied_reactions: List[str] = []
        self.authorized = authorized
        self.connected = connected
        self.requests: List[Any] = []
        self.sent_messages: List[Dict[str, Any]] = []
        self.joined: List[Any] = []
        self.get_entity_calls: List[Any] = []
        self.messages_fetched: List[Any] = []
        self.dialogs_calls = 0
        # inline-button presses (messages.GetBotCallbackAnswerRequest)
        self.votes: List[Dict[str, Any]] = []
        self.callback_presses: List[Dict[str, Any]] = []
        # force-subscribe: {channel_id: {user_id, ...}} — who is inside a channel
        self.channel_members: Dict[int, set] = {}
        # set to True to make every membership check raise (network-style failure)
        self.membership_error = False

    # -- connection ------------------------------------------------------
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self):
        return self._me

    # -- entity resolution ----------------------------------------------
    async def get_entity(self, peer):
        """Only succeeds for entities the session already has cached — exactly
        like Telethon, which is what makes the production bug reproducible."""
        self.get_entity_calls.append(peer)
        if isinstance(peer, str):
            handle = peer.lstrip("@")
            for chat in self.channels.values():
                if getattr(chat, "username", None) == handle:
                    self.known_entities.add(chat.id)
                    return chat
        key = self._peer_key(peer)
        if key in self.known_entities or (isinstance(peer, str) and peer in self.known_entities):
            return self.channels.get(key) or _StubEntity(key)
        raise ValueError(
            f"Could not find the input entity for {peer!r} (PeerChannel). Please read "
            f"https://docs.telethon.dev/en/stable/concepts/entities.html to find out more details."
        )

    def _reaction_accepted(self, emoji) -> bool:
        if emoji is None:
            return False
        if self.reaction_allowlist is not None:
            return emoji in self.reaction_allowlist or emoji.replace("\ufe0f", "") in {
                e.replace("\ufe0f", "") for e in self.reaction_allowlist
            }
        from src.services.settings import is_allowed_reaction_emoji
        return is_allowed_reaction_emoji(emoji)

    async def get_input_entity(self, peer):
        """Telethon caches input peers for every chat it can see."""
        key = self._peer_key(peer)
        chat = self.channels.get(key)
        if chat is None:
            raise ValueError(f"Could not find the input entity for {peer!r}")
        from telethon.tl.types import InputPeerChannel
        return InputPeerChannel(channel_id=int(getattr(chat, "id", key)),
                                access_hash=int(getattr(chat, "access_hash", 1)))

    @staticmethod
    def _peer_key(peer):
        if isinstance(peer, (PeerChannel, InputChannel)):
            return int(getattr(peer, "channel_id", 0))
        if hasattr(peer, "id") and not isinstance(peer, (int, str)):
            # a real Channel/Chat/User entity object
            return int(getattr(peer, "id") or 0)
        if isinstance(peer, int):
            if str(peer).startswith("-100"):
                return int(str(peer)[4:])
            return abs(peer)
        return peer

    async def get_permissions(self, entity, user=None):
        """Channel permissions of *user* — raise ``UserNotParticipantError`` when
        the user is outside, exactly like Telethon does."""
        from telethon import errors as tl_errors

        if self.membership_error:
            raise ConnectionError("simulated network failure")
        key = self._peer_key(entity)
        uid = int(getattr(user, "id", user) or 0)
        members = self.channel_members.get(key) or self.channel_members.get(abs(key)) or set()
        if uid and uid in {int(m) for m in members}:
            return type("Permissions", (), {"channel_id": key, "user_id": uid})()
        raise tl_errors.UserNotParticipantError(request=None)

    async def get_dialogs(self, limit: int = 100):
        """Only chats the account actually participates in show up in dialogs."""
        self.dialogs_calls += 1
        out = []
        for channel_id, chat in self.channels.items():
            if channel_id not in self.member_of:
                continue
            self.known_entities.add(int(getattr(chat, "id", 0)))
            out.append(type("Dialog", (), {"entity": chat})())
        return out

    # -- requests --------------------------------------------------------
    async def __call__(self, request, *args, **kwargs):
        self.requests.append(request)
        name = type(request).__name__

        if name == "GetChannelsRequest":
            chats = []
            for input_channel in getattr(request, "id", []):
                channel_id = int(getattr(input_channel, "channel_id", 0))
                channel = self.channels.get(channel_id)
                # The server only hands over a channel the account is a member
                # of (regardless of access hash) — otherwise it returns nothing.
                if channel is not None and (channel_id in self.member_of or channel_id in self.known_entities):
                    self.known_entities.add(channel.id)
                    chats.append(channel)
            return type("Channels", (), {"chats": chats, "users": []})()

        if name == "ResolveUsernameRequest":
            username = getattr(request, "username", "")
            for chat in self.channels.values():
                if getattr(chat, "username", None) == username:
                    self.known_entities.add(chat.id)
                    return type("Resolved", (), {"chats": [chat], "users": []})()
            raise ValueError(f"Cannot find any entity corresponding to @{username}")

        if name == "ImportChatInviteRequest":
            from telethon import errors as tl_errors
            invite_hash = getattr(request, "hash", "")
            if self.fail_join or invite_hash in self.stale_hashes:
                raise tl_errors.InviteHashExpiredError(request=request)
            if self.request_approval:
                # Telegram turns the import into a join *request* and answers with
                # InviteRequestSentError — the account stays outside until an
                # admin approves it.
                raise tl_errors.InviteRequestSentError(request=request)
            chat = next(iter(self.channels.values()), None)
            if chat is not None:
                self.known_entities.add(chat.id)
                self.member_of.add(chat.id)          # importing makes you a member
            self.joined.append(getattr(request, "hash", None))
            return type("Invite", (), {"chats": [chat] if chat else [], "users": []})()

        if name == "CheckChatInviteRequest":
            # Exactly like Telegram: only ChatInviteAlready has a ``chat`` field.
            # A plain ChatInvite (the "you are not a member yet" answer) does NOT,
            # which is the whole point of this fake — modelling it wrong is what
            # hid the "accounts can never join" bug.
            from telethon.tl.types import ChatInvite, ChatInviteAlready

            chat = next(iter(self.channels.values()), None)
            from telethon import errors as tl_errors
            if self.fail_join or getattr(request, "hash", "") in self.stale_hashes:
                raise tl_errors.InviteHashInvalidError(request=request)
            if chat is not None and chat.id in self.member_of:
                return ChatInviteAlready(chat=chat)
            if self.request_approval:
                from telethon import errors as tl_errors
                return ChatInvite(
                    title="Fake", photo=None, participants_count=2, color=0,
                    channel=True, broadcast=True, public=False, megagroup=False,
                    request_needed=True, verified=False, scam=False, fake=False,
                    can_refulfill_subscription=False, about=None, participants=None,
                    subscription_pricing=None, subscription_form_id=None,
                    bot_verification=None,
                )
            return ChatInvite(
                title="Fake", photo=None, participants_count=2, color=0,
                channel=True, broadcast=True, public=False, megagroup=False,
                request_needed=False, verified=False, scam=False, fake=False,
                can_refulfill_subscription=False, about=None, participants=None,
                subscription_pricing=None, subscription_form_id=None,
                bot_verification=None,
            )

        if name == "JoinChannelRequest":
            entity = getattr(request, "channel", None)
            self.joined.append(entity)
            channel_id = int(getattr(entity, "channel_id", getattr(entity, "id", 0)) or 0)
            if channel_id:
                self.member_of.add(channel_id)
                self.known_entities.add(channel_id)
            return type("Joined", (), {})()

        if name == "SendReactionRequest":
            emoji = None
            reaction = getattr(request, "reaction", None) or []
            if reaction:
                emoji = getattr(reaction[0], "emoticon", None)
            if not self._reaction_accepted(emoji):
                from telethon.errors import ReactionInvalidError
                raise ReactionInvalidError(request)
            if emoji:
                # only emojis the (fake) server accepted are recorded here
                self.applied_reactions.append(emoji)
            for message in getattr(self, "messages", {}).values():
                if getattr(message, "id", None) == getattr(request, "msg_id", None):
                    message.reactions.append(reaction)
            return type("Updates", (), {})()

        if name == "LeaveChannelRequest":
            entity = getattr(request, "channel", None)
            channel_id = int(getattr(entity, "channel_id", getattr(entity, "id", 0)) or 0)
            self.left.append(channel_id)
            self.member_of.discard(channel_id)
            self.known_entities.discard(channel_id)
            return type("Left", (), {})()

        if name == "GetMessagesViewsRequest":
            return type("Views", (), {})()
        if name == "SendVoteRequest":
            self.votes.append({
                "msg_id": getattr(request, "msg_id", None),
                "options": list(getattr(request, "options", None) or []),
            })
            return type("Updates", (), {})()
        if name == "GetBotCallbackAnswerRequest":
            self.callback_presses.append({
                "msg_id": getattr(request, "msg_id", None),
                "data": getattr(request, "data", None),
            })
            return type("BotCallbackAnswer", (), {"message": None, "alert": False})()
        if name == "ExportChatInviteRequest":
            return type("ExportedInvite", (), {"link": "https://t.me/+fakeprivate"})()

        if name == "GetParticipantsRequest":
            # Models `channels.GetParticipants(ChannelParticipantsAdmins)`, which
            # is how the bot attributes a channel when Telegram hides who added
            # it. ``admin_ids`` decides the answer (empty = nothing to learn).
            users = []
            participants = []
            for admin_id in (self.admin_ids or ()):
                users.append(FakeUser(int(admin_id)))
                participants.append(type("ChannelParticipantAdmin", (), {
                    "user_id": int(admin_id),
                })())
            if self.creator_id:
                users.append(FakeUser(int(self.creator_id)))
                participants.append(type("ChannelParticipantCreator", (), {
                    "user_id": int(self.creator_id),
                })())
            return type("Participants", (), {
                "participants": participants, "users": users, "count": len(participants),
            })()

        return type("Ok", (), {})()

    # -- helpers ---------------------------------------------------------
    async def get_messages(self, entity, ids=None, limit=None):
        self.messages_fetched.append((entity, ids))
        ids = ids or []
        return next(iter(self.messages.values()), None) if ids else []

    async def send_message(self, entity, text, **kwargs):
        self.sent_messages.append({"entity": entity, "text": text, **kwargs})
        return FakeMessage(len(self.sent_messages), text)


class _StubEntity:
    def __init__(self, key):
        self.id = key if isinstance(key, int) else 0
        self.username = key if isinstance(key, str) else None
        self.title = str(key)
        self.broadcast = True


class FakeBot:
    """Minimal stand-in for ``TelegramBot`` used by services and panels."""

    def __init__(self, clients: Optional[Dict[str, Any]] = None, bot_client=None):
        import asyncio
        self.active_clients = dict(clients or {})
        self.active_clients_lock = asyncio.Lock()
        self.tbot = bot_client or FakeTelegramClient("bot", user_id=999)
        self.config = {"clients": {}, "inactive_accounts": {}}
        self.panel_router = None
        self.account_registry = None
        self.store = None
        self.exported_invites: List[str] = []

    async def _export_invite(self, channel_id):
        """Same contract as the real bot: a fresh invite link for a private chat.

        Public channels need none, so this mirrors that by returning None when
        the (fake) chat has a username.
        """
        from src.peers import PeerResolver
        internal = PeerResolver.to_internal_channel_id(channel_id)
        chat = self.tbot.channels.get(internal)
        if chat is not None and getattr(chat, "username", None):
            return None
        link = f"https://t.me/+fake{internal}"
        self.exported_invites.append(link)
        return link


async def build_stack(clients: Optional[Dict[str, Any]] = None, *, bot_client=None):
    """Build a complete, freshly reset stack backed by an in-memory store."""
    from src.db.store import MemoryStore, reset_store
    from src.panels import get_panel_router, reset_panel_router
    from src.runtime import set_bot
    from src.services.accounts import AccountStore, AccountRegistry, reset_account_registry
    from src.services.autoengage import get_auto_engagement, reset_auto_engagement
    from src.services.channels import get_channel_service, reset_channel_service
    from src.services.engagement import get_engagement_service, reset_engagement_service
    from src.services.executor import get_order_executor, reset_order_executor
    from src.services.forcesub import get_force_sub_service, reset_force_sub_service
    from src.services.notify import get_notifier, reset_notifier
    from src.services.orders import get_order_service, reset_order_service
    from src.services.payments import get_payment_service, reset_payment_service
    from src.services.requests import reset_plan_request_service
    from src.services.settings import get_settings_service, reset_settings_service
    from src.services.users import get_user_service, reset_user_service
    from src.peers import reset_peer_resolver

    store = MemoryStore()
    await reset_store(store)
    for resetter in (reset_settings_service, reset_user_service, reset_order_service,
                     reset_payment_service, reset_channel_service, reset_engagement_service,
                     reset_order_executor, reset_auto_engagement, reset_account_registry,
                     reset_panel_router, reset_notifier, reset_peer_resolver,
                     reset_plan_request_service, reset_force_sub_service):
        resetter()

    settings = get_settings_service(store)
    users = get_user_service(store, settings)
    orders = get_order_service(store, settings, users)
    payments = get_payment_service(store, settings, users)
    channels = get_channel_service(store, settings)
    registry = AccountRegistry(AccountStore(store))
    engagement = get_engagement_service(registry, settings)
    notifier = get_notifier()
    executor = get_order_executor(engagement, orders, notifier, channels, settings, users)
    auto = get_auto_engagement(engagement, channels, users, settings, orders)
    force_sub = get_force_sub_service(settings)

    bot = FakeBot(clients, bot_client)
    bot.store = store
    bot.account_registry = registry.bind(bot)
    accounts_store = registry.store
    set_bot(bot)
    notifier.bind(bot.tbot)

    # Tests must not sit through the human-like random delays.
    for path in ("reactions.delay_min", "reactions.delay_max",
                 "reactions.auto.delay_min", "reactions.auto.delay_max",
                 "views.auto.delay_min", "views.auto.delay_max",
                 "members.delay_min", "members.delay_max",
                  "comments.delay_min", "comments.delay_max",
                 "reactions.free_cooldown_seconds"):
        await settings.set_path(path, 0)

    router = get_panel_router(bot)
    bot.panel_router = router
    return type("Stack", (), {
        "store": store, "accounts": accounts_store, "settings": settings,
        "users": users, "orders": orders,
        "payments": payments, "channels": channels, "registry": registry,
        "engagement": engagement, "executor": executor, "auto": auto,
        "force_sub": force_sub,
        "notifier": notifier, "router": router, "bot": bot,
        "user_panel": router.user_panel, "admin_panel": router.admin_panel,
    })()


async def session_stack(accounts: int = 2, *, user_id_start: int = 8000):
    """A stack with N signed-in accounts and a synced registry.

    Used by the round-6 pool tests: one of these accounts is deliberately
    disconnected to reproduce the production "5 reactions from 6 accounts" case.
    """
    clients = {
        f"phone{index}": FakeTelegramClient(f"phone{index}", user_id=user_id_start + index)
        for index in range(accounts)
    }
    clients["919797869192"] = FakeTelegramClient("919797869192", user_id=919797869192)
    stack = await build_stack(clients)
    await stack.registry.sync_from_live_clients()
    return stack


def make_channel(channel_id: int, username: Optional[str] = None, title: str = "Test Channel") -> Channel:
    return Channel(
        id=channel_id,
        title=title,
        photo=None,
        date=None,
        creator=False,
        left=False,
        broadcast=True,
        verified=False,
        megagroup=False,
        restricted=False,
        signatures=False,
        min=False,
        scam=False,
        has_link=False,
        has_geo=False,
        slowmode_enabled=False,
        access_hash=channel_id * 3,
        username=username,
        restriction_reason=None,
        admin_rights=None,
        banned_rights=None,
        default_banned_rights=None,
        participants_count=1,
    )


# ---------------------------------------------------------------------------
# Fake events
# ---------------------------------------------------------------------------
class FakeMessage:
    """The event-payload message (also carries media / inline buttons)."""

    def __init__(self, text: str = "", message_id: int = 1, post: bool = False,
                 media=None, reply_markup=None):
        self.text = text
        self.message = text
        self.id = message_id
        self.post = post
        self.media = media
        self.reply_markup = reply_markup


class FakeEvent:
    """Acts like a Telethon NewMessage / CallbackQuery event."""

    def __init__(self, *, sender_id: int, chat_id: int, text: str = "",
                 data: Optional[str] = None, sender=None, message_id: int = 1,
                 is_channel: bool = False, post: bool = False, chat=None):
        self.sender_id = sender_id
        self.chat_id = chat_id
        self.id = message_id
        self.message_id = message_id
        self.raw_text = text
        self.text = text
        self.message = FakeMessage(text, message_id, post)
        self.data = data.encode() if data else None
        self.sender = sender or FakeUser(sender_id)
        self.is_channel = is_channel
        self.post = post
        self.chat = chat
        self.edits: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self.answers: List[Dict[str, Any]] = []

    # callbacks --------------------------------------------------------
    async def answer(self, text=None, alert=False, **kwargs):
        self.answers.append({"text": text, "alert": alert})

    async def edit(self, text, buttons=None, parse_mode=None, **kwargs):
        self.edits.append({"text": text, "buttons": buttons, "parse_mode": parse_mode})
        return type("Message", (), {"id": self.message_id})()

    async def respond(self, text, buttons=None, parse_mode=None, **kwargs):
        self.responses.append({"text": text, "buttons": buttons, "parse_mode": parse_mode})
        return type("Message", (), {"id": self.message_id + 100})()

    async def get_chat(self):
        return self.chat

    # convenience ------------------------------------------------------
    @property
    def last_text(self) -> str:
        if self.edits:
            return self.edits[-1]["text"]
        if self.responses:
            return self.responses[-1]["text"]
        return ""

    @property
    def last_buttons(self):
        if self.edits:
            return self.edits[-1]["buttons"]
        if self.responses:
            return self.responses[-1]["buttons"]
        return None

    def button_labels(self) -> List[str]:
        labels = []
        for row in (self.last_buttons or []):
            for button in row:
                labels.append(getattr(button, "text", str(button)))
        return labels

    def all_button_labels(self) -> List[str]:
        labels = []
        for entry in self.edits + self.responses:
            for row in (entry.get("buttons") or []):
                for button in row:
                    labels.append(getattr(button, "text", str(button)))
        return labels


async def drain_executor(stack, timeout: float = 15.0) -> None:
    """Wait for every background order task the executor started.

    Accepts the stack object from ``build_stack`` or a plain dict.
    """
    import asyncio
    executor = stack["executor"] if isinstance(stack, dict) else getattr(stack, "executor", None)
    tasks = [t for t in list(getattr(executor, "_tasks", {}).values()) if not t.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)
