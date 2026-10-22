"""
Environment resolution + start-up validation.

Written after a real Railway deploy crashed in a loop with:

    ❌ Environment configuration error
       • ADMIN_ID: Your Telegram User ID (get from @userinfobot)

…while the service actually had ``OWNER_ID`` and ``ADMIN_IDS`` set. Two things
had to change and are guarded here:

1. an owner may be given as ``ADMIN_ID`` **or** ``OWNER_ID`` **or** the first
   entry of ``ADMIN_IDS`` — any one of them is enough to start;
2. dashboard values may arrive quoted / padded / with a trailing newline
   (Railway's raw editor does this), so they are sanitised before parsing.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MANAGED = (
    "API_ID", "API_HASH", "BOT_TOKEN", "ADMIN_ID", "OWNER_ID", "ADMIN_IDS",
    "MONGODB_URI", "MONGO_URI", "MONGODB_URL", "MONGO_URL", "MONGODB_DB",
    "MONGO_DB", "MONGO_DATABASE", "MONGOHOST", "MONGOPORT", "MONGOUSER",
    "MONGOPASSWORD", "STORAGE_BACKEND", "DATA_DIR", "PORT", "HEALTH_PORT",
    "HEALTH_HOST", "CHANNEL_ID", "RAILWAY_ENVIRONMENT", "RENDER", "FLY_APP_NAME",
)


def reload_config(monkeypatch, env: dict):
    """Import src.config in a clean environment containing only *env*."""
    for key in MANAGED:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import src.config as config_module
    return importlib.reload(config_module)


# The exact shape of the failing Railway deployment (secrets replaced).
RAILWAY_DEPLOY = {
    "API_ID": "25396020",
    "API_HASH": "228ea638bed51dd4ae3cc9e4e51e198c",
    "BOT_TOKEN": "8866247571:AAFQrdk3PPLwasB64rqitEGZwQULkiKjmjc",
    "BOT_SESSION_NAME": "bot_session_reac",
    "CHANNEL_ID": "-1003852827894",
    "OWNER_ID": "8707101133",          # ← no ADMIN_ID anywhere
    "ADMIN_IDS": "8707101133",
    "MONGODB_URI": "mongodb+srv://user:pass@cluster0.licqd1.mongodb.net/",
    "MONGODB_DB": "telegram_panel_rexn",
    "STORAGE_BACKEND": "auto",
    "HEALTH_PORT": "",
    "HEALTH_HOST": "0.0.0.0",
    "RAILWAY_ENVIRONMENT": "production",
    "PORT": "8080",
}


class TestOwnerResolution:
    def test_owner_id_alone_is_enough(self, monkeypatch):
        """The reproduction case: OWNER_ID set, ADMIN_ID missing."""
        cfg = reload_config(monkeypatch, RAILWAY_DEPLOY)
        assert cfg.ADMIN_ID == 8707101133
        assert cfg.OWNER_ID == 8707101133
        assert cfg.ADMIN_IDS == [8707101133]
        cfg.validate_env_file()          # must not raise

    def test_admin_id_alone_also_works(self, monkeypatch):
        cfg = reload_config(monkeypatch, {**RAILWAY_DEPLOY, "OWNER_ID": "",
                                          "ADMIN_IDS": "", "ADMIN_ID": "555"})
        assert cfg.ADMIN_ID == 555
        assert cfg.OWNER_ID == 555
        cfg.validate_env_file()

    def test_admin_ids_list_alone_also_works(self, monkeypatch):
        cfg = reload_config(monkeypatch, {**RAILWAY_DEPLOY, "OWNER_ID": "",
                                          "ADMIN_IDS": "111,222"})
        assert cfg.ADMIN_ID == 111
        assert cfg.OWNER_ID == 111
        assert cfg.ADMIN_IDS == [111, 222]
        cfg.validate_env_file()

    def test_explicit_admin_id_wins_and_is_added_to_the_list(self, monkeypatch):
        cfg = reload_config(monkeypatch, {**RAILWAY_DEPLOY, "ADMIN_ID": "999"})
        assert cfg.ADMIN_ID == 999
        assert 999 in cfg.ADMIN_IDS

    def test_owner_id_beats_admin_id_for_ownership(self, monkeypatch):
        cfg = reload_config(monkeypatch, {**RAILWAY_DEPLOY, "ADMIN_ID": "123",
                                          "OWNER_ID": "456"})
        assert cfg.OWNER_ID == 456
        assert set(cfg.ADMIN_IDS) >= {123, 456}

    def test_semicolons_and_spaces_are_accepted(self, monkeypatch):
        cfg = reload_config(monkeypatch, {**RAILWAY_DEPLOY, "ADMIN_IDS": "111; 222 ,333"})
        for extra in (111, 222, 333):
            assert extra in cfg.ADMIN_IDS
        # every listed admin plus the owner, nothing dropped
        assert len(cfg.ADMIN_IDS) == len({111, 222, 333, cfg.OWNER_ID})


class TestValidation:
    def test_missing_everything_names_the_variables(self, monkeypatch):
        cfg = reload_config(monkeypatch, {})
        with pytest.raises(ValueError) as exc:
            cfg.validate_env_file()
        message = str(exc.value)
        for name in ("API_ID", "API_HASH", "BOT_TOKEN"):
            assert name in message
        assert "ADMIN_ID" in message and "OWNER_ID" in message

    def test_platform_message_points_at_the_dashboard(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"RAILWAY_ENVIRONMENT": "production"})
        with pytest.raises(ValueError) as exc:
            cfg.validate_env_file()
        assert "Variables" in str(exc.value)
        assert "cp env.example .env" not in str(exc.value)

    def test_self_hosted_message_points_at_dotenv(self, monkeypatch):
        cfg = reload_config(monkeypatch, {})
        with pytest.raises(ValueError) as exc:
            cfg.validate_env_file()
        assert "cp env.example .env" in str(exc.value)

    def test_placeholders_are_treated_as_missing(self, monkeypatch):
        cfg = reload_config(monkeypatch, {
            "API_ID": "your_api_id_here", "API_HASH": "x",
            "BOT_TOKEN": "your_bot_token_here", "OWNER_ID": "123",
        })
        with pytest.raises(ValueError) as exc:
            cfg.validate_env_file()
        assert "API_ID" in str(exc.value)

    def test_valid_deployment_passes(self, monkeypatch):
        cfg = reload_config(monkeypatch, RAILWAY_DEPLOY)
        cfg.validate_env_file()


class TestQuotedValues:
    def test_numbers_strip_quotes_and_spaces(self, monkeypatch):
        cfg = reload_config(monkeypatch, {
            **RAILWAY_DEPLOY,
            "ADMIN_IDS": ' "8707101133, 999" ',
            "HEALTH_PORT": '"9090"',
        })
        assert cfg.ADMIN_IDS == [999, 8707101133]
        assert cfg.HEALTH_PORT == 9090

    def test_empty_string_quotes_mean_unset(self, monkeypatch):
        cfg = reload_config(monkeypatch, {
            **RAILWAY_DEPLOY, "HEALTH_PORT": '""', "DATA_DIR": "''",
        })
        assert cfg.HEALTH_PORT == 8080          # falls back to $PORT
        assert cfg.DATA_DIR != "''"

    def test_connection_string_keeps_its_scheme(self, monkeypatch):
        """A quoted/newline-wrapped URI must not become mongodb://"mongodb+srv…"""
        cfg = reload_config(monkeypatch, {
            **RAILWAY_DEPLOY,
            "MONGODB_URI": '"mongodb+srv://u:p@c.mongodb.net/\n"',
        })
        assert cfg.MONGODB_URI.startswith("mongodb+srv://u:p@c.mongodb.net/")
        assert '"' not in cfg.MONGODB_URI
        assert "\n" not in cfg.MONGODB_URI
        assert cfg.MONGODB_DB == "telegram_panel_rexn"

    def test_database_from_quoted_uri(self, monkeypatch):
        cfg = reload_config(monkeypatch, {
            "MONGO_URL": '"mongodb://host:27017/mydb"',
            "OWNER_ID": "1",
        })
        assert cfg.MONGODB_URI == "mongodb://host:27017/mydb"
        assert cfg.MONGODB_DB == "mydb"

    def test_clean_env_value_helper(self, monkeypatch):
        cfg = reload_config(monkeypatch, RAILWAY_DEPLOY)
        assert cfg.clean_env_value('"abc"') == "abc"
        assert cfg.clean_env_value("  abc  ") == "abc"
        assert cfg.clean_env_value('"abc\n"') == "abc"
        assert cfg.clean_env_value('""') == ""
        assert cfg.clean_env_value(None) is None
        assert cfg.clean_env_value(5) == 5


