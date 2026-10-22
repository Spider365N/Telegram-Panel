from __future__ import annotations

import os
import json
from dotenv import load_dotenv
import logging
from typing import Dict, Any, Union, Optional

# Set up logger for the configuration manager
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class ConfigManager:
    def __init__(self, filename: Optional[str] = None, config: Optional[Dict[str, Any]] = None):
        """
        Initialize the ConfigManager class.
        Handles loading, saving, and managing configuration settings.
        :param filename: Path to the configuration file.
        :param config: Initial configuration dictionary (optional).
        """
        try:
            # The legacy client/settings JSON defaults to the configured data
            # directory, so it remains writable in a read-only container.
            # Explicit paths still win for tests and compatibility callers.
            configured_path = filename or os.getenv("CLIENTS_JSON_PATH") or "clients.json"
            self.filepath = os.fspath(os.path.expanduser(str(configured_path)))
            # Sanitize filename for display purposes only
            import re
            sanitized = re.sub(r'[^\w\-_\.]', '', os.path.basename(self.filepath))
            if not sanitized.endswith('.json'):
                sanitized = "config.json"
            self.filename = sanitized
            self.default_config = {
                "TARGET_GROUPS": [],  # List of target groups for the bot
                "KEYWORDS": [],       # List of keywords for filtering
                "IGNORE_USERS": [],   # List of user IDs to ignore
                "clients": {}         # Dictionary of client configurations
            }
            # Load existing configuration or use the provided one
            self.config = config if config else self.load_config()  # type: Dict[str, Any]
            logger.info("ConfigManager initialized successfully.")
        except Exception as e:
            logger.critical(f"Error initializing ConfigManager: {e}")
            raise

    def load_config(self) -> Dict[str, Any]:
        """
        Load configuration from a JSON file.
        If the file doesn't exist or is invalid, the default configuration is used.
        :return: Configuration dictionary.
        """
        try:
            if not os.path.exists(self.filepath):
                # Normal on a fresh deployment: the panel stores its state in
                # MongoDB, clients.json is only a legacy mirror.
                logger.info(
                    f"Config file '{self.filepath}' not found — starting from the "
                    f"defaults (normal on a fresh deployment)"
                )
                return self.default_config.copy()
            
            if os.path.getsize(self.filepath) == 0:
                logger.warning(f"Config file '{self.filepath}' is empty. Using default settings.")
                return self.default_config.copy()

            with open(self.filepath, 'r', encoding='utf-8') as f:
                loaded_config = json.load(f)
                if not isinstance(loaded_config, dict):
                    raise ValueError("Config file must contain a JSON object.")
                logger.info("Config file loaded successfully.")
                # Merge loaded configuration with default values
                return {**self.default_config, **loaded_config}
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.error(f"Error loading config file '{self.filepath}': {e}. Falling back to default config.")
            return self.default_config.copy()

    def save_config(self, config: Dict[str, Any]) -> None:
        """
        Save the current configuration to the JSON file.
        :param config: Configuration dictionary to save.
        """
        try:
            # Ensure directory exists
            target_dir = os.path.dirname(self.filepath) or '.'
            os.makedirs(target_dir, exist_ok=True)
            # Write atomically: serialize to a temp file in the same directory and
            # os.replace() it into place, so a crash/disk-full mid-write can never
            # leave a truncated config that load_config would discard.
            import tempfile
            fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix='.tmp')
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(config, f, indent=4, ensure_ascii=False)
                os.replace(tmp_path, self.filepath)
            except BaseException:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                raise
            # Only update the in-memory copy once the on-disk write succeeded.
            self.config = config.copy()
            logger.info("Configuration saved successfully.")
        except OSError as e:
            logger.error(f"Failed to save config file '{self.filepath}': {e}")

    def update_config(self, key: str, value: Any) -> None:
        """
        Update a specific key in the configuration and save changes.
        :param key: Configuration key to update.
        :param value: New value for the key.
        """
        try:
            if key not in self.default_config:
                logger.warning(f"Key '{key}' not in default configuration. Adding it.")
            self.config[key] = value
            self.save_config(self.config)
        except Exception as e:
            logger.error(f"Error updating config key '{key}': {e}")

    def merge_config(self, new_config: Dict[str, Any]) -> None:
        """
        Merge a new configuration dictionary with the current configuration.
        :param new_config: Dictionary containing configuration updates.
        """
        try:
            # Reload current config from file to ensure we have the latest,
            # then merge into it so externally-changed keys are not lost.
            current_config = self.load_config()
            for key, value in new_config.items():
                if key in current_config and isinstance(current_config[key], list) and isinstance(value, list):
                    # Combine lists while avoiding duplicates but preserving all unique items
                    combined = current_config[key] + value
                    # Remove duplicates while preserving order
                    seen = set()
                    result = []
                    for item in combined:
                        if item not in seen:
                            seen.add(item)
                            result.append(item)
                    current_config[key] = result
                else:
                    current_config[key] = value
            self.config = current_config
            self.save_config(self.config)
        except Exception as e:
            logger.error(f"Error merging config: {e}")

    def get_config(self, key: Optional[str] = None) -> Union[Dict[str, Any], Any]:
        """
        Retrieve a specific configuration value or the entire configuration.
        :param key: Configuration key to retrieve (optional).
        :return: Configuration value or the full configuration dictionary.
        """
        try:
            if key is None:
                return self.config
            return self.config.get(key, None)
        except Exception as e:
            logger.error(f"Error retrieving config key '{key}': {e}")
            return None

