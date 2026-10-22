"""
Bot command auto-registration.

Every command the bot implements is declared once here and pushed to Telegram
with ``setMyCommands`` style scopes, so the ☰ menu in every chat is always in
sync with the code — no more "hand-typed in BotFather" drift.

Scopes used:
* default scope          → user commands (what everyone sees)
* chat-admins scope      → admin panel shortcuts (shown to the owner/admins)
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence, Tuple

from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.tl.types import (
    BotCommand,
    BotCommandScopeChatAdmins,
    BotCommandScopeDefault,
)

logger = logging.getLogger(__name__)

# (command, description) — the single source of truth for the bot menu.
USER_COMMANDS: List[Tuple[str, str]] = [
    ("start", "Open the panel"),
    ("mychannels", "Your channels & their status"),
    ("panel", "Open the panel"),
    ("menu", "Show the main menu"),
    ("views", "Order views on a post"),
    ("reactions", "Order reactions on a post"),
    ("autoviews", "Auto views for your channel"),
    ("autoreactions", "Auto reactions for your channel"),
    ("members", "Order members for your channel"),
    ("orders", "Your orders & their status"),
    ("wallet", "Wallet balance and top-up"),
    ("premium", "Premium plans"),
    ("account", "Your account info"),
    ("addchannel", "Add the bot to your channel"),
    ("addgroup", "Add the bot to your group"),
    ("support", "Contact support"),
    ("help", "How to use the bot"),
]

ADMIN_COMMANDS: List[Tuple[str, str]] = [
    ("admin", "Open the admin panel"),
    ("grant", "Give a user a plan (owner only)"),
    ("pricing", "Edit prices"),
    ("limits", "Free-tier limits & automation"),
    ("users", "Manage users"),
    ("aorders", "All orders"),
    ("topups", "Approve wallet top-ups"),
    ("channels", "Channels using the bot"),
    ("accounts", "Account pool"),
    ("broadcast", "Message all users"),
    ("settings", "Payment & support settings"),
    ("stats", "Bot statistics"),
]

OWNER_COMMANDS: List[Tuple[str, str]] = [
    ("sendpv", "Send a private message (owner only)"),
    ("revoke", "Remove a user's plan (owner only)"),
    ("unlimited", "Give a user the unlimited plan (owner only)"),
]


def _build(pairs: Sequence[Tuple[str, str]]) -> List[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d in pairs]


async def register_commands(client, *, owner_id: Optional[int] = None,
                            admin_ids: Sequence[int] = ()) -> dict:
    """Push command menus to Telegram. Never raises — failures are logged."""
    result = {"user": False, "admin": False, "owner": False}
    scopes = [
        ("user", BotCommandScopeDefault(), _build(USER_COMMANDS)),
        ("admin", BotCommandScopeChatAdmins(), _build(ADMIN_COMMANDS)),
    ]
    for name, scope, commands in scopes:
        try:
            await client(SetBotCommandsRequest(scope=scope, lang_code="", commands=commands))
            result[name] = True
        except Exception as exc:
            logger.warning(f"Could not register {name} commands: {exc}")
    if owner_id:
        commands = _build(list(USER_COMMANDS) + list(ADMIN_COMMANDS) + list(OWNER_COMMANDS))
        # A per-user command scope needs the owner's input peer (user id +
        # access hash), which Telegram only reveals once the owner has written
        # to the bot. Until then the owner still gets the admin menu through
        # BotCommandScopeChatAdmins and every command keeps working when typed.
        scopes_to_try = []
        try:
            peer = await client.get_input_entity(int(owner_id))
        except Exception as exc:
            peer = None
            logger.info(
                "Owner menu will appear after the owner sends /start to the bot "
                f"(could not resolve the peer yet: {exc})"
            )
        if peer is not None:
            try:
                from telethon.tl.types import BotCommandScopePeerUser
                scopes_to_try.append((
                    "owner",
                    BotCommandScopePeerUser(peer=peer, user_id=int(owner_id)),
                ))
            except Exception as exc:  # pragma: no cover - older Telethon
                logger.debug(f"BotCommandScopePeerUser unavailable: {exc}")

        for label, scope in scopes_to_try:
            try:
                await client(SetBotCommandsRequest(scope=scope, lang_code="", commands=commands))
                result["owner"] = True
                break
            except Exception as exc:
                logger.debug(f"Owner command scope ({label}) not applied: {exc}")
    logger.info(
        "Command menu registered — user:%s admin:%s owner:%s",
        result["user"], result["admin"], result["owner"],
    )
    return result


def all_commands() -> List[str]:
    """Every registered command name (used by tests and docs)."""
    names = [c for c, _ in USER_COMMANDS] + [c for c, _ in ADMIN_COMMANDS]
    names += [c for c, _ in OWNER_COMMANDS]
    return sorted(set(names))