class TestRequirements:
    def test_dnspython_is_declared_for_atlas(self):
        """mongodb+srv:// URIs need dnspython — otherwise Atlas cannot connect."""
        text = ROOT.joinpath("requirements.txt").read_text().lower()
        assert "dnspython" in text
        assert "pymongo" in text

    def test_no_quoted_placeholder_for_the_owner(self):
        """env.example must not ship a literal ADMIN_ID placeholder that fails."""
        text = ROOT.joinpath("env.example").read_text()
        assert "OWNER_ID" in text and "ADMIN_IDS" in text
        assert "optional" in text.lower() or "either" in text.lower()


class TestStartupIsQuietAndFailsFast:
    @pytest.mark.asyncio
    async def test_bot_boots_with_the_railway_environment(self, monkeypatch):
        """Full boot with the deployment that used to crash-loop."""
        import importlib as _importlib

        for key in MANAGED:
            monkeypatch.delenv(key, raising=False)
        for key, value in RAILWAY_DEPLOY.items():
            monkeypatch.setenv(key, value)

        import src.config as config_module
        config_module = _importlib.reload(config_module)

        # The fake Atlas host cannot be resolved in CI; point the bot at the
        # local test database when one is available so the boot is realistic.
        test_uri = os.getenv("TEST_MONGODB_URI")
        if test_uri:
            monkeypatch.setenv("MONGODB_URI", f"{test_uri}/telegram_panel_boot_test")

        import src.telbot as telbot_module
        from src.db.store import reset_store
        from tests.test_bot_boot import FakeBotTransport

        monkeypatch.setattr(telbot_module, "validate_env_file", config_module.validate_env_file)
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

        class FakeMonitor:
            def __init__(self, tbot):
                self.tbot = tbot

            def cleanup_client_handlers(self, client):
                return None

        monkeypatch.setattr(telbot_module, "SessionManager", FakeSessionManager)
        monkeypatch.setattr(telbot_module, "Monitor", FakeMonitor)

        await reset_store(None)
        try:
            bot = telbot_module.TelegramBot()
            await bot.start()
            assert bot.services_ready is True
            assert bot.health_server is not None      # $PORT was 8080
            await bot.shutdown()
        finally:
            monkeypatch.delenv("PORT", raising=False)
            monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
            _importlib.reload(config_module)

    @pytest.mark.asyncio
    async def test_config_error_stops_the_retry_loop(self, monkeypatch, caplog):
        """A missing variable must not spin the restart loop."""
        import src.telbot as telbot_module
        from tests.test_bot_boot import FakeBotTransport

        monkeypatch.setattr(telbot_module, "setup_logging", lambda *a, **k: None)
        monkeypatch.setattr(telbot_module, "TelegramClient",
                            lambda *a, **k: FakeBotTransport())

        def broken():
            raise ValueError("❌ Environment configuration error — ADMIN_ID missing")

        monkeypatch.setattr(telbot_module, "validate_env_file", broken)
        bot = telbot_module.TelegramBot()
        await bot.run()          # would loop 5× before the fix
        assert bot.tbot is None or not getattr(bot.tbot, "ran_forever", False)


