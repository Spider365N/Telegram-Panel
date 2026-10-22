import os
import time
import logging
import asyncio
import json
from datetime import datetime
from typing import Optional
from telethon import TelegramClient, events, Button
from telethon.errors import (
    AuthRestartError,
    FloodWaitError,
    PasswordEmptyError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SessionPasswordNeededError,
    UserDeactivatedError,
)
from telethon.tl.types import Channel, Chat
from src.config import ConfigManager, API_ID, API_HASH, ADMIN_ID, CHANNEL_ID, CLIENTS_JSON_PATH, RATE_LIMIT_SLEEP, GROUPS_BATCH_SIZE, GROUPS_UPDATE_SLEEP, REPORT_CHECK_BOT
from src.keyboards import Keyboard
from src.validation import InputValidator
from src.utils import (
    extract_account_name,
    get_safe_session_file_path,
    is_fatal_session_error,
    is_session_revoked_error,
    prompt_for_input,
    remove_revoked_session_completely,
    sanitize_session_name,
    send_error_message,
)


# How many times a wrong OTP / 2-step password may be retried before the
# sign-in flow is restarted. One typo must not throw a working setup away.
MAX_AUTH_ATTEMPTS = 3

# Set up logger for the SessionManager class
logger = logging.getLogger(__name__)

