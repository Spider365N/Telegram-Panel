"""
Wiring smoke test: every service must work the way production builds it.

The Railway log showed `name 'get_store_sync' is not defined` coming from the
settings service — a missing import that only bites when a service is created
*without* an explicit store (i.e. in production, never in most unit tests).

This file builds every service the exact way the bot does at start-up (no
arguments, resolved singletons) and touches the lazy properties that read from
the database, so a missing import or a typo'ed attribute fails here instead of
in a deployment log.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

import pytest
import pytest_asyncio

from src.db.store import MemoryStore, reset_store

SERVICES = [
    ("src.services.settings", "get_settings_service", "reset_settings_service"),
    ("src.services.users", "get_user_service", "reset_user_service"),
    ("src.services.orders", "get_order_service", "reset_order_service"),
    ("src.services.payments", "get_payment_service", "reset_payment_service"),
    ("src.services.channels", "get_channel_service", "reset_channel_service"),
    ("src.services.accounts", "get_account_registry", "reset_account_registry"),
    ("src.services.engagement", "get_engagement_service", "reset_engagement_service"),
    ("src.services.executor", "get_order_executor", "reset_order_executor"),
    ("src.services.autoengage", "get_auto_engagement", "reset_auto_engagement"),
    ("src.services.notify", "get_notifier", "reset_notifier"),
    ("src.panels", "get_panel_router", "reset_panel_router"),
]


@pytest_asyncio.fixture
async def wired_store():
    """A global store, exactly like the bot installs at start-up."""
    store = MemoryStore()
    await reset_store(store)
    yield store
    await reset_store(None)


@pytest.fixture
def clean_services():
    """Reset every service singleton before and after the test."""
    def _reset():
        for module_name, _getter, resetter in SERVICES:
            module = importlib.import_module(module_name)
            getattr(module, resetter)()
    _reset()
    yield
    _reset()


class TestServiceWiring:
    @pytest.mark.asyncio
    async def test_every_service_reaches_the_global_store(self, wired_store, clean_services):
        """No service may need an explicit store to find the database."""
        module = importlib.import_module("src.services.settings")
        service = module.get_settings_service()          # production call shape
        assert service._store is None                    # nothing was injected
        assert service.store is wired_store              # …and the lazy path works

        for module_name, getter, _reset in SERVICES:
            mod = importlib.import_module(module_name)
            instance = getattr(mod, getter)()
            assert instance is not None, module_name
            # every service that owns a `store` property must resolve it
            if hasattr(type(instance), "store"):
                try:
                    resolved = instance.store
                except Exception as exc:                 # pragma: no cover
                    pytest.fail(f"{module_name}.store raised {exc!r}")
                assert resolved is not None, module_name

    @pytest.mark.asyncio
    async def test_settings_round_trip_without_an_injected_store(self, wired_store, clean_services):
        """Write with one instance, read with a fresh one (the real bug)."""
        module = importlib.import_module("src.services.settings")
        writer = module.get_settings_service()
        await writer.set_path("reactions.free_account_limit", 8)
        await writer.update_section("payments", {"upi_id": "abc@upi"})

        module.reset_settings_service()
        reader = module.get_settings_service()
        settings = await reader.get(refresh=True)
        assert settings["reactions"]["free_account_limit"] == 8
        assert settings["payments"]["upi_id"] == "abc@upi"

    @pytest.mark.asyncio
    async def test_panels_build_without_arguments(self, wired_store, clean_services):
        panels = importlib.import_module("src.panels")
        router = panels.get_panel_router()
        assert router.user_panel is not None
        assert router.admin_panel is not None
        assert router.users is not None


class TestNoMissingImports:
    def test_every_module_imports(self):
        """Import every module under src/ — a broken import must not ship."""
        import src

        root = Path(src.__file__).parent
        failures = []
        for info in pkgutil.walk_packages([str(root)], prefix="src."):
            try:
                importlib.import_module(info.name)
            except Exception as exc:                     # pragma: no cover
                failures.append(f"{info.name}: {exc!r}")
        assert not failures, failures

    def test_no_undefined_store_helpers(self):
        """Guard against the exact bug: using get_store_sync without importing."""
        import re

        root = Path(__file__).resolve().parents[1] / "src"
        defining_module = root / "db" / "store.py"      # this one defines them
        offenders = []
        for path in root.rglob("*.py"):
            if path == defining_module:
                continue
            text = path.read_text()
            for helper in ("get_store_sync", "get_store", "init_store"):
                # ignore definitions and local aliases
                used = re.search(rf"(?<!def )\b{helper}\((?![^)]*=\s*{helper})", text)
                if not used:
                    continue
                imported = (
                    f"import {helper}" in text
                    or bool(re.search(rf"from src\.db\.store import[^\n]*\b{helper}\b", text))
                )
                if not imported:
                    offenders.append(f"{path.name} uses {helper} but never imports it")
        assert not offenders, offenders