class TestStorageFallback:
    @pytest.mark.asyncio
    async def test_auto_mode_degrades_to_json_when_mongo_is_unreachable(self, monkeypatch):
        """STORAGE_BACKEND=auto must never crash the bot (the deploy bug)."""
        import importlib

        for key in MANAGED:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("STORAGE_BACKEND", "auto")
        monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1/nope")  # nothing listens
        import src.config as config_module
        config_module = importlib.reload(config_module)

        import src.db.store as store_module
        monkeypatch.setattr(store_module, "_store", None)
        store = await store_module.init_store()
        try:
            assert type(store).__name__ == "JsonStore", type(store).__name__
        finally:
            await store_module.reset_store(None)

    @pytest.mark.asyncio
    async def test_explicit_mongo_mode_still_fails_fast(self, monkeypatch):
        import importlib

        for key in MANAGED:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("STORAGE_BACKEND", "mongo")
        monkeypatch.setenv("MONGODB_URI", "mongodb://127.0.0.1:1/nope")
        import src.config as config_module
        config_module = importlib.reload(config_module)

        import src.db.store as store_module
        monkeypatch.setattr(store_module, "_store", None)
        with pytest.raises(Exception):
            await store_module.init_store()
        await store_module.reset_store(None)

    @pytest.mark.asyncio
    async def test_mongo_mode_without_a_uri_names_the_variable(self, monkeypatch):
        import importlib

        for key in MANAGED:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("STORAGE_BACKEND", "mongo")
        import src.config as config_module
        config_module = importlib.reload(config_module)

        import src.db.store as store_module
        monkeypatch.setattr(store_module, "_store", None)
        with pytest.raises(RuntimeError) as exc:
            await store_module.init_store()
        assert "MONGODB_URI" in str(exc.value) or "MONGO_URL" in str(exc.value)
        await store_module.reset_store(None)
