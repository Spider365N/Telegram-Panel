"""The VPS uses a private JSON runtime config instead of a .env file."""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture(autouse=True)
def _restore_config_module_after_env_patch(monkeypatch):
    """Undo JSON loader's direct os.environ writes between tests."""
    import os

    original_env = dict(os.environ)
    yield
    monkeypatch.undo()
    for key in MANAGED_KEYS:
        if key in original_env:
            os.environ[key] = original_env[key]
        else:
            os.environ.pop(key, None)
    import src.config as config_module
    importlib.reload(config_module)


MANAGED_KEYS = {
    "API_ID", "API_HASH", "BOT_TOKEN", "BOT_SESSION_NAME", "ADMIN_ID", "OWNER_ID",
    "ADMIN_IDS", "CHANNEL_ID", "MONGODB_URI", "MONGODB_DB", "STORAGE_BACKEND",
    "DATA_DIR", "CLIENTS_JSON_PATH", "RATE_LIMIT_SLEEP", "GROUPS_BATCH_SIZE",
    "GROUPS_UPDATE_SLEEP", "HTTP_PORT", "HTTPS_PORT", "TELEGRAM_PORT",
    "REPORT_CHECK_BOT", "PORT", "HEALTH_PORT", "HEALTH_HOST", "WEB_ADMIN",
    "ADMIN_PANEL_PASSWORD", "ADMIN_PANEL_SECRET", "SUPPORT_USERNAME", "PAYMENT_UPI_ID",
    "PAYMENT_UPI_NAME", "CURRENCY", "CONFIG_FILE",
}


def _reload_with_config(monkeypatch, config_path, *, overrides=None):
    for key in MANAGED_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CONFIG_FILE", str(config_path))
    for key, value in (overrides or {}).items():
        monkeypatch.setenv(key, value)
    import src.config as config_module
    return importlib.reload(config_module)


def test_json_runtime_config_loads_and_normalizes_admin_ids(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "API_ID": 12345,
        "API_HASH": "api-hash",
        "BOT_TOKEN": "bot-token",
        "OWNER_ID": 111,
        "ADMIN_IDS": [222, "333"],
        "DATA_DIR": str(tmp_path / "data"),
        "CLIENTS_JSON_PATH": str(tmp_path / "data" / "clients.json"),
        "WEB_ADMIN": True,
        "ADMIN_PANEL_PASSWORD": "a-unique-password-with-more-than-16-chars",
        "ADMIN_PANEL_SECRET": "f" * 64,
    }), encoding="utf-8")

    cfg = _reload_with_config(monkeypatch, path)

    assert cfg.CONFIG_FILE_LOADED is True
    assert cfg.API_ID == 12345
    assert cfg.BOT_TOKEN == "bot-token"
    assert cfg.OWNER_ID == 111
    assert cfg.ADMIN_IDS == [111, 222, 333]
    assert cfg.DATA_DIR == str(tmp_path / "data")
    assert cfg.CLIENTS_JSON_PATH == str(tmp_path / "data" / "clients.json")


def test_environment_values_override_json_values(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "API_ID": 12345,
        "API_HASH": "api-hash",
        "BOT_TOKEN": "json-token",
        "ADMIN_ID": 77,
    }), encoding="utf-8")

    cfg = _reload_with_config(monkeypatch, path, overrides={"BOT_TOKEN": "environment-token"})

    assert cfg.BOT_TOKEN == "environment-token"
    assert cfg.ADMIN_ID == 77


def test_bad_json_and_unknown_keys_fail_fast(tmp_path):
    import src.config as config_module

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"API_ID":', encoding="utf-8")
    with pytest.raises(ValueError, match="Could not read runtime config"):
        config_module.load_runtime_config_file(str(malformed))

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"UNSUPPORTED_SECRET": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown runtime config key"):
        config_module.load_runtime_config_file(str(unknown))


def test_explicit_missing_config_is_not_silently_ignored(tmp_path):
    import src.config as config_module

    with pytest.raises(FileNotFoundError, match="Runtime config file not found"):
        config_module.load_runtime_config_file(str(tmp_path / "missing.json"))


def test_config_example_is_valid_and_has_no_real_credentials():
    from pathlib import Path

    example_path = Path(__file__).resolve().parents[1] / "config.example.json"
    example = json.loads(example_path.read_text(encoding="utf-8"))

    assert example["HEALTH_PORT"] == 8070
    assert example["API_ID"] == 0
    assert example["BOT_TOKEN"] == "your_bot_token_here"
    assert example["ADMIN_PANEL_PASSWORD"].startswith("CHANGE_ME")
    assert example["ADMIN_PANEL_SECRET"].startswith("CHANGE_ME")
