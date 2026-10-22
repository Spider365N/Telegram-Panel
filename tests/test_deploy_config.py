"""
Deployment checks — VPS (docker compose) **and** Railway/Render/Fly.

These guard the things that broke a real deploy:
  * Railway rejects a Dockerfile containing persistent-storage/healthcheck
    instructions, so the Dockerfile must stay free of them
  * Railway hands the database over as MONGO_URL / MONGOHOST+… instead of
    MONGODB_URI, and provides $PORT for the health endpoint
  * the container filesystem is ephemeral, so the data directory must be
    auto-detected (a Railway Volume lives at /data)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _fresh_config(monkeypatch, env: dict):
    """Import src.config with a clean environment (and only the given vars)."""
    import importlib

    for key in ("MONGODB_URI", "MONGO_URI", "MONGODB_URL", "MONGO_URL", "MONGOHOST",
                "MONGO_HOST", "MONGOPORT", "MONGO_PORT", "MONGOUSER", "MONGO_USER",
                "MONGOPASSWORD", "MONGO_PASSWORD", "MONGO_DB", "MONGODB_DB",
                "MONGO_DATABASE", "DATA_DIR", "PORT", "HEALTH_PORT", "HEALTH_HOST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import src.config as config_module
    return importlib.reload(config_module)


class TestDockerfile:
    def test_has_no_railway_forbidden_instructions(self):
        """Railway: 'docker VOLUME at Line N is not supported' — keep both out."""
        text = ROOT.joinpath("Dockerfile").read_text()
        instruction_lines = [
            line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not [line for line in instruction_lines
                    if re.match(r"^(VOLUME|HEALTHCHECK)\b", line, re.I)], instruction_lines

    def test_parses_and_runs_the_bot(self):
        pytest.importorskip("dockerfile_parse", reason="dev-only Dockerfile parser")
        from dockerfile_parse import DockerfileParser

        parser = DockerfileParser(path=str(ROOT))
        assert parser.baseimage.startswith("python:3.11")
        assert parser.cmd == '["python", "main.py"]'

    def test_installs_requirements_and_copies_the_source(self):
        text = ROOT.joinpath("Dockerfile").read_text()
        assert "requirements.txt" in text
        assert re.search(r"^COPY \. \.$", text, re.M)

    def test_dockerignore_keeps_junk_and_secrets_out(self):
        ignored = ROOT.joinpath(".dockerignore").read_text().split()
        for entry in (".venv", ".git", "__pycache__", ".env", "*.session", "tests"):
            assert entry in ignored, entry


class TestRailwayConfig:
    def test_railway_json_is_valid_and_points_at_the_dockerfile(self):
        data = json.loads(ROOT.joinpath("railway.json").read_text())
        assert data["build"]["builder"] == "DOCKERFILE"
        assert data["build"]["dockerfilePath"] == "Dockerfile"
        assert data["deploy"]["startCommand"] == "python main.py"
        assert data["deploy"]["healthcheckPath"] == "/health"

    def test_nixpacks_fallback_exists(self):
        text = ROOT.joinpath("nixpacks.toml").read_text()
        assert "python main.py" in text

    def test_compose_is_vps_ready(self):
        text = ROOT.joinpath("docker-compose.yml").read_text()
        assert "mongo:7.0" in text and "bot:" in text
        assert "MONGODB_URI: mongodb://mongo:27017" in text
        assert "healthcheck:" in text


class TestMongoUriResolution:
    def test_mongodb_uri_wins(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {
            "MONGODB_URI": "mongodb://primary:27017",
            "MONGO_URL": "mongodb://ignored:27017",
        })
        assert cfg.MONGODB_URI == "mongodb://primary:27017"

    def test_mongo_url_alias(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {
            "MONGO_URL": "mongodb+srv://user:pw@cluster.mongodb.net/?retryWrites=true",
        })
        assert cfg.MONGODB_URI.startswith("mongodb+srv://user:pw@cluster.mongodb.net")

    def test_railway_plugin_parts_are_combined(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {
            "MONGOHOST": "mongo.railway.internal",
            "MONGOPORT": "27017",
            "MONGOUSER": "admin",
            "MONGOPASSWORD": "p@ss word",
            "MONGO_DB": "railway",
        })
        uri = cfg.MONGODB_URI
        assert uri.startswith("mongodb://admin:p%40ss+word@mongo.railway.internal:27017/railway")
        assert "authSource=admin" in uri

    def test_railway_parts_without_credentials(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {"MONGOHOST": "mongo.railway.internal"})
        assert cfg.MONGODB_URI == (
            "mongodb://mongo.railway.internal:27017/telegram_panel?authSource=admin"
        )

    def test_bare_host_is_turned_into_a_uri(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {"MONGO_URL": "mongo:27017"})
        assert cfg.MONGODB_URI == "mongodb://mongo:27017"

    def test_database_name_comes_from_the_uri(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {
            "MONGO_URL": "mongodb://mongo:mongo@mongodb.railway.internal:27017/railway",
        })
        assert cfg.MONGODB_DB == "railway"

    def test_explicit_db_name_beats_the_uri(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {
            "MONGO_URL": "mongodb://host:27017/from_uri",
            "MONGODB_DB": "explicit",
        })
        assert cfg.MONGODB_DB == "explicit"

    def test_atlas_uri_without_path_uses_the_default(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {
            "MONGODB_URI": "mongodb+srv://u:p@cluster.mongodb.net/?retryWrites=true",
        })
        assert cfg.MONGODB_DB == "telegram_panel"

    def test_nothing_set_means_json_fallback(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {})
        assert cfg.MONGODB_URI == ""


class TestDataDir:
    def test_explicit_data_dir_is_respected(self, monkeypatch, tmp_path):
        cfg = _fresh_config(monkeypatch, {"DATA_DIR": str(tmp_path / "custom")})
        assert cfg.DATA_DIR == str(tmp_path / "custom")

    def test_volume_path_is_preferred_when_writable(self, monkeypatch, tmp_path):
        """On Railway the Volume is mounted at /data — that must be picked up."""
        cfg = _fresh_config(monkeypatch, {})
        assert cfg.DATA_DIR in ("/data", "data", os.path.join("/tmp", "telegram_panel"))
        # and the chosen path really is writable
        probe = Path(cfg.DATA_DIR) / ".probe"
        probe.write_text("ok")
        assert probe.read_text() == "ok"
        probe.unlink()


class TestHealthEndpoint:
    def test_default_is_disabled(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {})
        assert cfg.HEALTH_PORT == 0

    def test_port_env_enables_it(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {"PORT": "8080"})
        assert cfg.HEALTH_PORT == 8080

    def test_health_port_env_enables_it(self, monkeypatch):
        cfg = _fresh_config(monkeypatch, {"HEALTH_PORT": "9000"})
        assert cfg.HEALTH_PORT == 9000

    def test_health_port_helper_reads_env(self, monkeypatch):
        from src.health import health_port_from_env
        monkeypatch.setenv("PORT", "1234")
        assert health_port_from_env() == 1234
        monkeypatch.delenv("PORT")
        monkeypatch.setenv("HEALTH_PORT", "4321")
        assert health_port_from_env() == 4321
        monkeypatch.delenv("HEALTH_PORT")
        assert health_port_from_env() == 0

    @pytest.mark.asyncio
    async def test_server_answers_health_and_root(self):
        """Boot the real HTTP server on a free port and query it."""
        import aiohttp

        from src.health import HealthServer
        from tests.fakes import build_stack

        stack = await build_stack()
        server = HealthServer(0, host="127.0.0.1", bot=stack.bot)
        # port 0 → pick a free one ourselves through the OS
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server.port = port
        assert await server.start() is True
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as response:
                    assert response.status == 200
                    payload = await response.json()
                async with session.get(f"http://127.0.0.1:{port}/") as response:
                    assert response.status == 200
                    text = await response.text()
                async with session.get(f"http://127.0.0.1:{port}/healthz") as response:
                    assert response.status == 200
        finally:
            await server.stop()

        assert payload["status"] == "ok"
        assert payload["version"]
        assert payload["storage"]
        assert "accounts" in payload and "live" in payload["accounts"]
        assert "Telegram Panel is running" in text

    @pytest.mark.asyncio
    async def test_web_admin_false_disables_panel_routes(self, monkeypatch):
        import aiohttp
        import socket

        from src.health import HealthServer
        from tests.fakes import build_stack

        monkeypatch.setenv("WEB_ADMIN", "false")
        stack = await build_stack()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = HealthServer(port, host="127.0.0.1", bot=stack.bot)
        assert await server.start() is True
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/health") as health_response:
                    assert health_response.status == 200
                async with session.get(f"http://127.0.0.1:{port}/admin", allow_redirects=False) as admin_response:
                    assert admin_response.status == 404
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_disabled_server_is_a_noop(self):
        from src.health import HealthServer

        server = HealthServer(0, bot=None)
        assert await server.start() is False
        await server.stop()

    @pytest.mark.asyncio
    async def test_status_survives_a_bot_without_services(self):
        """A half-initialised bot must still answer 200 (no restart loops)."""
        from src.health import HealthServer

        class Bare:
            store = None
            account_registry = None
            active_clients = {}

        payload = await HealthServer(8080, bot=Bare()).status()
        assert payload["status"] == "ok"
        assert payload["storage"] == "not-ready"


class TestBotWiring:
    @pytest.mark.asyncio
    async def test_bot_starts_and_stops_the_endpoint(self, monkeypatch):
        import importlib

        import src.config as config_module
        import src.telbot as telbot_module
        from src.db.store import reset_store

        monkeypatch.setenv("PORT", "18099")
        importlib.reload(config_module)
        try:
            # boot the real TelegramBot with the fake transport
            from tests.test_bot_boot import FakeBotTransport
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

            class FakeMonitor:
                def __init__(self, tbot):
                    self.tbot = tbot

                def cleanup_client_handlers(self, client):
                    return None

            monkeypatch.setattr(telbot_module, "SessionManager", FakeSessionManager)
            monkeypatch.setattr(telbot_module, "Monitor", FakeMonitor)

            await reset_store(None)
            bot = telbot_module.TelegramBot()
            await bot.start()
            assert bot.health_server is not None
            payload = await bot.health_server.status()
            assert payload["status"] == "ok"
            await bot.shutdown()
            assert bot.health_server._runner is None
        finally:
            monkeypatch.delenv("PORT", raising=False)
            importlib.reload(config_module)


def test_deploy_docs_mention_both_targets():
    readme = ROOT.joinpath("README.md").read_text().lower()
    assert "railway" in readme
    assert "docker compose up -d --build" in readme
