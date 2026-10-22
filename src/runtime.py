"""
Tiny runtime registry so deep services can reach the live bot instance.

The panel is event driven: handlers are created by ``TelegramBot`` and services
are shared singletons. Instead of threading the bot object through every
constructor (and breaking in tests), services call ``get_bot()`` when they need
the bot client, and ``TelegramBot.start()`` registers itself here.
"""
from __future__ import annotations

from typing import Any, Optional

_bot: Optional[Any] = None


def set_bot(bot) -> None:
    global _bot
    _bot = bot


def get_bot():
    return _bot


def clear_bot() -> None:
    global _bot
    _bot = None