# Legacy .env support remains for local/hosted deployments, but the recommended
# Docker/VPS deployment uses config/config.json mounted read-only into the
# container. Process environment variables intentionally take precedence over
# JSON values so deployment-specific values (Mongo hostname, data path, port)
# can be safely fixed in Compose.
load_dotenv()

_CONFIG_ENV_KEYS = frozenset({
    "API_ID", "API_HASH", "BOT_TOKEN", "BOT_SESSION_NAME",
    "ADMIN_ID", "OWNER_ID", "ADMIN_IDS", "CHANNEL_ID",
    "MONGODB_URI", "MONGO_URI", "MONGODB_URL", "MONGO_URL",
    "MONGODB_DB", "MONGO_DB", "MONGO_DATABASE", "MONGOHOST", "MONGOPORT",
    "MONGOUSER", "MONGOPASSWORD", "MONGO_HOST", "MONGO_PORT", "MONGO_USER",
    "MONGO_PASSWORD", "STORAGE_BACKEND", "DATA_DIR", "CLIENTS_JSON_PATH",
    "RATE_LIMIT_SLEEP", "GROUPS_BATCH_SIZE", "GROUPS_UPDATE_SLEEP",
    "HTTP_PORT", "HTTPS_PORT", "TELEGRAM_PORT", "REPORT_CHECK_BOT",
    "PORT", "HEALTH_PORT", "HEALTH_HOST", "WEB_ADMIN",
    "ADMIN_PANEL_PASSWORD", "ADMIN_PANEL_SECRET", "SUPPORT_USERNAME",
    "PAYMENT_UPI_ID", "PAYMENT_UPI_NAME", "CURRENCY",
})


def load_runtime_config_file(path: Optional[str] = None) -> bool:
    """Read scalar deployment settings from JSON into ``os.environ``.

    ``CONFIG_FILE`` may point to an explicit file. Without it, the standard
    ``<project>/config/config.json`` is optional (so tests and legacy .env
    installs still work). If an explicit file is missing or malformed, fail
    immediately rather than silently starting with defaults and risking a
    broken or non-persistent deployment.

    JSON values only fill variables not already provided by the process
    environment. Lists are accepted only for ``ADMIN_IDS`` and become a
    comma-separated string.
    """
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[1]
    requested = path if path is not None else os.getenv("CONFIG_FILE")
    explicit = bool(requested)
    config_path = Path(requested).expanduser() if requested else project_root / "config" / "config.json"
    if not config_path.is_absolute():
        config_path = project_root / config_path

    if not config_path.is_file():
        if explicit:
            raise FileNotFoundError(
                f"Runtime config file not found: {config_path}. "
                "Copy config.example.json to config/config.json and fill in the required values."
            )
        return False

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            values = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read runtime config file {config_path}: {exc}") from exc

    if not isinstance(values, dict):
        raise ValueError(f"Runtime config file must contain a JSON object: {config_path}")

    unknown = sorted(set(values) - _CONFIG_ENV_KEYS)
    if unknown:
        raise ValueError(
            "Unknown runtime config key(s): " + ", ".join(unknown) +
            ". Use the uppercase keys listed in config.example.json."
        )

    for key, value in values.items():
        if value is None:
            rendered = ""
        elif key == "ADMIN_IDS" and isinstance(value, (list, tuple)):
            rendered = ",".join(str(item).strip() for item in value if str(item).strip())
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            rendered = str(value).strip()
        else:
            raise ValueError(f"Runtime config value {key!r} must be a string, number, boolean, or null")

        if key not in os.environ or not str(os.environ.get(key) or "").strip():
            os.environ[key] = rendered

    logger.info("Loaded runtime configuration from %s", config_path)
    return True


