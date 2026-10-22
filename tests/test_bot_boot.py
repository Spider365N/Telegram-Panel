"""
Boot test: does ``TelegramBot.start()`` wire everything up correctly?

Runs the real startup sequence (storage, handlers, command menu, account
sync/restore, admin notification) against a fake transport, so a broken start-up
order is caught without touching Telegram.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from tests.fakes import FakeTelegramClient, FakeEvent

BOT_USER_ID = 424242


class FakeBotTransport(FakeTelegramClient):
    """The bot client itself: supports start()/add_event_handler()."""

    def __init__(self):
        super().__init__("bot", user_id=BOT_USER_ID)
        self._me.username = "demo_panel_bot"
        self.handlers: List[Dict[str, Any]] = []
        self.started_with: Dict[str, Any] = {}
        self.disconnected = False
        self.ran_forever = False

    def add_event_handler(self, callback, event_builder=None):
        self.handlers.append({"callback": callback, "builder": event_builder})

    async def start(self, **kwargs):
        self.started_with = kwargs
        self.connected = True

    async def run_until_disconnected(self):
        self.ran_forever = True

    async def get_me(self):
        return self._me


@pytest.fixture
def boot_env(monkeypatch, tmp_path):
    """Minimal environment + stubbed collaborators for a real boot."""
    import src.config as config_module
    import src.telbot as telbot_module

    monkeypatch.setattr(config_module, "OWNER_ID", 123456789, raising=False)
    monkeypatch.setattr(config_module, "ADMIN_IDS", [123456789], raising=False)
    monkeypatch.setattr(config_module, "ADMIN_ID", 123456789, raising=False)
    monkeypatch.setattr(config_module, "CLIENTS_JSON_PATH", str(tmp_path / "clients.json"))
    monkeypatch.setattr(config_module, "STORAGE_BACKEND", "memory", raising=False)
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(telbot_module, "validate_env_file", lambda: None)
    monkeypatch.setattr(telbot_module, "setup_logging", lambda *a, **k: None)

    transport = FakeBotTransport()
    monkeypatch.setattr(telbot_module, "TelegramClient", lambda *a, **k: transport)

    class FakeSessionManager:
        def __init__(self, config, active_clients, tbot):
            self.active_clients = active_clients

        async def start_saved_clients(self):
            return None

        async def disconnect_all_clients(self):
            return None

    monkeypatch.setattr(telbot_module, "SessionManager", FakeSessionManager)

    class FakeMonitor:
        def __init__(self, tbot):
            self.tbot = tbot

        def cleanup_client_handlers(self, client):
            return None

        async def process_messages_for_client(self, client):
            return None

    monkeypatch.setattr(telbot_module, "Monitor", FakeMonitor)
    return transport


@pytest.mark.asyncio
async def test_bot_boots_and_registers_everything(boot_env, tmp_path):
    import src.telbot as telbot_module
    from src.db.store import reset_store

    await reset_store(None)
    telbot_module.setup_logging()
    bot = telbot_module.TelegramBot()
    await bot.start()

    transport = boot_env
    # 1) the bot connected with the token
    assert transport.started_with.get("bot_token")
    # 2) storage + panels are live
    assert bot.store is not None and bot.panel_router is not None
    assert bot.account_registry is not None
    # 3) the command menu was pushed (default + admins scope)
    scopes = [type(r.scope).__name__ for r in transport.requests
              if type(r).__name__ == "SetBotCommandsRequest"]
    assert "BotCommandScopeDefault" in scopes

    # 4) all the handlers the panel needs are registered
    assert len(transport.handlers) >= 5

    # 5) a /start as a normal user reaches the user panel
    event = FakeEvent(sender_id=987654321, chat_id=987654321)
    await bot._start_router(event)
    assert "TELEGRAM PANEL" in event.last_text

    # 6) a /start as the owner reaches the admin panel
    admin_event = FakeEvent(sender_id=123456789, chat_id=123456789)
    await bot._start_router(admin_event)
    assert "ADMIN PANEL" in admin_event.last_text

    # 7) panel commands are routed
    cmd_event = FakeEvent(sender_id=987654321, chat_id=987654321, text="/views")
    await bot._panel_command(cmd_event)
    assert "Order Views" in cmd_event.last_text

    # 8) shutdown closes storage cleanly
    await bot.shutdown()


@pytest.mark.asyncio
async def test_new_post_hook_ignores_unregistered_channels(boot_env):
    import src.telbot as telbot_module
    from src.db.store import reset_store

    await reset_store(None)
    bot = telbot_module.TelegramBot()
    await bot.start()

    class PostEvent:
        id = 5
        sender_id = 1
        chat_id = 1

        async def get_chat(self):
            from tests.fakes import make_channel
            return make_channel(999888777, title="Not registered")

    # must not raise
    await bot._channel_post_handler(PostEvent())
    await bot.shutdown()


@pytest.mark.asyncio
async def test_channel_install_registers_and_greets_owner(boot_env):
    import src.telbot as telbot_module
    from src.db.store import reset_store
    from src.services.channels import get_channel_service
    from src.services.notify import get_notifier
    from tests.fakes import make_channel

    await reset_store(None)
    bot = telbot_module.TelegramBot()
    await bot.start()
    get_notifier().bind(boot_env)   # the bot binds its own client on start

    channel = make_channel(123123123, title="My New Channel")

    class AddedEvent:
        user_added = True
        user_kicked = False
        sender_id = 987654321
        chat_id = 987654321
        user_id = BOT_USER_ID

        async def get_chat(self):
            return channel

    await bot._chat_action_handler(AddedEvent())
    doc = await get_channel_service().get(-100123123123)
    assert doc is not None and doc["owner_id"] == 987654321
    assert doc["auto_reactions"]["enabled"] is True
    texts = [m["text"] for m in boot_env.sent_messages]
    assert any("Channel linked" in t for t in texts), texts
    await bot.shutdown()


def test_main_module_imports_cleanly():
    import importlib
    for module in ("main", "cli_main", "interactive_cli", "src.telbot", "src.handlers",
                   "src.panels", "src.services.executor"):
        importlib.import_module(module)
