import logging
import os
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Create logs directory if it doesn't exist
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# Setup logging configuration
def setup_logging(log_filename: str = "bot.log") -> None:
    """Sets up logging: a file (when writable) plus stdout.

    Stdout matters on hosted platforms — Railway/Space/Docker colour anything
    written to stderr as an *error* even when it is an INFO line, which used to
    make a perfectly healthy boot look broken.
    """
    import sys
    # Sanitize log filename to prevent path traversal
    import re
    log_filename = re.sub(r'[^\w\-_\.]', '', log_filename)
    if not log_filename.endswith('.log'):
        log_filename = "bot.log"
    
    log_path = os.path.join(LOG_DIR, log_filename)
    # Ensure the resolved path is still within LOG_DIR
    if not os.path.abspath(log_path).startswith(os.path.abspath(LOG_DIR)):
        log_path = os.path.join(LOG_DIR, "bot.log")
    
    handlers = []
    try:
        handlers.append(RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        ))
    except OSError as exc:  # read-only filesystem (some hosts) — stdout is enough
        logging.getLogger(__name__).warning(f"File logging disabled: {exc}")
    try:
        stream = logging.StreamHandler(sys.stdout)
    except Exception:  # pragma: no cover - defensive
        stream = logging.StreamHandler()
    handlers.append(stream)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    # Third-party noise nobody needs in production
    for noisy, level in (("telethon", logging.WARNING), ("asyncio", logging.WARNING),
                         ("pymongo", logging.WARNING), ("motor", logging.WARNING)):
        logging.getLogger(noisy).setLevel(level)

    # Telethon logs a WARNING every 90 seconds for its normal reconnect cycle
    # ("Server closed the connection: 0 bytes read…"). The production log had
    # ~200 of those lines and they hid the real problems, so they are dropped
    # here while genuine errors keep going through.
    class _NoiseFilter(logging.Filter):
        NOISE = (
            "server closed the connection",
            "server resent the older message",
            "retrying to send",
            "telegram is having internal issues",
            "sleeping for",
            "got difference for",
        )

        def filter(self, record: logging.LogRecord) -> bool:
            try:
                message = record.getMessage().lower()
            except Exception:
                return True
            return not any(needle in message for needle in self.NOISE)

    noise = _NoiseFilter()
    for handler in handlers:
        handler.addFilter(noise)
    for name in ("telethon", "telethon.network", "telethon.client.updates",
                 "telethon.network.mtprotosender"):
        logging.getLogger(name).addFilter(noise)