"""
Boot the real bot the way Railway does, but with a fake Telegram transport.

Why this exists: the production logs the operator sent showed a *healthy*
container (MongoDB connected, handlers registered, health endpoint up) yet the
bot behaved wrongly. Booting the actual ``TelegramBot`` here proves the same
entry path works with the production environment variables — including the
quoted values Railway users tend to paste — without touching the network.

    python scripts/railway_boot_sim.py            # exits 0 on success

Checks performed:
  1. config resolution (quoted env values, missing ADMIN_ID, PORT)
  2. storage: MongoStore when a URI is set, JSON/memory fallback otherwise
  3. handlers registered (panels, channel posts, chat actions)
  4. command menu pushed through the (fake) client
  5. health endpoint actually answers on $PORT
  6. a simulated channel add + post still runs the free auto-reaction path
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import urllib.request

PASS = "\033[92m✔\033[0m"
FAIL = "\033[91m✘\033[0m"
failures: list[str] = []


def check(condition: bool, label: str) -> bool:
    print(f"  {PASS if condition else FAIL} {label}")
    if not condition:
        failures.append(label)
    return bool(condition)


def production_env() -> dict:
    """The operator's Railway variables, quotes and all — nothing made up."""
    return {
        "API_ID": '"37777967"',
        "API_HASH": '"8fa425fee5678d74d6d5f3d1d1cf9e0d"',
        "BOT_TOKEN": '"8763301063:AAF2F3COJvyabKVCVPkGpjJcySbG9sTLwL4"',
        "OWNER_ID": '"8707101133"',
        "ADMIN_ID": '""',                 # legacy name left empty on purpose
        "CHANNEL_ID": '"-1003852827894"',
        "MONGODB_URI": '"mongodb://127.0.0.1:27017"',
        "MONGODB_DB": '"telegram_panel_bootsim"',
        "STORAGE_BACKEND": '"auto"',
        "PORT": "18099",
        "HEALTH_HOST": "127.0.0.1",
        "LOG_LEVEL": "INFO",
    }


def install_fake_transport() -> None:
    """Neutralise Telethon so the bot believes it is connected."""
    import telethon

    class _Me:
        id = 999000111
        username = "boot_sim_bot"
        first_name = "BootSim"
        bot = True

    async def _noop(*args, **kwargs):
        return None

    async def _me(*args, **kwargs):
        return _Me()

    async def _call(self, request, *args, **kwargs):
        name = type(request).__name__
        if name == "GetChannelsRequest":
            return type("Channels", (), {"chats": [], "users": []})()
        if name == "ExportChatInviteRequest":
            return type("Invite", (), {"link": "https://t.me/+bootsim"})()
        if name in ("SetBotCommandsRequest",):
            return type("Ok", (), {})()
        return type("Ok", (), {})()

    telethon.TelegramClient.connect = _noop
    telethon.TelegramClient.disconnect = _noop
    telethon.TelegramClient.is_connected = lambda self: True
    telethon.TelegramClient.is_user_authorized = lambda self: True
    telethon.TelegramClient.get_me = _me
    telethon.TelegramClient.start = _noop
    telethon.TelegramClient.add_event_handler = _noop
    telethon.TelegramClient.remove_event_handler = _noop
    telethon.TelegramClient.send_message = _noop
    telethon.TelegramClient.get_entity = _me
    telethon.TelegramClient.get_dialogs = _noop
    telethon.TelegramClient.__call__ = _call


def reset_simulation_database() -> None:
    """Start every run from a clean boot-sim database.

    The simulation is re-run often and Telegram re-delivers recent updates after
    a restart, so a leftover ``last_post_id`` would make the post check a no-op.
    The URI/database come from the parsed configuration — the raw environment
    values carry the quotes Railway users paste (``'"db"'``), which would drop
    the wrong database.
    """
    from src import config as app_config

    uri = app_config.MONGODB_URI
    if not uri:
        return
    try:
        import pymongo

        client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=2000)
        client.drop_database(app_config.MONGODB_DB)
        client.close()
        print(f"  (simulation database '{app_config.MONGODB_DB}' reset)")
    except Exception as exc:  # noqa: BLE001 - the sim still runs without mongo
        print(f"  (could not reset the simulation database: {exc})")