CONFIG_FILE_LOADED = load_runtime_config_file()

PLACEHOLDER_VALUES = {
    'x', '0', 'your_api_id_here', 'your_api_hash_here', 'your_bot_token_here',
    'your_admin_user_id_here', 'your_channel_id_or_username',
}


def validate_env_file() -> None:
    """Validate the environment before start-up.

    Only the Telegram API credentials and the bot token are hard requirements;
    an owner/admin is required too, but it may arrive as ``ADMIN_ID``,
    ``OWNER_ID`` or the first entry of ``ADMIN_IDS`` — a deployment that only
    sets one of them is perfectly valid (this is what Railway/Render setups
    usually do).

    Raises ValueError listing everything that is wrong, so the operator sees the
    full picture in one log line instead of one problem per restart.
    """
    required = {
        'API_ID': 'Telegram API ID (https://my.telegram.org/apps)',
        'API_HASH': 'Telegram API Hash (https://my.telegram.org/apps)',
        'BOT_TOKEN': 'Bot token (from @BotFather)',
    }
    missing = []
    for name, description in required.items():
        value = clean_env_value(os.getenv(name) or '')
        if not value or value in PLACEHOLDER_VALUES:
            missing.append(f"  • {name}: {description}")

    owner_value = _first_id(
        clean_env_value(os.getenv('OWNER_ID') or ''),
        clean_env_value(os.getenv('ADMIN_ID') or ''),
        _parse_id_list(os.getenv('ADMIN_IDS') or ''),
    )
    if not owner_value:
        missing.append(
            "  • ADMIN_ID (or OWNER_ID / ADMIN_IDS): your numeric Telegram user id "
            "(get it from @userinfobot)"
        )

    # The production JSON template is private and is the source of truth for the
    # dashboard credentials. Refuse obvious placeholders/weak values rather
    # than exposing the panel with a predictable password or signing key.
    web_admin_enabled = (clean_env_value(os.getenv("WEB_ADMIN", "1") or "1") or "1").lower() not in {
        "0", "off", "no", "false"
    }
    if CONFIG_FILE_LOADED and web_admin_enabled:
        panel_password = str(clean_env_value(os.getenv("ADMIN_PANEL_PASSWORD") or "") or "")
        if len(panel_password) < 16 or panel_password.lower().startswith(("replace_", "change_me")):
            missing.append("  • ADMIN_PANEL_PASSWORD: set a unique password of at least 16 characters")
        panel_secret = str(clean_env_value(os.getenv("ADMIN_PANEL_SECRET") or "") or "")
        if len(panel_secret) < 32 or panel_secret.lower().startswith(("replace_", "change_me")):
            missing.append("  • ADMIN_PANEL_SECRET: set a random secret of at least 32 characters")

    optional = {
        'CHANNEL_ID': 'Channel id/username for forwarding (optional)',
        'MONGODB_URI': 'MongoDB connection string (recommended, cloud-safe storage)',
    }
    for name, description in optional.items():
        value = clean_env_value(os.getenv(name) or '')
        if not value or value in PLACEHOLDER_VALUES:
            logger.info(f"Optional variable '{name}' not set — {description}")

    if missing:
        on_platform = any(
            os.getenv(var) for var in ('RAILWAY_ENVIRONMENT', 'RAILWAY_PROJECT_ID',
                                       'RENDER', 'FLY_APP_NAME', 'DYNO', 'KOYEB_APP_NAME')
        )
        how_to = (
            "1. Open your service → Variables (Railway/Render dashboard)\n"
            "2. Add the variables listed above\n"
            "3. Redeploy\n"
            if on_platform else
            "1. Copy config.example.json to config/config.json: mkdir -p config && cp config.example.json config/config.json\n"
            "2. Fill in the required JSON values, then restart the bot\n"
            "Legacy installs may still use env.example/.env, but Docker/VPS should use config/config.json.\n"
        )
        error_msg = (
            "\n❌ Environment configuration error\n\n"
            "Missing or invalid variables:\n" + "\n".join(missing) + "\n\n"
            "How to fix it:\n" + how_to
        )
        logger.critical(error_msg)
        raise ValueError(error_msg)

    logger.info("✓ Environment configuration looks good")


