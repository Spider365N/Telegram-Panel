"""
Optional health/status HTTP endpoint.

Railway (and Render, Fly, Koyeb …) expect a service to listen on ``$PORT``.
When ``PORT`` or ``HEALTH_PORT`` is set the bot starts this tiny read-only
server; on a plain VPS neither variable exists so nothing is started and the
bot behaves exactly as before.

Endpoints
---------
``GET /``        → human-readable status page (also what Railway's checker hits)
``GET /health``  → JSON ``{"status": "ok", ...}`` with pool/order details
``GET /healthz`` / ``GET /ping`` → same JSON, for whichever name your platform expects
``GET /admin``   → brutalist web admin panel (login: operator id + ADMIN_PANEL_PASSWORD)

Set ``WEB_ADMIN=0`` to serve only the health endpoints.

Every handler swallows its own errors and answers ``200`` as long as the process
is alive, so a missing database or an empty account pool can never cause a
restart loop.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VERSION = "3.5.1"


class HealthServer:
    """A very small aiohttp app that reports what the bot is doing."""

    def __init__(self, port: int, host: str = "0.0.0.0", bot: Any = None):
        self.port = int(port)
        self.host = host or "0.0.0.0"
        self.bot = bot
        self._runner = None
        self._site = None
        self.started_at = time.time()

    # ------------------------------------------------------------------
    async def status(self) -> Dict[str, Any]:
        """Collect a snapshot of the running bot (never raises)."""
        payload: Dict[str, Any] = {
            "status": "ok",
            "service": "telegram-panel",
            "version": VERSION,
            "uptime_seconds": round(time.time() - self.started_at, 1),
        }
        bot = self.bot
        if bot is None:
            return payload

        store = getattr(bot, "store", None)
        payload["storage"] = type(store).__name__ if store is not None else "not-ready"

        registry = getattr(bot, "account_registry", None)
        if registry is not None:
            try:
                stats = await registry.stats()
                payload["accounts"] = {
                    "live": stats.get("live", 0),
                    "total": stats.get("total", 0),
                    "revoked": stats.get("revoked", 0),
                    "inactive": stats.get("inactive", 0),
                }
            except Exception as exc:  # pragma: no cover - defensive
                payload["accounts"] = {"error": str(exc)[:120]}
        try:
            payload["active_clients"] = len(getattr(bot, "active_clients", {}) or {})
        except Exception:  # pragma: no cover - defensive
            pass
        if store is not None:
            try:
                payload["users"] = await store.count("users")
                payload["orders"] = await store.count("orders")
                payload["channels"] = await store.count("channels")
            except Exception:  # pragma: no cover - store may not be ready yet
                pass
        return payload

    async def _json(self, request):
        from aiohttp import web

        body = await self.status()
        return web.json_response(body, dumps=_dumps)

    async def _index(self, request):
        from aiohttp import web

        body = await self.status()
        accounts = body.get("accounts") or {}
        text = (
            "Telegram Panel is running ✅\n"
            f"version: {body.get('version')}\n"
            f"uptime:  {body.get('uptime_seconds')}s\n"
            f"storage: {body.get('storage')}\n"
            f"accounts: {accounts.get('live', '?')} live / {accounts.get('total', '?')} total\n"
            f"users:   {body.get('users', '?')}   orders: {body.get('orders', '?')}\n\n"
            "JSON status: /health\n"
        )
        return web.Response(text=text, content_type="text/plain")

    # ------------------------------------------------------------------
    async def start(self) -> bool:
        """Start the server. Returns True when it is listening."""
        if self.port <= 0:
            return False
        try:
            from aiohttp import web
        except ImportError:  # pragma: no cover - aiohttp is a dependency
            logger.warning("aiohttp not installed — health endpoint disabled")
            return False

        app = web.Application()
        handler = self._json
        for path in ("/health", "/healthz", "/ping", "/status"):
            app.router.add_get(path, handler)
        app.router.add_get("/", self._index)

        # Brutalist web admin panel (same loop, same services → live sync).
        try:
            web_admin_setting = (os.getenv("WEB_ADMIN", "1") or "1").strip().lower()
            if web_admin_setting not in {"0", "off", "no", "false"}:
                from src.webadmin import get_web_admin

                get_web_admin(self.bot).install(app)
        except Exception as exc:  # noqa: BLE001 - the health endpoint must survive
            logger.warning(f"Web admin panel disabled: {exc}")

        try:
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self.host, self.port)
            await self._site.start()
            logger.info(f"Health endpoint listening on http://{self.host}:{self.port}/health")
            return True
        except Exception as exc:
            logger.warning(f"Health endpoint could not start on port {self.port}: {exc}")
            await self.stop()
            return False

    async def stop(self) -> None:
        self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug(f"Health endpoint cleanup failed: {exc}")
            self._runner = None


def _dumps(value: Any, *args, **kwargs) -> str:
    import json

    return json.dumps(value, default=str)


def health_port_from_env() -> int:
    """PORT wins over HEALTH_PORT; 0 disables the endpoint."""
    import os

    for name in ("PORT", "HEALTH_PORT"):
        raw = (os.getenv(name) or "").strip().strip("'\"")
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return 0