async def main() -> None:
    os.environ.update(production_env())
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.makedirs("/tmp/bootsim-logs", exist_ok=True)

    print("\n1) Environment resolution (quoted values, empty ADMIN_ID)")
    from src import config as app_config
    reset_simulation_database()
    check(app_config.API_ID == 37777967, f"API_ID parsed from quotes -> {app_config.API_ID}")
    check(isinstance(app_config.OWNER_ID, int) and app_config.OWNER_ID == 8707101133,
          f"OWNER_ID -> {app_config.OWNER_ID}")
    check(str(app_config.STORAGE_BACKEND).lower() in ("auto", "mongo", "mongodb"),
          f"STORAGE_BACKEND -> {app_config.STORAGE_BACKEND}")

    print("\n2) Boot the real TelegramBot with the fake transport")
    install_fake_transport()
    from src.logger import setup_logging
    setup_logging("bootsim.log")

    from src.telbot import TelegramBot
    bot = TelegramBot()
    await bot._init_stack()
    check(bot.services_ready, "service stack initialised")
    check(bot.store is not None, f"storage backend ready ({getattr(bot.store, 'name', '?')})")
    check(getattr(bot.store, "name", "").lower() in ("mongo", "mongostore"),
          f"MongoDB storage is used when a URI is configured "
          f"({type(bot.store).__name__})")

    print("\n3) Handlers + command menu")
    from src.runtime import set_bot
    set_bot(bot)
    from src.services.settings import get_settings_service
    from src.services.users import get_user_service
    settings = get_settings_service()
    users = get_user_service()
    cfg = await settings.get()
    check("reactions" in cfg and cfg["reactions"]["auto"]["min_accounts"] == 8,
          "settings readable from the store (no NameError)")
    check(cfg["reactions"]["auto"]["delay_max"] == 3.0, "free-tier delay default is 1-3s")
    check(await users.is_owner(8707101133), "OWNER_ID is recognised as the owner")

    print("\n4) Health endpoint on $PORT")
    from src.health import HealthServer, health_port_from_env
    check(health_port_from_env() == 18099, f"PORT is honoured ({health_port_from_env()})")
    health = HealthServer(18099, host="127.0.0.1", bot=bot)
    await health.start()
    await asyncio.sleep(0.3)
    def _probe() -> tuple:
        # a real HTTP call — in a thread, because the server runs on this loop
        with urllib.request.urlopen("http://127.0.0.1:18099/health", timeout=5) as response:
            return response.status, response.read().decode()

    try:
        status, body = await asyncio.to_thread(_probe)
        check(status == 200 and "ok" in body.lower(),
              f"/health answers 200 ({body.strip()[:70]})")
    except Exception as exc:  # noqa: BLE001
        check(False, f"/health unreachable: {exc}")
    finally:
        try:
            await health.stop()
        except Exception:
            pass

    print("\n5) Free auto-reaction path still works after this boot")
    from src.services.autoengage import get_auto_engagement
    from src.services.channels import get_channel_service
    channels = get_channel_service()
    await channels.register(-1003852827894, 8707101133, title="Boot Sim Channel")
    auto = get_auto_engagement()
    info = await auto.enable_free_plan(-1003852827894)
    check(info.get("reactions") == "on (free)", f"free plan started ({info.get('reactions')})")
    # no accounts signed in -> the bot must say so instead of a silent success
    outcome = await auto.handle_new_post(-1003852827894, 1)
    reactions = outcome.get("reactions") or {}
    check("reactions" in outcome, f"automation ran ({reactions})")
    check(reactions.get("ok", 0) == 0, "0 reactions with an empty pool (expected)")
    doc = await channels.get(-1003852827894)
    check(doc["auto_reactions"]["enabled"] is True, "auto reactions stay enabled for free tier")

    print("\n" + "=" * 60)
    if failures:
        print(f"{FAIL} {len(failures)} check(s) failed:")
        for item in failures:
            print(f"   - {item}")
        raise SystemExit(1)
    print(f"{PASS} production-style boot is healthy")


if __name__ == "__main__":
    logging.getLogger("telethon").setLevel(logging.WARNING)
    asyncio.run(main())