# Variables the bot cannot work without: only these log a warning when missing
# (platforms like Railway surface every WARNING as an "error" line, and an
# optional variable such as ADMIN_IDS made the log look broken).
_IMPORTANT_ENV_VARS = {'API_ID', 'API_HASH', 'BOT_TOKEN'}

# Everything else has a sane default and is reported once, at INFO level.
_OPTIONAL_ENV_VARS = {
    'ADMIN_ID', 'OWNER_ID', 'ADMIN_IDS', 'MONGODB_URI', 'MONGO_URL',
    'MONGODB_DB', 'STORAGE_BACKEND', 'DATA_DIR',
}
_DEFAULTED_ENV_VARS: set = set()


def clean_env_value(value: Any) -> Any:
    """Normalise a raw environment value.

    Dashboards (Railway's raw editor, Docker Compose, Render, Heroku …) happily
    store values with quotes, padding whitespace or a stray line break, and all
    of those break naive parsing. Examples:

        '"800000001"'                  -> '800000001'
        '  mongodb+srv://u:p@c/?x=1\n'  -> 'mongodb+srv://u:p@c/?x=1'
        '""'                            -> ''
    """
    if not isinstance(value, str):
        return value
    cleaned = value.replace("\r", "").replace("\n", "").strip()
    for _ in range(3):
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
            cleaned = cleaned[1:-1].strip()
        else:
            break
    return cleaned


def get_env_variable(name: str, default: Optional[Any] = None) -> Any:
    """Retrieve an environment variable, sanitised, with an optional default."""
    try:
        value = os.getenv(name)
        if value is None:
            if name in _IMPORTANT_ENV_VARS:
                logger.warning(f"Environment variable '{name}' not set; using default {default!r}")
            elif name in _OPTIONAL_ENV_VARS:
                if name not in _DEFAULTED_ENV_VARS:
                    _DEFAULTED_ENV_VARS.add(name)
                    logger.info(
                        f"Optional environment variable '{name}' not set — "
                        f"using the default {default!r} (this is normal)"
                    )
                else:
                    logger.debug(f"Environment variable '{name}' not set; using default {default!r}")
            else:
                logger.debug(f"Environment variable '{name}' not set; using default {default!r}")
            return default
        return clean_env_value(value)
    except Exception as e:
        logger.error(f"Error retrieving environment variable '{name}': {e}")
        return default


def get_env_int(name: str, default: int = 0) -> int:
    """
    Retrieve an environment variable as an integer with a default value.
    :param name: Name of the environment variable.
    :param default: Default integer value if the variable is not set or invalid.
    :return: Integer value of the environment variable or the default.
    """
    try:
        value = get_env_variable(name, default=str(default))
        # Only map placeholder credential strings to the default. A real "0"
        # must be honoured (e.g. RATE_LIMIT_SLEEP=0); an empty/invalid string
        # falls through to the ValueError handler below.
        value = clean_env_value(value)
        if value in ['x', 'your_api_id_here', 'your_admin_user_id_here', '']:
            return default
        return int(value)
    except (ValueError, TypeError):
        return default

