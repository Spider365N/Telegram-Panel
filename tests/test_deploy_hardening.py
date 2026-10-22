"""Static/offline checks for the VPS Compose security and persistence setup."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose():
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))


def test_admin_panel_uses_requested_port_and_is_loopback_only():
    bot = _compose()["services"]["bot"]
    assert bot["ports"] == ["127.0.0.1:8070:8070"]
    assert bot["environment"]["HEALTH_PORT"] == "8070"
    assert bot["environment"]["HEALTH_HOST"] == "0.0.0.0"


def test_compose_persists_state_and_waits_for_mongodb():
    compose = _compose()
    bot = compose["services"]["bot"]
    mongo = compose["services"]["mongo"]

    assert "mongo_data:/data/db" in mongo["volumes"]
    assert "bot_data:/app/data" in bot["volumes"]
    assert "bot_logs:/app/logs" in bot["volumes"]
    assert bot["depends_on"]["mongo"]["condition"] == "service_healthy"
    assert "mongo_data" in compose["volumes"]
    assert "bot_data" in compose["volumes"]


def test_bot_container_is_hardened_and_has_bounded_resources():
    bot = _compose()["services"]["bot"]
    assert bot["read_only"] is True
    assert "ALL" in bot["cap_drop"]
    assert "no-new-privileges:true" in bot["security_opt"]
    assert bot["cpus"] == 3.25
    assert bot["mem_limit"] == "8g"
    assert "127.0.0.1:8070:8070" in bot["ports"]
    assert "max-size" in bot["logging"]["options"]


def test_entrypoint_shell_syntax_and_privilege_drop():
    entrypoint = ROOT / "docker-entrypoint.sh"
    subprocess.run(["sh", "-n", str(entrypoint)], check=True)
    text = entrypoint.read_text(encoding="utf-8")
    assert "CONFIG_SOURCE_FILE" in text
    assert "install -o \"$APP_USER\"" in text
    assert 'exec gosu "$APP_USER" "$@"' in text


def test_dockerfile_creates_unprivileged_account_and_does_not_copy_secrets():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert "useradd --uid 10001" in dockerfile
    assert "gosu" in dockerfile
    assert "COPY docker-entrypoint.sh" in dockerfile
    for secret_pattern in ("config/", "config.json", "clients.json", "*.session", ".env"):
        assert secret_pattern in dockerignore