class SessionManager:
    def __init__(self, config, active_clients, tbot):
        """
        Initialize the SessionManager to handle Telegram client sessions.
        :param config: Configuration dictionary for client sessions.
        :param active_clients: Dictionary of active clients mapped by session names.
        :param tbot: Telegram bot instance for sending messages.
        """
        try:
            self.config = config
            self.active_clients = active_clients
            self.tbot = tbot
            # Use ConfigManager to manage client configurations
            self.config_manager = ConfigManager("clients.json", self.config)
            logger.info("SessionManager initialized successfully.")
        except Exception as e:
            logger.critical(f"Error initializing SessionManager: {e}")
            raise

    async def detect_sessions(self):
        """
        Detect and load Telegram client sessions from the configuration.
        Adds sessions to `active_clients` if they are not already active.
        Note: This method is now async to support proper locking.
        """
        try:
            if not isinstance(self.config.get('clients', {}), dict):
                logger.warning("'clients' is not a dictionary. Initializing it as an empty dictionary.")
                self.config['clients'] = {}

            # Always use lock for thread-safety - raise error if lock is not available
            if not hasattr(self.tbot, 'active_clients_lock'):
                logger.error("active_clients_lock not available in tbot. This should not happen in production.")
                raise RuntimeError("active_clients_lock not initialized")
            
            async with self.tbot.active_clients_lock:
                for session_name in list(self.config['clients']):
                    if session_name not in self.active_clients:
                        # Sanitize session name before use
                        try:
                            sanitized_name = sanitize_session_name(session_name)
                        except ValueError as e:
                            logger.warning(f"Invalid session name '{session_name}' skipped: {e}")
                            continue
                        
                        # Initialize Telegram client for the session
                        client = TelegramClient(sanitized_name, API_ID, API_HASH)
                        self.active_clients[sanitized_name] = client
                        # Update config key if sanitized name differs
                        if sanitized_name != session_name:
                            if sanitized_name not in self.config['clients']:
                                self.config['clients'][sanitized_name] = self.config['clients'][session_name]
                            del self.config['clients'][session_name]
                            self.config_manager.save_config(self.config)
            
            logger.info("Sessions detected and loaded successfully.")
        except Exception as e:
            logger.error(f"Error detecting sessions: {e}")

    async def start_saved_clients(self):
        """
        Start all Telegram client sessions listed in the configuration.
        Ensures clients are authorized and ready to use.

        Account Persistence Behavior:
        - Accounts persist in clients.json and .session files
        - Only true SessionRevokedError sessions are deleted
        - Temporary network/API errors don't delete sessions
        - Accounts remain available for future use
        - Inactive accounts are tracked for admin review
        """
        try:
            # Load session information into active_clients
            await self.detect_sessions()

            # Legacy bookkeeping key: it is only ever *read* (and cleared) now so
            # that an older deployment does not lose its history. Nothing new is
            # written here — the account registry (MongoDB, ``status="revoked"``)
            # is the single source of truth, and only a ban/block lands there.

            for session_name, client in list(self.active_clients.items()):
                try:
                    # Connect client if not already connected
                    if not client.is_connected():
                        await client.connect()

                    # Check if the client is authorized
                    if await client.is_user_authorized():
                        logger.info(f"Successfully loaded and authorized client: {session_name}")
                        # An authorized account is active — in the registry and in
                        # any (legacy) inactive list left over from older versions.
                        await self._mark_active_in_registry(session_name)
                        legacy_inactive = self.config.get('inactive_accounts') or {}
                        if session_name in legacy_inactive:
                            del legacy_inactive[session_name]
                            self.config_manager.save_config(self.config)
                        # Ensure session is saved after successful authorization
                        client.session.save()
                    else:
                        # Client reported as unauthorized - check if it's really revoked
                        logger.warning(f"Client {session_name} reported as unauthorized. Checking if it's really revoked...")

                        try:
                            # Try to get a simple dialog to check if session is really revoked
                            await client.get_dialogs(limit=1)
                            logger.info(f"Client {session_name} can still access dialogs despite is_user_authorized() returning False. Keeping in active list.")
                            # Keep the client in active list since it can still function
                        except Exception as check_error:
                            check_error_msg = str(check_error).lower()
                            logger.warning(f"Client {session_name} failed dialog check: {check_error_msg}")

                            # Only bans / blocks / revoked sessions drop an
                            # account. Everything else (network, sqlite lock,
                            # invite problem) must be retried, never logged out:
                            # "An invalid Peer was used" used to match the old
                            # 'invalid' check and took healthy accounts offline.
                            if is_fatal_session_error(check_error):
                                logger.warning(f"Client {session_name} has true SessionRevokedError. Removing permanently...")
                                logger.warning(f"This happens when: 1) Account password changed, 2) Account logged out from official app, 3) Telegram security measures")
                                # Remove from active clients first
                                async with self.tbot.active_clients_lock:
                                    if session_name in self.tbot.active_clients:
                                        del self.tbot.active_clients[session_name]
                                await client.disconnect()
                                await self.delete_session(session_name)
                                await self._mark_revoked_in_registry(session_name, check_error)
                                logger.info(f"Unauthorized session {session_name} has been completely removed")
                            else:
                                # Not a fatal (ban/block/revoked) error, but the
                                # client is having issues. KEEP IT IN THE POOL and
                                # let the supervisor reconnect it.
                                logger.warning(f"Client {session_name} has connectivity issues but is not banned/blocked. Keeping it in the pool and retrying automatically.")
                                # IMPORTANT: Keep the client in active_clients even if it has issues
                                # This allows it to be retried later

                    # Sleep to avoid hitting Telegram flood limits
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.warning(f"Error loading client {session_name}: {e}")

                    # Only a ban / block / revoked session takes an account out.
                    # Connectivity problems, sqlite locks and invite errors are
                    # retried forever by the pool supervisor (requirement: an
                    # account must never be logged out or marked inactive
                    # because of an error).
                    if is_fatal_session_error(e):
                        # True authentication/session errors
                        logger.warning(f"Client {session_name} was banned/blocked or its session was revoked. Moving to inactive.")
                        async with self.tbot.active_clients_lock:
                            removed_client = self.tbot.active_clients.pop(session_name, None)
                        # Disconnect outside the lock to release the connection
                        # opened earlier (avoids leaking the socket/reader task).
                        if removed_client is not None:
                            try:
                                await removed_client.disconnect()
                            except Exception as disc_err:
                                logger.debug(f"Error disconnecting {session_name}: {disc_err}")

                        # Never keep a second, legacy list of "inactive" accounts:
                        # the Mongo registry (status=revoked) is the only place an
                        # account may be marked, and only a ban/block reaches here.
                        await self._mark_revoked_in_registry(session_name, e)
                    else:
                        # Temporary errors - keep trying, don't make inactive
                        logger.warning(f"Temporary error with client {session_name}: {e}. Will retry on next startup.")
                        # IMPORTANT: DO NOT remove from active_clients for temporary errors
                        # Keep it in active_clients so it can be retried
                        logger.info(f"Keeping {session_name} in active_clients despite temporary error for future retry")
        except Exception as e:
            logger.error(f"Error in start_saved_clients: {e}")

    async def _mark_active_in_registry(self, session_name: str) -> None:
        """Best-effort: an account that authorized again is active, not revoked."""
        try:
            from src.services.accounts import get_account_registry
            registry = get_account_registry()
            doc_id = await registry.doc_id_for(session_name)
            if doc_id:
                await registry.store.set_status(doc_id, "active", None)
        except Exception as exc:  # noqa: BLE001 - boot must continue
            logger.debug(f"could not mark {session_name} active in the registry: {exc}")

    async def _mark_revoked_in_registry(self, session_name: str, error: Exception) -> None:
        """Best-effort: record a ban/block in the account registry (MongoDB)."""
        try:
            from src.services.accounts import get_account_registry
            registry = get_account_registry()
            await registry.remove_by_session(session_name, f"fatal: {str(error)[:120]}")
            logger.info(f"Account {session_name} marked revoked in the registry")
        except Exception as exc:  # noqa: BLE001 - the log line above is the fallback
            logger.debug(f"could not mark {session_name} revoked in the registry: {exc}")

    async def show_inactive_accounts(self, event):
        """Display the accounts that were logged out (ban / block only).

        Accounts are never "inactive" because of an error: the only way into
        this list is Telegram reporting a ban/block/revoked session. The
        registry (MongoDB) is the source of truth; the legacy config key is
        only read when the registry is unreachable.
        """
        try:
            logger.info("show_inactive_accounts called")

            inactive_accounts = {}
            try:
                from src.services.accounts import get_account_registry
                registry = get_account_registry()
                for doc in await registry.store.list_accounts(status="revoked"):
                    phone = str(doc.get("phone") or doc.get("_id"))
                    inactive_accounts[phone] = {
                        "reason": "banned/blocked/revoked",
                        "last_seen": doc.get("updated_at") or 0,
                        "error_details": str(doc.get("last_error") or doc.get("revoke_reason") or ""),
                    }
            except Exception as exc:  # noqa: BLE001 - the legacy key is the fallback
                logger.debug(f"registry lookup for revoked accounts failed: {exc}")
            # Merge anything the (legacy) config still carries, so an older
            # deployment keeps showing its history. The registry wins.
            for phone, info in (self.config.get('inactive_accounts') or {}).items():
                inactive_accounts.setdefault(phone, info)

            if not inactive_accounts:
                await event.respond("No inactive accounts found. All accounts are working properly.")
                return

            # Build response message
            message = "**Inactive Accounts Report**\n\n"
            message += f"Total inactive accounts: {len(inactive_accounts)}\n\n"

            for phone, account_info in inactive_accounts.items():
                reason = account_info.get('reason', 'unknown')
                last_seen = account_info.get('last_seen', 0)
                error_details = account_info.get('error_details', 'No details available')

                # Format timestamp (seconds or an ISO string from the registry)
                import time
                try:
                    last_seen_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(last_seen)))
                except (TypeError, ValueError, OSError):
                    last_seen_str = str(last_seen or "unknown")

                message += f"**{phone}**\n"
                message += f"   Reason: {reason}\n"
                message += f"   Last seen: {last_seen_str}\n"
                message += f"   Error: {error_details[:100]}{'...' if len(error_details) > 100 else ''}\n\n"

            # Add management options
            message += "**Management Options:**\n"
            message += "• Use /start to return to main menu\n"
            message += "• Inactive accounts will be automatically reactivated when possible"

            # Send message (split if too long)
            if len(message) > 4000:
                # Split into chunks
                chunks = []
                current_chunk = ""
                lines = message.split('\n')

                for line in lines:
                    if len(current_chunk + line + '\n') > 4000:
                        chunks.append(current_chunk)
                        current_chunk = line + '\n'
                    else:
                        current_chunk += line + '\n'

                if current_chunk:
                    chunks.append(current_chunk)

                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await event.respond(chunk)
                    else:
                        await event.respond(f"(Part {i+1}/{len(chunks)})\n{chunk}")
            else:
                await event.respond(message)

        except Exception as e:
            logger.error(f"Error in show_inactive_accounts: {e}")
            await event.respond("Error retrieving inactive accounts list.")

    async def reactivate_account(self, event, phone_number):
        """
        Attempt to reactivate an inactive account.
        """
        try:
            logger.info(f"reactivate_account called for {phone_number}")

            if 'inactive_accounts' not in self.config or phone_number not in self.config['inactive_accounts']:
                await event.respond(f"Account {phone_number} is not in inactive accounts list.")
                return

            # Remove from inactive list and try to reload
            del self.config['inactive_accounts'][phone_number]
            self.config_manager.save_config(self.config)

            # Try to reload the account
            try:
                session_file = get_safe_session_file_path(phone_number)
            except ValueError as e:
                logger.error(f"Invalid phone number for session file: {e}")
                await event.respond(f"Invalid phone number format.")
                return
            
            if os.path.exists(session_file):
                try:
                    # Create new client instance
                    from telethon import TelegramClient
                    from src.config import API_ID, API_HASH

                    # Sanitize phone number for use as session name
                    try:
                        sanitized_phone = sanitize_session_name(phone_number)
                    except ValueError as e:
                        logger.error(f"Invalid phone number for session name: {e}")
                        await event.respond(f"Invalid phone number format.")
                        return
                    
                    client = TelegramClient(sanitized_phone, API_ID, API_HASH)

                    # Try to connect and authorize
                    await client.connect()
                    if await client.is_user_authorized():
                        # Use the sanitized name as the key, consistent with
                        # detect_sessions/finalize_client_setup and the actual
                        # session file Telethon opens.
                        async with self.tbot.active_clients_lock:
                            self.tbot.active_clients[sanitized_phone] = client

                        # Add back to config without wiping any previously
                        # discovered groups/metadata for this account.
                        if 'clients' not in self.config:
                            self.config['clients'] = {}
                        self.config['clients'].setdefault(sanitized_phone, [])
                        self.config_manager.save_config(self.config)

                        await event.respond(f"Account {phone_number} successfully reactivated!")
                        logger.info(f"Account {phone_number} reactivated successfully")
                    else:
                        await client.disconnect()
                        await event.respond(f"Account {phone_number} is still not authorized. Cannot reactivate.")
                        logger.warning(f"Account {phone_number} still not authorized during reactivation")

                except Exception as e:
                    logger.error(f"Error reactivating account {phone_number}: {e}")
                    await event.respond(f"Error reactivating account {phone_number}: {str(e)}")
            else:
                await event.respond(f"Session file for {phone_number} not found.")
                logger.warning(f"Session file {session_file} not found during reactivation")

        except Exception as e:
            logger.error(f"Error in reactivate_account: {e}")
            await event.respond("Error during account reactivation.")

    async def disconnect_all_clients(self):
        """
        Disconnect all active Telegram clients and clear the active client list.
        """
        try:
            for session_name, client in list(self.active_clients.items()):
                try:
                    # Cleanup handlers before disconnecting to prevent memory leaks
                    if hasattr(self.tbot, 'monitor'):
                        self.tbot.monitor.cleanup_client_handlers(client)
                    await client.disconnect()
                    logger.info(f"Client {session_name} disconnected successfully.")
                except Exception as e:
                    logger.error(f"Error disconnecting client {session_name}: {e}")
            
            # Clear the active_clients dictionary
            self.active_clients.clear()
            logger.info("All clients disconnected successfully.")
        except Exception as e:
            logger.error(f"Error disconnecting clients: {e}")

    async def delete_session(self, session_name):
        """
        Delete a session file and remove it from the configuration.
        :param session_name: The name of the session to delete.
        """
        try:
            if session_name in self.active_clients:
                client = self.active_clients[session_name]
                # Cleanup handlers before disconnecting
                if hasattr(self.tbot, 'monitor'):
                    self.tbot.monitor.cleanup_client_handlers(client)
                await client.disconnect()
                del self.active_clients[session_name]
                logger.info(f"Client {session_name} disconnected and removed from active clients.")

            # Mark the account as revoked/deleted in the database as well, so the
            # panel never tries to use a session that no longer exists.
            try:
                from src.services.accounts import get_account_registry
                registry = getattr(self.tbot, 'account_registry', None) or get_account_registry()
                await registry.remove_by_session(session_name, "session deleted")
            except Exception as exc:
                logger.debug(f"Could not mark {session_name} revoked in the database: {exc}")

            if session_name in self.config['clients']:
                del self.config['clients'][session_name]
                self.config_manager.save_config(self.config)

                try:
                    session_file = get_safe_session_file_path(session_name)
                    if os.path.exists(session_file):
                        try:
                            os.remove(session_file)
                            logger.info(f"Session file {session_file} deleted successfully.")
                        except OSError as e:
                            # File is in use by another process - this is normal
                            logger.warning(f"Session file {session_file} could not be deleted (in use by another process): {e}")
                            logger.info(f"Session {session_name} removed from config but file remains (will be cleaned up later)")
                except ValueError as e:
                    logger.error(f"Invalid session name for deletion: {e}")

                # Session deleted successfully - no need to send message as callback handler will respond
                logger.info(f"Session {session_name} deleted successfully.")
            else:
                logger.warning(f"Session {session_name} not found in configuration.")
        except Exception as e:
            logger.error(f"Error deleting session {session_name}: {e}")
            # Don't send error message here - let the callback handler handle it