# Load essential configuration values from environment variables
API_ID = get_env_int('API_ID', default=0)
API_HASH = get_env_variable('API_HASH', default='x')
BOT_TOKEN = get_env_variable('BOT_TOKEN', default='x')
CHANNEL_ID = get_env_variable('CHANNEL_ID', default='x')
BOT_SESSION_NAME = get_env_variable('BOT_SESSION_NAME', default='bot2')
def _first_id(*values: Any) -> int:
    """First usable numeric id among the given values (lists included)."""
    for value in values:
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                try:
                    candidate = int(str(item).strip())
                except (TypeError, ValueError):
                    continue
                if candidate:
                    return candidate
            continue
        try:
            candidate = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if candidate:
            return candidate
    return 0


def _parse_id_list(value: Any) -> list:
    """Parse '123, 456' / '123;456' / a list into a list of unique ints."""
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = str(value).replace(";", ",").split(",")
    out = []
    for item in items:
        try:
            number = int(str(item).strip())
        except (TypeError, ValueError):
            continue
        if number and number not in out:
            out.append(number)
    return out


# ---------------------------------------------------------------------------
# Who is in charge?
# ---------------------------------------------------------------------------
# Any of these may be used; the first one that is set wins:
#     ADMIN_ID  ->  OWNER_ID  ->  the first entry of ADMIN_IDS
# so a deployment that only defines OWNER_ID (or ADMIN_IDS) — like most Railway
# setups — works out of the box instead of refusing to start.
EXTRA_ADMIN_IDS = _parse_id_list(get_env_variable('ADMIN_IDS', default=''))
_OWNER_FROM_ENV = get_env_int('OWNER_ID', default=0)
_ADMIN_FROM_ENV = get_env_int('ADMIN_ID', default=0)
OWNER_ID = _first_id(_OWNER_FROM_ENV, _ADMIN_FROM_ENV, EXTRA_ADMIN_IDS)
ADMIN_ID = _first_id(_ADMIN_FROM_ENV, OWNER_ID, EXTRA_ADMIN_IDS)
ADMIN_IDS = sorted({OWNER_ID, ADMIN_ID, *EXTRA_ADMIN_IDS} - {0})
CLIENTS_JSON_PATH = str(get_env_variable('CLIENTS_JSON_PATH', default='clients.json'))
RATE_LIMIT_SLEEP = get_env_int('RATE_LIMIT_SLEEP', default=60)
GROUPS_BATCH_SIZE = get_env_int('GROUPS_BATCH_SIZE', default=10)
GROUPS_UPDATE_SLEEP = get_env_int('GROUPS_UPDATE_SLEEP', default=60)

# Load port configurations from environment variables
PORTS = {
    "HTTP": get_env_int('HTTP_PORT', default=80),
    "HTTPS": get_env_int('HTTPS_PORT', default=443),
    "TELEGRAM": get_env_int('TELEGRAM_PORT', default=443)
}

# Report check bot configuration
REPORT_CHECK_BOT = get_env_variable('REPORT_CHECK_BOT', default='')

# ---------------------------------------------------------------------------
# Persistence / database configuration
# ---------------------------------------------------------------------------
# When a MongoDB connection is configured every piece of state (signed-in
# accounts, users, orders, wallet balances, settings, entity access-hashes) lives
# in MongoDB so a crash, a redeploy or a rebuilt container cannot lose data.
# Leave it empty to fall back to the local JSON store (still crash-safe, but
# single-machine only).
#
# Accepted variable names, in order:
#   MONGODB_URI / MONGO_URI / MONGODB_URL / MONGO_URL   (Atlas, Railway, Render…)
#   …or the Railway MongoDB plugin parts:
#     MONGOHOST + MONGOPORT + MONGOUSER + MONGOPASSWORD (+ MONGO_DB / MONGODB_DB)
# Railway also exposes MONGOHOST as a private hostname; that is used as-is.
def _env(name: str) -> str:
    """Read one environment variable, sanitised (quotes/newlines removed)."""
    return str(clean_env_value(os.getenv(name) or "") or "")


def _build_mongo_uri() -> str:
    """Resolve the MongoDB connection string from any common env var style."""
    for name in ("MONGODB_URI", "MONGO_URI", "MONGODB_URL", "MONGO_URL"):
        raw = _env(name)
        if raw:
            if raw.startswith("mongodb://") or raw.startswith("mongodb+srv://"):
                return raw
            # Some hosts hand out a bare host:port
            return f"mongodb://{raw}"

    host = _env("MONGOHOST") or _env("MONGO_HOST")
    if not host:
        return ""
    port = _env("MONGOPORT") or _env("MONGO_PORT") or "27017"
    user = _env("MONGOUSER") or _env("MONGO_USER")
    password = _env("MONGOPASSWORD") or _env("MONGO_PASSWORD")
    database = (
        _env("MONGO_DB") or _env("MONGODB_DB") or _env("MONGO_DATABASE") or "telegram_panel"
    )
    credentials = ""
    if user and password:
        from urllib.parse import quote_plus
        credentials = f"{quote_plus(user)}:{quote_plus(password)}@"
    # authSource=admin is what the Railway MongoDB plugin needs
    return f"mongodb://{credentials}{host}:{port}/{database}?authSource=admin"


def _database_from_uri(uri: str) -> str:
    """Extract the database name from a MongoDB URI ('' when absent)."""
    if not uri:
        return ""
    tail = uri.split("://", 1)[-1]
    if "/" not in tail:
        return ""
    tail = tail.split("/", 1)[1]
    tail = tail.split("?", 1)[0].strip()
    return tail


def _resolve_mongodb_db(uri: str) -> str:
    """Database name: explicit var wins, then the URI path, then the default."""
    for name in ("MONGODB_DB", "MONGO_DB", "MONGO_DATABASE"):
        value = _env(name)
        if value:
            return value
    from_uri = _database_from_uri(uri)
    if from_uri:
        return from_uri
    return "telegram_panel"


MONGODB_URI = _build_mongo_uri() or get_env_variable('MONGODB_URI', default='')
MONGODB_DB = _resolve_mongodb_db(MONGODB_URI)

# auto  -> MongoDB when MONGODB_URI is set and reachable, else JSON fallback
# mongo -> insist on MongoDB (fail fast if unreachable)
# json  -> always use the local JSON store
# memory-> in-process only (used by tests / dry runs)
STORAGE_BACKEND = get_env_variable('STORAGE_BACKEND', default='auto').strip().lower()


def _default_data_dir() -> str:
    """Pick a writable data directory.

    Priority: an explicit ``DATA_DIR``, then ``/data`` when it exists and is
    writable (Railway mounts its volume there), then ``./data``.
    """
    explicit = _env("DATA_DIR")
    if explicit:
        return explicit
    import tempfile
    preferred = ["/data", "data", os.path.join(tempfile.gettempdir(), "telegram_panel")]
    for candidate in preferred:
        try:
            os.makedirs(candidate, exist_ok=True)
            probe = os.path.join(candidate, ".write_test")
            with open(probe, "w") as handle:
                handle.write("ok")
            os.remove(probe)
            return candidate
        except Exception:
            continue
    return "data"


DATA_DIR = _default_data_dir()

# ---------------------------------------------------------------------------
# Health / web endpoint (Railway, Render, Fly, Koyeb …)
# ---------------------------------------------------------------------------
# Railway (and friends) expect a service to listen on $PORT. When PORT (or
# HEALTH_PORT) is set the bot starts a tiny read-only HTTP status endpoint;
# on a plain VPS nothing changes because neither variable is set.
HEALTH_PORT = get_env_int('HEALTH_PORT', default=0) or get_env_int('PORT', default=0)
HEALTH_HOST = get_env_variable('HEALTH_HOST', default='0.0.0.0') or '0.0.0.0' 

# ---------------------------------------------------------------------------
# Panel contact details
# ---------------------------------------------------------------------------
# OWNER_ID / ADMIN_IDS are resolved earlier, see "Who is in charge?" above.

# Support contact shown in the user panel
SUPPORT_USERNAME = get_env_variable('SUPPORT_USERNAME', default='')
# Payment collection details (manual UPI flow; can also be edited in the panel)
PAYMENT_UPI_ID = get_env_variable('PAYMENT_UPI_ID', default='')
PAYMENT_UPI_NAME = get_env_variable('PAYMENT_UPI_NAME', default='')
# Currency symbol used across the user panel
CURRENCY = get_env_variable('CURRENCY', default='₹')

# Validate environment configuration - only when bot starts, not at import time
# This allows tests to set environment variables via fixtures before validation
# Validation will be called explicitly in main.py or Telbot.__init__