class AccountHandler:
    """
    Handles all account-related operations for the Telegram bot.
    Manages account creation, authentication, and message processing.
    """

    def __init__(self, tbot):
        """
        Initialize AccountHandler with bot instance.

        Args:
            tbot: Bot instance containing configuration and client management
        """
        self.tbot = tbot
        self._conversations = {}
        self.SessionManager = tbot.client_manager

    async def add_account(self, event):
        """
        Initiates the account addition process by requesting phone number.

        Args:
            event: Telegram event containing chat information
        """
        logger.info("add_account in AccountHandler")
        chat_id = event.chat_id
        try:
            await prompt_for_input(
                self.tbot, event,
                "Please enter your phone number:",
                'phone_number_handler'
            )
        except Exception as e:
            logger.error(f"Error in add_account: {e}")
            await self.tbot.tbot.send_message(chat_id, "Error occurred while adding account. Please try again.")

    # ==================================================================
    # sign-in retry bookkeeping
    # ==================================================================
    # The production log showed one wrong OTP (or one wrong 2FA password)
    # throwing the whole flow away: the client was closed, the state was wiped
    # and the operator had to start from /start again. Every entry now gets
    # ``MAX_AUTH_ATTEMPTS`` tries on the *same* client.
    AUTH_ATTEMPTS = MAX_AUTH_ATTEMPTS

    def _auth_attempts(self, key: str = "temp_attempts") -> int:
        try:
            return int(self.tbot.handlers.get(key) or 0)
        except Exception:  # pragma: no cover - defensive
            return 0

    def _set_auth_attempts(self, value: int, key: str = "temp_attempts") -> None:
        try:
            self.tbot.handlers[key] = int(value)
        except Exception:  # pragma: no cover - defensive
            pass

    async def _send_text(self, chat_id, text: str) -> None:
        """Best-effort chat message; the sign-in flow must never die on it."""
        try:
            await self.tbot.tbot.send_message(chat_id, text)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"could not send sign-in message: {exc}")

    async def _delete_message(self, message) -> None:
        if message is None:
            return
        try:
            await message.delete()
        except Exception as exc:
            logger.debug(f"Failed to delete prompt message: {exc}")

    async def _close_client_quietly(self, client) -> None:
        try:
            if client is not None and client.is_connected():
                await client.disconnect()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"could not close the temporary client: {exc}")

    def _discard_temp_client(self) -> None:
        """Close a half-finished sign-in client (never a live pool account).

        The old flow left it connected, so every OTP retry opened the *same*
        ``.session`` file a second time — that is where the production log got
        ``sqlite3.OperationalError: database is locked`` from, and the account
        that crashed with it silently fell out of the reaction pool (5 of 6).
        """
        try:
            client = self.tbot.handlers.get('temp_client')
        except Exception:  # pragma: no cover - defensive
            client = None
        if client is None:
            return
        try:
            live = list((getattr(self.tbot, "active_clients", {}) or {}).values())
        except Exception:  # pragma: no cover - defensive
            live = []
        if client in live:      # already promoted to the pool — keep it running
            return
        try:
            asyncio.create_task(self._close_client_quietly(client))
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"could not schedule temp-client cleanup: {exc}")

    async def _signin_client(self, sanitized_phone: str):
        """A sign-in client for this phone — never two clients on one session file."""
        existing = None
        try:
            existing = self.tbot.handlers.get('temp_client')
        except Exception:  # pragma: no cover - defensive
            existing = None
        stored_phone = ""
        try:
            # Compare the *sanitized* phone: the raw text ("+17209738889") and the
            # session name ("17209738889") used to look different, so the second
            # attempt opened a second client on the same .session file — that is
            # exactly where the production "sqlite3.OperationalError: database is
            # locked" came from.
            stored_phone = str(self.tbot.handlers.get('temp_phone_key')
                               or self.tbot.handlers.get('temp_phone') or "")
        except Exception:  # pragma: no cover - defensive
            stored_phone = ""
        if stored_phone:
            try:
                from src.utils import sanitize_session_name as _sanitize
                stored_phone = _sanitize(stored_phone)
            except Exception:  # pragma: no cover - defensive
                pass

        if existing is not None and stored_phone == str(sanitized_phone):
            try:
                if existing.is_connected():
                    logger.info("PHONE_NUMBER_HANDLER: reusing the existing sign-in client")
                    return existing
                await existing.connect()
                logger.info("PHONE_NUMBER_HANDLER: reconnected the existing sign-in client")
                return existing
            except Exception as exc:
                logger.warning(f"Sign-in client unusable, creating a new one: {exc}")
                self._discard_temp_client()
        elif existing is not None:
            # Another phone (or a stale attempt): close it *before* opening a new
            # session file, otherwise the sqlite session stays locked.
            self._discard_temp_client()
        try:
            self.tbot.handlers.pop('temp_client', None)
        except Exception:  # pragma: no cover - defensive
            pass

        logger.info(f"PHONE_NUMBER_HANDLER: Creating client for {sanitized_phone}")
        client = TelegramClient(sanitized_phone, API_ID, API_HASH)
        await client.connect()
        try:
            # Remember which phone this client belongs to *now*, so a retry from
            # the very same phone reuses it instead of opening the file twice.
            self.tbot.handlers['temp_client'] = client
            self.tbot.handlers['temp_phone_key'] = str(sanitized_phone)
        except Exception:  # pragma: no cover - defensive
            pass
        return client

    async def _send_login_code(self, client, phone_number: str, chat_id,
                               *, attempts_left: Optional[int] = None) -> bool:
        """(Re)request a login code, tolerating Telegram's restart demand."""
        try:
            await client.send_code_request(phone_number)
        except AuthRestartError:
            # Telegram asked us to start over: reconnect and try once more
            # instead of throwing the whole sign-in away.
            logger.warning("Telegram asked to restart the authorization — retrying the code request")
            try:
                await client.connect()
                await client.send_code_request(phone_number)
            except Exception as exc:
                logger.error(f"Could not send the code after an auth restart: {exc}")
                return False
        except FloodWaitError as exc:
            wait = int(getattr(exc, "seconds", 0) or 0)
            await self._send_text(
                chat_id,
                f"⏳ Telegram is limiting code requests. Please wait {wait} seconds "
                f"and send /start again — the account is still ready.",
            )
            return False
        except Exception as exc:
            logger.error(f"Could not send the login code: {exc}")
            await send_error_message(self.tbot, chat_id, exc, 'phone')
            return False

        text = (
            "📩 <b>Verification code sent</b>\n\n"
            "Enter the code Telegram just sent you."
        )
        if attempts_left:
            text += f"\n\nYou have <b>{attempts_left}</b> attempt(s)."
        await self._send_text(chat_id, text)
        return True

    async def phone_number_handler(self, event):
        """
        Handles phone number verification and initiates client connection.

        Args:
            event: Telegram event containing the phone number
        """
        logger.info("phone_number_handler in AccountHandler")
        chat_id = event.chat_id
        phone_number = event.message.text.strip()

        # Validate phone number
        is_valid, error_msg = InputValidator.validate_phone_number(phone_number)
        if not is_valid:
            await self._send_text(chat_id, f"{error_msg}\nPlease try again.")
            return

        # Sanitize phone number for use as session name
        try:
            sanitized_phone = sanitize_session_name(phone_number)
        except ValueError as e:
            logger.error(f"Invalid phone number format: {e}")
            await self._send_text(chat_id, "Invalid phone number format. Please try again.")
            return

        try:
            client = await self._signin_client(sanitized_phone)

            if await client.is_user_authorized():
                logger.info("PHONE_NUMBER_HANDLER: Already authorized, finalizing setup")
                await self.finalize_client_setup(client, phone_number, chat_id)
                return

            logger.info("PHONE_NUMBER_HANDLER: Not authorized, sending code request")
            wait_message = await self.tbot.tbot.send_message(chat_id, "Please wait...")
            sent = await self._send_login_code(
                client, phone_number, chat_id, attempts_left=MAX_AUTH_ATTEMPTS,
            )
            await self._delete_message(wait_message)
            if not sent:
                return

            # A fresh flow always starts with full attempts.
            self._set_auth_attempts(0, "temp_attempts")
            self._set_auth_attempts(0, "temp_pw_attempts")
            async with self.tbot._conversations_lock:
                self.tbot._conversations[chat_id] = 'code_handler'
            self.tbot.handlers['temp_client'] = client
            self.tbot.handlers['temp_phone'] = phone_number
            self.tbot.handlers['temp_phone_key'] = sanitized_phone
            logger.info("PHONE_NUMBER_HANDLER: Code request sent successfully")
        except Exception as e:
            logger.error(f"PHONE_NUMBER_HANDLER: Error - {e}")
            await send_error_message(self.tbot, chat_id, e, 'phone')
            self.cleanup_temp_handlers(discard=True)

    async def _retry_code(self, chat_id, phone_number: str, attempts: int, reason: str) -> None:
        """Ask for the code again instead of restarting the whole setup."""
        if attempts >= MAX_AUTH_ATTEMPTS:
            await self._send_text(
                chat_id,
                f"❌ {reason}.\n\n{MAX_AUTH_ATTEMPTS} attempts used — the setup was "
                f"cancelled. Send /start to add the account again.",
            )
            self.cleanup_temp_handlers(discard=True)
            return
        left = MAX_AUTH_ATTEMPTS - attempts
        try:
            async with self.tbot._conversations_lock:
                self.tbot._conversations[chat_id] = 'code_handler'
        except Exception:  # pragma: no cover - defensive
            pass
        await self._send_text(
            chat_id,
            f"❌ {reason}.\n\nSend the code again — <b>{left}</b> attempt(s) left.\n"
            f"<i>You can also send a new code with /start.</i>",
        )

    async def _retry_password(self, chat_id, attempts: int, reason: str) -> None:
        if attempts >= MAX_AUTH_ATTEMPTS:
            await self._send_text(
                chat_id,
                f"❌ {reason}.\n\n{MAX_AUTH_ATTEMPTS} attempts used — the setup was "
                f"cancelled. Send /start to add the account again.",
            )
            self.cleanup_temp_handlers(discard=True)
            return
        left = MAX_AUTH_ATTEMPTS - attempts
        try:
            async with self.tbot._conversations_lock:
                self.tbot._conversations[chat_id] = 'password_handler'
        except Exception:  # pragma: no cover - defensive
            pass
        await self._send_text(
            chat_id,
            f"❌ {reason}.\n\nSend your 2-step password again — "
            f"<b>{left}</b> attempt(s) left.",
        )

    async def code_handler(self, event):
        """
        Processes verification code and completes authentication.

        A wrong code no longer restarts the flow: the same client and phone stay
        in place and the user gets ``MAX_AUTH_ATTEMPTS`` tries.

        Args:
            event: Telegram event containing the verification code
        """
        chat_id = event.chat_id
        try:
            logger.info("code_handler in AccountHandler")
            code = event.message.text.strip()
            client = self.tbot.handlers.get('temp_client')
            phone_number = self.tbot.handlers.get('temp_phone')

            if not client or not phone_number:
                await self._send_text(chat_id, "Session expired or invalid. Please start over with /start.")
                self.cleanup_temp_handlers(discard=True)
                return

            processing_msg = await self.tbot.tbot.send_message(chat_id, "Verifying code...")
            try:
                # Attempt sign in with code
                await client.sign_in(phone=phone_number, code=code)

                # ✅ CRITICAL: Verify that login was successful
                if await client.is_user_authorized():
                    logger.info(f"Code verification successful for {phone_number}")
                    await self._delete_message(processing_msg)
                    self._set_auth_attempts(0, "temp_attempts")
                    self._set_auth_attempts(0, "temp_pw_attempts")
                    await self.finalize_client_setup(client, phone_number, chat_id)
                else:
                    # Login failed but no exception
                    logger.error(f"Code verification failed for {phone_number}: not authorized after sign_in")
                    await self._delete_message(processing_msg)
                    attempts = self._auth_attempts() + 1
                    self._set_auth_attempts(attempts)
                    await self._retry_code(chat_id, phone_number, attempts,
                                           "Telegram did not accept that code")
            except SessionPasswordNeededError:
                await self._delete_message(processing_msg)
                self._set_auth_attempts(0, "temp_pw_attempts")
                await self._send_text(
                    chat_id,
                    "🔐 <b>Two-factor authentication is enabled.</b>\n\n"
                    "Please enter your 2-step password.\n"
                    f"<i>You get {MAX_AUTH_ATTEMPTS} tries — a wrong password will not "
                    "cancel the setup.</i>",
                )
                async with self.tbot._conversations_lock:
                    self.tbot._conversations[chat_id] = 'password_handler'
            except (PhoneCodeInvalidError, PhoneCodeEmptyError):
                await self._delete_message(processing_msg)
                attempts = self._auth_attempts() + 1
                self._set_auth_attempts(attempts)
                logger.warning(f"Wrong code for {phone_number} (attempt {attempts}/{MAX_AUTH_ATTEMPTS})")
                await self._retry_code(chat_id, phone_number, attempts, "That code was not correct")
            except PhoneCodeExpiredError:
                await self._delete_message(processing_msg)
                logger.info(f"Code expired for {phone_number} — sending a fresh one")
                # Expiry is not a wrong attempt: keep the counter untouched.
                sent = await self._send_login_code(
                    client, phone_number, chat_id,
                    attempts_left=MAX_AUTH_ATTEMPTS - self._auth_attempts(),
                )
                if not sent:
                    self.cleanup_temp_handlers(discard=True)
            except AuthRestartError:
                await self._delete_message(processing_msg)
                logger.info(f"Auth restart requested for {phone_number} — resending the code")
                await self._send_login_code(
                    client, phone_number, chat_id,
                    attempts_left=MAX_AUTH_ATTEMPTS - self._auth_attempts(),
                )
            except FloodWaitError as exc:
                await self._delete_message(processing_msg)
                wait = int(getattr(exc, "seconds", 0) or 0)
                await self._send_text(
                    chat_id,
                    f"⏳ Too many attempts. Wait {wait} seconds and send the code again — "
                    f"nothing was cancelled.",
                )
            except (PhoneNumberBannedError, PhoneNumberInvalidError,
                    PhoneNumberUnoccupiedError, UserDeactivatedError):
                await self._delete_message(processing_msg)
                logger.error(f"Phone number unusable for {phone_number}: this account cannot sign in")
                await self._send_text(
                    chat_id,
                    "🚫 Telegram refuses this phone number (banned, invalid or "
                    "deactivated account). Please use another account.",
                )
                self.cleanup_temp_handlers(discard=True)
            except Exception as sign_in_error:
                await self._delete_message(processing_msg)
                raise sign_in_error
        except FloodWaitError as e:
            await self._send_text(chat_id, f"Too many attempts. Please wait {e.seconds} seconds and try again.")
        except Exception as e:
            logger.error(f"Error during code verification: {e}")
            await send_error_message(self.tbot, chat_id, e, 'code')
            self.cleanup_temp_handlers(discard=True)

    async def password_handler(self, event):
        """
        Handles 2FA password verification if required.

        Args:
            event: Telegram event containing the 2FA password
        """
        chat_id = event.chat_id
        try:
            logger.info("password_handler in AccountHandler")
            password = event.message.text.strip()
            client = self.tbot.handlers.get('temp_client')
            phone_number = self.tbot.handlers.get('temp_phone')

            if not client or not phone_number:
                await self._send_text(chat_id, "Session expired or invalid. Please start over with /start.")
                self.cleanup_temp_handlers(discard=True)
                return

            processing_msg = await self.tbot.tbot.send_message(chat_id, "Verifying password...")
            try:
                # Attempt sign in with password
                await client.sign_in(password=password)

                # ✅ CRITICAL: Verify that login was successful
                if await client.is_user_authorized():
                    logger.info(f"Password verification successful for {phone_number}")
                    await self._delete_message(processing_msg)
                    self._set_auth_attempts(0, "temp_attempts")
                    self._set_auth_attempts(0, "temp_pw_attempts")
                    await self.finalize_client_setup(client, phone_number, chat_id)
                else:
                    logger.error(f"Password verification failed for {phone_number}: not authorized after sign_in")
                    await self._delete_message(processing_msg)
                    attempts = self._auth_attempts("temp_pw_attempts") + 1
                    self._set_auth_attempts(attempts, "temp_pw_attempts")
                    await self._retry_password(chat_id, attempts,
                                               "Telegram did not accept that password")
            except (PasswordHashInvalidError, PasswordEmptyError):
                await self._delete_message(processing_msg)
                attempts = self._auth_attempts("temp_pw_attempts") + 1
                self._set_auth_attempts(attempts, "temp_pw_attempts")
                logger.warning(f"Wrong 2FA password for {phone_number} "
                               f"(attempt {attempts}/{MAX_AUTH_ATTEMPTS})")
                await self._retry_password(chat_id, attempts, "That password was not correct")
            except SessionPasswordNeededError:
                await self._delete_message(processing_msg)
                await self._retry_password(
                    chat_id, self._auth_attempts("temp_pw_attempts"),
                    "Two-factor password is still needed",
                )
            except FloodWaitError as exc:
                await self._delete_message(processing_msg)
                wait = int(getattr(exc, "seconds", 0) or 0)
                await self._send_text(
                    chat_id,
                    f"⏳ Too many attempts. Wait {wait} seconds and send your password "
                    f"again — nothing was cancelled.",
                )
            except Exception as sign_in_error:
                await self._delete_message(processing_msg)
                raise sign_in_error
        except FloodWaitError as e:
            await self._send_text(chat_id, f"Too many attempts. Please wait {e.seconds} seconds and try again.")
        except Exception as e:
            logger.error(f"Error during password verification: {e}")
            await send_error_message(self.tbot, chat_id, e, 'password')
            self.cleanup_temp_handlers(discard=True)

    async def finalize_client_setup(self, client, phone_number, chat_id):
        """
        Completes client setup and saves configuration.

        Args:
            client: Authorized TelegramClient instance
            phone_number: User's phone number
            chat_id: Chat ID for response messages
        """
        logger.info("finalize_client_setup in AccountHandler")
        try:
            # ✅ DOUBLE CHECK: Make absolutely sure the client is authorized before saving
            if not await client.is_user_authorized():
                logger.error(f"CRITICAL: Client {phone_number} is NOT authorized in finalize_client_setup!")
                await self.tbot.tbot.send_message(chat_id, "Authentication failed. Account is not properly authorized.\n\nPlease try again with /start.")
                self.cleanup_temp_handlers()
                return
            
            # ✅ Verify we can get user info
            try:
                me = await client.get_me()
                if not me:
                    logger.error(f"CRITICAL: get_me() returned None for {phone_number}")
                    await self.tbot.tbot.send_message(chat_id, "Authentication verification failed.\n\nPlease try again with /start.")
                    self.cleanup_temp_handlers()
                    return
                logger.info(f"Verified user: {me.first_name} (@{me.username}) - ID: {me.id}")
            except Exception as verify_error:
                logger.error(f"CRITICAL: Cannot verify user {phone_number}: {verify_error}")
                await self.tbot.tbot.send_message(chat_id, f"Authentication verification failed: {verify_error}\n\nPlease try again with /start.")
                self.cleanup_temp_handlers()
                return
            
            # ✅ NOW save the session (only after all checks passed)
            # Use sanitized phone number as session name
            try:
                session_name = sanitize_session_name(phone_number)
            except ValueError as e:
                logger.error(f"Invalid phone number for session name: {e}")
                await self.tbot.tbot.send_message(chat_id, "Invalid phone number format.")
                self.cleanup_temp_handlers()
                return
            
            client.session.save()
            logger.info(f"Session saved for {phone_number}")

            # Persist to the database (session string + metadata) so the account
            # survives a crash, a restart or a wiped .session file.
            await self._persist_account(client, phone_number, session_hint=session_name, me=me)

            if not isinstance(self.tbot.config['clients'], dict):
                logger.warning("'clients' is not a dictionary. Initializing it as an empty dictionary.")
                self.tbot.config['clients'] = {}

            self.tbot.config['clients'][session_name] = []
            self.tbot.config_manager.save_config(self.tbot.config)
            logger.info(f"Config saved for {phone_number}")

            # Use lock when modifying active_clients
            async with self.tbot.active_clients_lock:
                self.tbot.active_clients[session_name] = client
            logger.info(f"Added to active_clients: {phone_number}")
            
            # Set up message monitoring for this new client
            if not hasattr(client, '_message_processing_set'):
                await self.tbot.monitor.process_messages_for_client(client)
                client._message_processing_set = True

            await self.tbot.tbot.send_message(
                chat_id, 
                f"<b>Account added successfully!</b>\n\n"
                f"Phone: <code>{phone_number}</code>\n"
                f"Name: {me.first_name}\n"
                f"Username: @{me.username if me.username else 'N/A'}\n\n"
                f"The free pool now uses this account too (up to "
                f"{await self._pool_size()} accounts).",
                parse_mode="html",
            )
            logger.info(f"Account {phone_number} fully set up and verified")
            asyncio.create_task(self._warm_channels_for_new_account(session_name, chat_id))
            self.cleanup_temp_handlers()

        except Exception as e:
            logger.error(f"Error in finalize_client_setup: {e}", exc_info=True)
            await self.tbot.tbot.send_message(chat_id, f"Error occurred while finalizing setup: {str(e)[:100]}")
            self.cleanup_temp_handlers()

    async def _pool_size(self) -> int:
        """How many accounts are signed in right now (for the success message)."""
        try:
            from src.services.accounts import get_account_registry
            registry = getattr(self.tbot, "account_registry", None) or get_account_registry()
            return await registry.available_count()
        except Exception:
            return len(getattr(self.tbot, "active_clients", {}) or {})

    async def _warm_channels_for_new_account(self, session_name: str, chat_id: int) -> None:
        """Put a freshly signed-in account into every channel that needs members.

        Channels added earlier only know the old pool, so without this a new
        account would never join them and the free 8-10 account tier could never
        be reached there. Runs in the background: the sign-in flow must not wait.

        Auto-join bookkeeping is **logged, never posted into the chat**: the
        operator asked for the join successes to stay in the logs while the
        panel chat only shows what he has to act on.
        """
        try:
            from src.services.autoengage import get_auto_engagement
            outcome = await get_auto_engagement().warm_all_channels(
                reason=f"new account {session_name}", notify=False,
            )
            joined = int(outcome.get("joined") or 0)
            logger.info(
                f"New account {session_name} placed in {outcome.get('channels', 0)} "
                f"channel(s) ({joined} membership(s))"
            )
            for item in (outcome.get("details") or [])[:10]:
                logger.info(
                    f"  • {item.get('title') or item.get('channel_id')} — "
                    f"{item.get('joined')} account(s) joined"
                )
        except Exception as exc:  # noqa: BLE001 - the account is already usable
            logger.warning(f"Could not place the new account into the channels: {exc}")

    async def _persist_account(self, client, phone_number=None, session_hint=None,
                               owner_id=None, me=None):
        """Store the account (with its session string) in the database.

        Failures here are logged but never abort the login flow: the panel keeps
        working with session files, it just loses the extra recovery guarantee.
        """
        try:
            from src.services.accounts import get_account_registry
            registry = getattr(self.tbot, 'account_registry', None) or get_account_registry()
            doc_id = await registry.register(
                session_hint or phone_number or 'unknown',
                client,
                owner_id=owner_id if owner_id is not None else getattr(self.tbot, 'owner_hint', None),
                phone=phone_number,
            )
            logger.info(f"Account stored in database: {doc_id}")
            return doc_id
        except Exception as e:
            logger.error(f"Could not persist account {phone_number} to the database: {e}")
            return None

    def cleanup_temp_handlers(self, discard: bool = False):
        """
        Removes temporary handlers and data after setup completion.

        ``discard=True`` (every abandoned flow) also disconnects the sign-in
        client, so its ``.session`` file is not left open by a second client.
        """
        logger.info("cleanup_temp_handlers in AccountHandler")
        try:
            if discard:
                self._discard_temp_client()
            self.tbot.handlers.pop('temp_client', None)
            self.tbot.handlers.pop('temp_phone', None)
            self.tbot.handlers.pop('temp_phone_key', None)
            self.tbot.handlers.pop('temp_attempts', None)
            self.tbot.handlers.pop('temp_pw_attempts', None)
        except Exception as e:
            logger.error(f"Error in cleanup_temp_handlers: {e}")

    async def update_groups(self, event):
        """
        Updates group information for all clients.

        Args:
            event: Telegram event triggering the update
        """
        logger.info("Started update_groups process.")
        groups_per_client = {}
        await self.SessionManager.detect_sessions()

        try:
            status_message = await event.respond("Please wait, identifying groups for each client...")

            json_data = {
                "TARGET_GROUPS": [],
                "KEYWORDS": [],
                "IGNORE_USERS": [],
                "clients": {}
            }

            if os.path.exists(CLIENTS_JSON_PATH):
                try:
                    with open(CLIENTS_JSON_PATH, "r", encoding='utf-8') as json_file:
                        loaded_data = json.loads(json_file.read())
                        json_data.update(loaded_data)
                    logger.info("Loaded existing client data from clients.json.")
                except json.JSONDecodeError as e:
                    logger.error("Error decoding clients.json.", exc_info=True)

            # Normalize the clients section to dict format regardless of where it
            # came from (missing file, legacy list format, or new dict format).
            if isinstance(json_data["clients"], list):
                json_data["clients"] = {session: [] for session in json_data["clients"]}
            elif not isinstance(json_data["clients"], dict):
                json_data["clients"] = {}

            # Get a snapshot of active clients to avoid holding the lock during long operations
            async with self.tbot.active_clients_lock:
                active_clients_snapshot = list(self.tbot.active_clients.items())
            
            for session_name, client in active_clients_snapshot:
                try:
                    logger.info(f"Processing client: {session_name}")
                    group_ids = set()

                    # Process dialogs in batches for better performance
                    batch_count = 0
                    dialog_limit = 5000  # Reasonable limit to prevent excessive processing
                    
                    async for dialog in client.iter_dialogs(limit=dialog_limit):
                        try:
                            if isinstance(dialog.entity, (Chat, Channel)) and not (
                                isinstance(dialog.entity, Channel) and dialog.entity.broadcast
                            ):
                                group_ids.add(dialog.entity.id)
                            
                            batch_count += 1
                            
                            # Rate limiting and progress updates
                            if batch_count % GROUPS_BATCH_SIZE == 0:
                                await asyncio.sleep(1)  # Shorter sleep for better responsiveness
                                
                            if len(group_ids) % 20 == 0:
                                await status_message.edit(f"Found {len(group_ids)} groups for {session_name}...")
                                
                        except Exception as e:
                            logger.error(f"Error processing dialog for client {session_name}.", exc_info=True)
                            continue

                    groups_per_client[session_name] = list(group_ids)
                    logger.info(f"Found {len(group_ids)} groups for client {session_name}.")
                    await status_message.edit(f"Found {len(group_ids)} groups for {session_name}.")
                    await asyncio.sleep(GROUPS_UPDATE_SLEEP)

                except FloodWaitError as e:
                    wait_time = e.seconds
                    logger.warning(f"FloodWaitError: Sleeping for {wait_time} seconds for client {session_name}.")
                    await status_message.edit(f"Rate limited. Waiting for {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    continue
                except Exception as e:
                    error_msg = str(e).lower()
                    # Check if session is revoked
                    from src.utils import is_session_revoked_error, remove_revoked_session_completely
                    if is_session_revoked_error(e):
                        logger.error(f"Session revoked while updating groups for {session_name}: {e}")
                        await remove_revoked_session_completely(self.tbot, session_name)
                        continue
                    logger.error(f"Unexpected error while processing client {session_name}.", exc_info=True)
                    continue

            for session_name, group_ids in groups_per_client.items():
                existing = json_data["clients"].get(session_name)
                if isinstance(existing, dict):
                    # New format: preserve metadata (e.g. is_reported) and merge groups
                    existing_groups = existing.get('groups', [])
                    existing['groups'] = list(set(existing_groups + group_ids))
                    json_data["clients"][session_name] = existing
                elif isinstance(existing, list):
                    json_data["clients"][session_name] = list(set(existing + group_ids))
                else:
                    json_data["clients"][session_name] = group_ids

            with open(CLIENTS_JSON_PATH, "w", encoding='utf-8') as json_file:
                json.dump(json_data, json_file, indent=4, ensure_ascii=False)
                logger.info(f"Saved updated client data for {len(groups_per_client)} clients to clients.json.")

            await status_message.edit(f"That's it, groups identified and saved successfully for all clients!")

        except Exception as e:
            logger.error("Critical error in update_groups function.", exc_info=True)
            await event.respond(f"Error identifying groups: {str(e)}")


    async def check_all_accounts_report_status(self, event):
        """
        Check report status for all existing accounts.
        """
        if not REPORT_CHECK_BOT:
            await event.respond("REPORT_CHECK_BOT not configured. Please set it in start_bot.py or .env file.")
            return
        
        try:
            await event.respond("Checking report status for all accounts... This may take a while.")
            
            clients_data = self.tbot.config.get('clients', {})
            if not clients_data:
                await event.respond("No accounts found to check.")
                return
            
            reported_accounts = []
            checked_count = 0
            
            # Only check accounts that are in active_clients (skip revoked sessions)
            async with self.tbot.active_clients_lock:
                active_sessions = set(self.tbot.active_clients.keys())
                sessions_to_check = [s for s in clients_data.keys() if s in active_sessions]
            
            for session_name in sessions_to_check:
                try:
                    # Get phone number from session name
                    phone_number = session_name.replace('.session', '').strip()
                    if not phone_number.startswith('+'):
                        phone_number = '+' + phone_number
                    
                    # Get account client
                    async with self.tbot.active_clients_lock:
                        account = self.tbot.active_clients.get(session_name)
                    
                    if account:
                        checked_count += 1
                        # Check report status
                        from src.actions import Actions
                        actions = Actions(self.tbot)
                        is_reported = await actions.check_report_status(phone_number, account)
                        
                        if is_reported:
                            reported_accounts.append(session_name)
                            # Save report status to config
                            if not isinstance(clients_data[session_name], dict):
                                old_groups = clients_data[session_name]
                                clients_data[session_name] = {
                                    'groups': old_groups if isinstance(old_groups, list) else [],
                                    'is_reported': True
                                }
                            else:
                                clients_data[session_name]['is_reported'] = True
                            
                            # Save config
                            self.tbot.config_manager.save_config(self.tbot.config)
                            logger.info(f"Marked {session_name} as reported in config")
                        
                        # Small delay between checks
                        await asyncio.sleep(1)
                except Exception as e:
                    logger.error(f"Error checking report status for {session_name}: {e}")
            
            # Report results
            if reported_accounts:
                reported_phones = [acc.replace('.session', '') for acc in reported_accounts]
                result_msg = f"Checked {checked_count} accounts.\n\nReported accounts: {', '.join(reported_phones)}"
            else:
                result_msg = f"Checked {checked_count} accounts.\n\nNo reported accounts found."
            
            await event.respond(result_msg)
            
        except Exception as e:
            logger.error(f"Error in check_all_accounts_report_status: {e}", exc_info=True)
            await event.respond(f"Error checking accounts: {str(e)}")

    async def show_accounts(self, event):
        """
        Display all registered accounts with their current status and controls.

        Args:
            event: Telegram event triggering the account display
        """
        logger.info("Executing show_accounts method in AccountHandler")

        try:
            clients_data = self.tbot.config.get('clients', {})
            inactive_data = self.tbot.config.get('inactive_accounts', {})

            # Get active clients snapshot
            async with self.tbot.active_clients_lock:
                active_sessions = set(self.tbot.active_clients.keys())

            messages = []

            # Process active/inactive clients from clients_data
            if isinstance(clients_data, dict) and clients_data:
                for session, account_data in clients_data.items():
                    try:
                        # Skip revoked sessions - if session is in config but not in active_clients,
                        # and not in inactive_accounts, it's likely revoked - skip it
                        if session not in active_sessions:
                            # Check if it's in inactive_accounts
                            if 'inactive_accounts' not in self.tbot.config or session not in self.tbot.config['inactive_accounts']:
                                # Session is in config but not active and not in inactive_accounts - likely revoked, skip it
                                logger.debug(f"Skipping potentially revoked session {session} (not in active_clients and not in inactive_accounts)")
                                continue
                            else:
                                # Check if reason indicates revoked session
                                inactive_reason = self.tbot.config['inactive_accounts'][session].get('reason', '').lower()
                                if 'revoked' in inactive_reason or 'auth' in inactive_reason or 'session' in inactive_reason:
                                    logger.debug(f"Skipping revoked session {session}")
                                    continue
                        
                        phone = session.replace('.session', '') if session else 'Unknown'
                        
                        # Handle both old format (list of groups) and new format (dict)
                        if isinstance(account_data, dict):
                            groups = account_data.get('groups', [])
                            is_reported = account_data.get('is_reported', False)
                        else:
                            groups = account_data if isinstance(account_data, list) else []
                            is_reported = False
                        
                        groups_count = len(groups) if isinstance(groups, list) else 0
                        is_active = session in active_sessions
                        status = "Active" if is_active else "Inactive"
                        
                        # Add report status
                        report_status = "Reported" if is_reported else "Clean"

                        logger.debug(f"Processing account: {session}, Status: {status}, Groups: {groups_count}, Reported: {is_reported}")

                        text = (
                            f"Phone: {phone}\n"
                            f"Groups: {groups_count}\n"
                            f"Status: {status}\n"
                            f"Report: {report_status}\n"
                        )

                        buttons = Keyboard.toggle_and_delete_keyboard(status, session)
                        messages.append((text, buttons))
                    except Exception as e:
                        logger.error(f"Error processing account {session}: {e}", exc_info=True)

            # Process truly inactive accounts (those in inactive_accounts but not in clients)
            if isinstance(inactive_data, dict) and inactive_data:
                for session, account_info in inactive_data.items():
                    try:
                        # Skip if already processed above
                        if session in clients_data:
                            continue

                        phone = account_info.get('phone', session)
                        reason = account_info.get('reason', 'unknown')
                        last_seen = account_info.get('last_seen', 0)

                        # Format timestamp
                        import time
                        try:
                            last_seen_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_seen))
                        except (ValueError, OSError, Exception):
                            last_seen_str = 'Unknown'

                        status = "Inactive (Auth Error)"

                        logger.debug(f"Processing inactive account: {session}, Reason: {reason}")

                        text = (
                            f"Phone: {phone}\n"
                            f"Groups: N/A\n"
                            f"Status: {status}\n"
                            f"Reason: {reason}\n"
                            f"Last seen: {last_seen_str}\n"
                        )

                        buttons = Keyboard.toggle_and_delete_keyboard(status, session)
                        messages.append((text, buttons))
                    except Exception as e:
                        logger.error(f"Error processing inactive account {session}: {e}", exc_info=True)

            if not messages:
                await event.respond("No accounts found.")
                logger.warning("No accounts found in the configuration.")
                return

            for message_text, message_buttons in messages:
                try:
                    await event.respond(message_text, buttons=message_buttons)
                    # Safely extract session name from message text for logging
                    try:
                        parts = message_text.split(' ')
                        if len(parts) > 1:
                            logger.info(f"Sent account details for session: {parts[1]}.")
                        else:
                            logger.info(f"Sent account details message.")
                    except (IndexError, AttributeError):
                        logger.info(f"Sent account details message.")
                except Exception as e:
                    logger.error(f"Error sending message for account: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Critical error in show_accounts: {e}", exc_info=True)
            await event.respond("Error showing accounts. Please try again.")

    TRANSIENT_ERRORS = (
        (('database is locked', 'sqlite'), 'database issue', 'Database lock'),
        (('flood', 'too many'), 'Telegram limit', 'Flood control'),
        (('network', 'connection'), 'network issue', 'Network issue'),
    )

    def _mark_inactive(self, session: str, reason: str, details: str):
        self.tbot.config.setdefault('inactive_accounts', {})[session] = {
            'phone': session,
            'last_seen': time.time(),
            'reason': reason,
            'error_details': details,
        }
        self.tbot.config_manager.save_config(self.tbot.config)

    async def _disable_client(self, session: str, event):
        async with self.tbot.active_clients_lock:
            client = self.tbot.active_clients[session]
            if hasattr(self.tbot, 'monitor'):
                self.tbot.monitor.cleanup_client_handlers(client)
            await client.disconnect()
            del self.tbot.active_clients[session]
        logger.info(f"Client {session} disabled successfully.")
        await event.respond(f"Account {session} disabled.")

    async def _activate_authorized_client(self, session: str, client, event):
        async with self.tbot.active_clients_lock:
            self.tbot.active_clients[session] = client

        if hasattr(self.tbot, 'monitor') and not hasattr(client, '_message_processing_set'):
            await self.tbot.monitor.process_messages_for_client(client)
            client._message_processing_set = True

        logger.info(f"Client {session} enabled successfully.")
        # Re-persist: a re-activated account may carry a refreshed session string.
        await self._persist_account(client, session, session_hint=session)
        await event.respond(f"Account {session} activated.")

        inactive = self.tbot.config.get('inactive_accounts', {})
        if session in inactive:
            del inactive[session]
            self.tbot.config_manager.save_config(self.tbot.config)

    async def _handle_enable_error(self, session: str, error: Exception, event):
        error_msg = str(error).lower()
        logger.error(f"Error enabling client {session}: {error}")

        for needles, user_reason, log_reason in self.TRANSIENT_ERRORS:
            if any(needle in error_msg for needle in needles):
                await event.respond(
                    f"Account {session} is temporarily inactive ({user_reason}). Please try again later.")
                logger.warning(f"{log_reason} for {session}, will retry later")
                return

        if is_fatal_session_error(error):
            # Ban / block / revoked session: this one really is gone.
            self._mark_inactive(session, 'session_revoked', str(error))
            await event.respond(f"Account {session} is banned/blocked or its session was revoked.")
            return

        # Anything else is temporary: keep the account listed and retry instead of
        # marking a healthy session inactive (requirement: never auto-logout).
        logger.warning(
            f"{session} could not be activated right now ({error}) — keeping it and "
            f"retrying automatically"
        )
        await event.respond(
            f"Account {session} could not be activated right now — it stays in the "
            f"list and the panel keeps retrying it."
        )

    async def _enable_client(self, session: str, event):
        try:
            session_file = get_safe_session_file_path(session)
        except ValueError as e:
            logger.error(f"Invalid session name: {e}")
            await event.respond("Invalid session name format.")
            return

        if not os.path.exists(session_file):
            await event.respond(f"Session file for account {session} not found.")
            logger.error(f"Session file {session_file} not found")
            return

        client = TelegramClient(session, API_ID, API_HASH)
        try:
            await client.connect()
            if await client.is_user_authorized():
                await self._activate_authorized_client(session, client, event)
                return

            # "not authorized" can be a transient answer right after connecting,
            # so probe once more and only believe it when it is a fatal error.
            probe_error = None
            try:
                await client.get_me()
            except Exception as probe_exc:  # noqa: BLE001
                probe_error = probe_exc
            if probe_error is None or not is_fatal_session_error(probe_error):
                logger.warning(
                    f"Client {session} is not authorized yet — keeping it in the pool "
                    f"and retrying automatically"
                )
                await event.respond(
                    f"Account {session} is not ready yet — it stays in the pool and the "
                    f"panel keeps retrying it."
                )
                return
            await client.disconnect()
            self._mark_inactive(session, 'not_authorized_on_reactivation', 'Client not authorized')
            await event.respond(f"Account {session} has been unauthorized by Telegram.")
            logger.warning(f"Client {session} not authorized on reactivation")
        except Exception as e:
            await client.disconnect()
            await self._handle_enable_error(session, e, event)

    async def toggle_client(self, session: str, event):
        """
        Toggle the active/inactive status of a client account.

        Args:
            session (str): Session identifier for the account
            event: Telegram event triggering the toggle
        """
        logger.info(f"toggle_client called for session: {session}")
        try:
            if session not in self.tbot.config['clients']:
                logger.warning(f"Session {session} not found in clients.")
                await event.respond("Account not found.")
                return

            async with self.tbot.active_clients_lock:
                currently_active = session in self.tbot.active_clients

            logger.info(f"Current status for {session}: {'Active' if currently_active else 'Inactive'}")

            if currently_active:
                await self._disable_client(session, event)
            else:
                try:
                    await self._enable_client(session, event)
                except Exception as e:
                    logger.error(f"Error in toggle_client enable section: {e}")
                    await event.respond(f"Error activating account {session}.")

            logger.info("Saving updated configuration.")
            self.tbot.config_manager.save_config(self.tbot.config)

        except Exception as e:
            logger.error(f"Error toggling client {session}: {e}", exc_info=True)
            try:
                await event.respond("Error changing account status.")
            except Exception as resp_e:
                logger.error(f"Failed to send error response: {resp_e}")

    async def delete_client(self, session: str, event):
        """
        Permanently delete a client account and its associated data.

        Args:
            session (str): Session identifier for the account to delete
            event: Telegram event triggering the deletion
        """
        logger.info(f"delete_client called for session: {session}")
        try:
            # Check if client is active with lock
            async with self.tbot.active_clients_lock:
                is_active = session in self.tbot.active_clients
            
            if is_active:
                logger.info(f"Disconnecting active client: {session}")
                # Use lock when modifying active_clients
                async with self.tbot.active_clients_lock:
                    client = self.tbot.active_clients[session]
                    # Cleanup handlers before disconnecting
                    if hasattr(self.tbot, 'monitor'):
                        self.tbot.monitor.cleanup_client_handlers(client)
                    await client.disconnect()
                    del self.tbot.active_clients[session]
                logger.info(f"Client {session} disconnected and removed from active clients.")

            # Drop it from the database too (session string is useless now).
            try:
                from src.services.accounts import get_account_registry
                registry = getattr(self.tbot, 'account_registry', None) or get_account_registry()
                doc_id = await registry.doc_id_for(session)
                if doc_id:
                    await registry.store.remove(doc_id)
                    logger.info(f"Account {doc_id} removed from the database")
            except Exception as exc:
                logger.debug(f"Could not remove {session} from the database: {exc}")

            if session in self.tbot.config['clients']:
                logger.info(f"Removing session {session} from configuration.")
                del self.tbot.config['clients'][session]
                self.tbot.config_manager.save_config(self.tbot.config)

                try:
                    session_file = get_safe_session_file_path(session)
                    if os.path.exists(session_file):
                        try:
                            logger.info(f"Deleting session file: {session_file}")
                            os.remove(session_file)
                        except OSError as e:
                            # File is in use by another process - this is normal
                            logger.warning(f"Session file {session_file} could not be deleted (in use by another process): {e}")
                            logger.info(f"Session {session} removed from config but file remains (will be cleaned up later)")
                except ValueError as e:
                    logger.error(f"Invalid session name for deletion: {e}")

                await event.respond("Account deleted successfully.")
            else:
                logger.warning(f"Session {session} not found in configuration.")
                await event.respond("Account not found.")

        except Exception as e:
            logger.error(f"Error deleting client {session}: {e}", exc_info=True)
            await event.respond("Error deleting account.")

