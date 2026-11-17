from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from dataclasses import dataclass, field

from core.config import AppConfig
from core.models import AccountRecord, utc_now_iso
from core.results import ActionResult
from core.storage import update_json
from core.telegram_client import AccountBusyError, create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.email_inbox import create_email_inbox
from modules.email_mailbox import MailboxError
from utils.rate_limit import human_delay
from utils.telegram_errors import is_frozen_error, is_invalid_auth_error, short_error

EMAIL_OPERATION_OVERHEAD = 90.0
EMAIL_SESSION_WAIT_TIMEOUT = 30.0

def passkey_restore_candidates(accounts: list[AccountRecord]) -> list[AccountRecord]:
    """Only narrow the caller's scope; never discover accounts in other groups."""
    return [account for account in accounts if (
        not account.enabled
        or str(account.metadata.get("health_status") or "").lower() in {"invalid", "error", "revoked"}
        or account.metadata.get("last_error")
    ) and str(account.metadata.get("health_status") or "").lower() != "frozen"]


def _security_error(exc: BaseException) -> str:
    text = f"{exc.__class__.__name__}: {exc}"
    if "EMAIL_NOT_SETUP" in text:
        return (
            "Login email is not set on this account. Telegram API can change an existing "
            "login email, but initial setup requires the phone login flow."
        )
    password_markers = (
        "PASSWORD_HASH_INVALID",
        "PASSWORD_INVALID",
        "password is invalid",
        "invalid password",
        "wrong password",
    )
    if any(marker.lower() in text.lower() for marker in password_markers):
        return "Wrong 2FA password for this account."
    return short_error(exc)


def _is_transient_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (MailboxError, AccountBusyError)):
        return False
    text = f"{exc.__class__.__name__}: {exc}".lower()
    if "no email code received" in text:
        return False
    markers = (
        "connecttimeout",
        "readtimeout",
        "timeout",
        "connection lost",
        "connectionreseterror",
        "connection aborted",
        "socket.send",
        "network",
    )
    return any(marker in text for marker in markers)


async def _with_network_retries(operation, *, attempts: int = 3, base_delay: float = 8.0):
    for attempt in range(attempts):
        try:
            return await operation()
        except Exception as exc:
            if not _is_transient_network_error(exc) or attempt == attempts - 1:
                raise
            logging.getLogger("security").warning(
                "Network operation failed (%s); retry %s/%s in %.0fs",
                type(exc).__name__, attempt + 2, attempts, base_delay * (attempt + 1),
            )
            await asyncio.sleep(base_delay * (attempt + 1))


async def _run_email_operation(operation, code_timeout: float):
    try:
        return await asyncio.wait_for(
            _with_network_retries(operation), timeout=code_timeout + EMAIL_OPERATION_OVERHEAD
        )
    except TimeoutError as exc:
        if not str(exc):
            raise TimeoutError("Email operation timed out. Check the last mailbox/Telegram stage in the log.") from None
        raise




def security_scan_candidates(accounts: list[AccountRecord], selected_ids: list[str]) -> list[AccountRecord]:
    wanted = set(selected_ids)
    return [account for account in accounts if account.enabled and (not wanted or account.id in wanted)]


@dataclass
class SecurityScanResult(ActionResult):
    observations: dict[str, dict] = field(default_factory=dict)


class AccountSecurityService:
    async def scan_security(
        self, accounts: list[AccountRecord], *, progress: OperationProgress | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> ActionResult:
        """Manual read-only Guard foundation, never an account recovery guarantee."""
        result = SecurityScanResult()
        # Preserve caller scope and order, ignore duplicate/disabled entries.
        scoped = {account.id: account for account in accounts if account.enabled}
        for account in scoped.values():
            if stop_event is not None and stop_event.is_set():
                break
            freeze_meta: dict[str, object] = {}
            try:
                async with asyncio.timeout(60):
                    async with create_client(self.config, account) as client:
                        try:
                            snapshot = await client.inspect_security()
                        except Exception as inspect_exc:
                            if is_frozen_error(inspect_exc) and hasattr(client, "get_app_config"):
                                try:
                                    app_cfg = await asyncio.wait_for(client.get_app_config(), timeout=10)
                                    for key in ("freeze_since_date", "freeze_until_date", "freeze_appeal_url"):
                                        if app_cfg.get(key):
                                            freeze_meta[key] = app_cfg[key]
                                except Exception:
                                    pass
                            raise
                        if account.backend == "pyrogram" and hasattr(client, "client") and getattr(client, "client", None) is not None:
                            try:
                                from core.passkey_service import PasskeyService
                                pk_dir = str(self.config.data_dir / "passkeys")
                                pk_svc = PasskeyService(self.config.api_id, self.config.api_hash, passkeys_dir=pk_dir)
                                remote_pks = await pk_svc.get_remote_passkeys(client.client)
                                remote_ids = [str(getattr(pk, "id", "")) for pk in remote_pks if getattr(pk, "id", None)]
                                local_id = None
                                try:
                                    local_id = str(pk_svc.load_passkey(account.id).get("passkey_id") or "")
                                except FileNotFoundError:
                                    local_id = None
                                unknown_pks = [pid for pid in remote_ids if pid != local_id]
                                snapshot["passkeys_count"] = len(remote_ids)
                                snapshot["passkey_ids"] = remote_ids
                                snapshot["unknown_passkeys"] = unknown_pks
                            except Exception:
                                pass
                review = (
                    snapshot["password_reset_pending"]
                    or snapshot["other_sessions"] > 0
                    or not snapshot["twofa_enabled"]
                    or not snapshot["recovery_email_set"]
                    or bool(snapshot.get("unknown_passkeys"))
                )
                snapshot.update(checked_at=utc_now_iso(), status="needs_review" if review else "observed")
                self.accounts.update_profile_metadata(account.id, {"security_scan": snapshot, "security_scan_error": None})
                result.add_ok(account.id)
                result.observations[account.id] = snapshot
                result.details[account.id] = (
                    f"2FA={'on' if snapshot['twofa_enabled'] else 'off'}, "
                    f"other sessions={snapshot['other_sessions']}, "
                    f"unconfirmed={snapshot['unconfirmed_sessions']}, "
                    f"passkeys={snapshot.get('passkeys_count', 0)}"
                    f"{' (UNKNOWN!)' if snapshot.get('unknown_passkeys') else ''}, "
                    f"2FA reset={'PENDING' if snapshot['password_reset_pending'] else 'not observed'}. "
                    "Read-only; SIM ownership and account deletion protection are not verified."
                )
                self.log.info("%s: %s", account.id, result.details[account.id])
                if progress:
                    progress.mark_ok(account.id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not mark old observations fresh, disable, restore or delete
                # accounts because a read-only scan timed out or was rate-limited.
                err = short_error(exc)
                result.add_error(account.id, err)
                if is_frozen_error(exc):
                    self.accounts.update_profile_metadata(account.id, {
                        "health_status": "frozen",
                        "health_checked_at": utc_now_iso(),
                        "security_scan_error": {"at": utc_now_iso(), "kind": type(exc).__name__},
                        **freeze_meta,
                    })
                    self.accounts.mark_error(account.id, err, disable=True)
                else:
                    self.accounts.update_profile_metadata(account.id, {
                        "security_scan_error": {"at": utc_now_iso(), "kind": type(exc).__name__},
                    })
                if progress:
                    progress.mark_error(account.id)
        return result

    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("security")

    def _email_inbox(self, domain: str, api_url: str, token: str, timeout: float):
        owners: dict[str, set[str]] = {}
        if getattr(self.config, "email_inbox_backend", "http") != "http":
            for account in self.accounts.list_accounts():
                for key in ("login_email", "recovery_email"):
                    address = str(account.metadata.get(key) or "").strip().lower()
                    if address:
                        owners.setdefault(address, set()).add(account.id)
        return create_email_inbox(self.config, domain=domain, api_url=api_url,
                                  token=token, timeout=timeout, owners=owners)

    async def terminate_other_sessions(
        self,
        accounts: list[AccountRecord],
        desktop_only: bool = False,
        *,
        progress: OperationProgress | None = None,
    ) -> ActionResult:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    async with create_client(self.config, account) as client:
                        removed = await client.terminate_other_sessions(desktop_only=desktop_only)
                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    result.details[account.id] = f"removed={removed}"
                    self.log.info("Terminated %s other session(s) for %s", removed, account.id)
                except Exception as exc:
                    error = short_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Terminate sessions failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result

    async def set_cloud_password(
        self,
        accounts: list[AccountRecord],
        new_password: str,
        hint: str = "",
        current_password: str | None = None,
        *,
        progress: OperationProgress | None = None,
    ) -> ActionResult:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    async def operation() -> None:
                        async with create_client(self.config, account) as client:
                            await client.set_cloud_password(
                                new_password=new_password,
                                hint=hint,
                                current_password=current_password or None,
                            )

                    await _with_network_retries(operation)
                    self.accounts.update_profile_metadata(
                        account.id,
                        {
                            "cloud_password": True,
                            "cloud_password_val": None,
                            "cloud_password_checked_at": str(int(time.time())),
                        },
                    )
                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    self.log.info("Cloud password updated for %s", account.id)
                except Exception as exc:
                    error = _security_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc) or is_frozen_error(exc)
                    if is_frozen_error(exc):
                        self.accounts.update_profile_metadata(
                            account.id, {"health_status": "frozen", "health_checked_at": utc_now_iso()}
                        )
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Cloud password failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result

    async def remove_cloud_password(
        self,
        accounts: list[AccountRecord],
        current_password: str,
        *,
        progress: OperationProgress | None = None,
    ) -> ActionResult:
        if not current_password:
            raise RuntimeError("Current 2FA password is required.")
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    async def operation() -> None:
                        async with create_client(self.config, account) as client:
                            await client.remove_cloud_password(current_password)

                    await _with_network_retries(operation)
                    self.accounts.update_profile_metadata(
                        account.id,
                        {
                            "cloud_password": False,
                            "cloud_password_val": None,
                            "cloud_password_checked_at": str(int(time.time())),
                        },
                    )
                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    self.log.info("Cloud password removed for %s", account.id)
                except Exception as exc:
                    error = _security_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc) or is_frozen_error(exc)
                    if is_frozen_error(exc):
                        self.accounts.update_profile_metadata(
                            account.id, {"health_status": "frozen", "health_checked_at": utc_now_iso()}
                        )
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Remove cloud password failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result

    async def delete_passkeys(
        self, accounts: list[AccountRecord], *, progress: OperationProgress | None = None,
    ) -> ActionResult:
        import os
        import json
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()
        passkeys_dir = os.path.join(str(self.config.data_dir), "passkeys")
        manifest_file = os.path.join(passkeys_dir, "manifest.json")

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    async def operation() -> int:
                        async with create_client(self.config, account) as client:
                            return await client.delete_passkeys()

                    removed = await _with_network_retries(operation)

                    # Remove local passkey file
                    local_file = os.path.join(passkeys_dir, f"{account.id}.json")
                    if os.path.exists(local_file):
                        try:
                            os.remove(local_file)
                        except Exception:
                            pass

                    # Remove from manifest
                    if os.path.exists(manifest_file):
                        def remove_entry(manifest):
                            manifest.pop(account.id, None)
                            return manifest
                        update_json(Path(manifest_file), {}, remove_entry)

                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    result.details[account.id] = f"removed={removed}"
                    self.log.info("Deleted %s passkey(s) for %s", removed, account.id)
                except Exception as exc:
                    error = _security_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Delete passkeys failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result

    async def add_passkeys(
        self, accounts: list[AccountRecord], *, progress: OperationProgress | None = None,
    ) -> ActionResult:
        import os
        import json
        from core.passkey_service import PasskeyService
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()
        passkeys_dir = os.path.join(str(self.config.data_dir), "passkeys")
        os.makedirs(passkeys_dir, exist_ok=True)
        manifest_file = os.path.join(passkeys_dir, "manifest.json")
        passkey_service = PasskeyService(
            self.config.api_id,
            self.config.api_hash,
            passkeys_dir=passkeys_dir
        )

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                try:
                    async def operation() -> dict:
                        if account.backend != "pyrogram":
                            raise ValueError("Passkey registration currently supports Kurigram accounts only")
                        async with create_client(self.config, account) as client:
                            return await passkey_service.register_passkey(client.client, account.id)

                    res = await _with_network_retries(operation)

                    # Update manifest
                    entry = {
                        "account_id": account.id,
                        "passkey_id": res["passkey_id"],
                        "user_id": res["user_id"],
                        "dc_id": res["dc_id"],
                        "phone": res["phone"],
                        "username": res["username"],
                        "secured_at": res["created_at"],
                        "passkey_file": f"{account.id}.json"
                    }
                    def add_entry(manifest):
                        manifest[account.id] = entry
                        return manifest
                    update_json(Path(manifest_file), {}, add_entry)

                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    result.details[account.id] = f"passkey_id={res['passkey_id']}"
                    self.log.info("Registered passkey for %s", account.id)
                except Exception as exc:
                    error = _security_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc) or is_frozen_error(exc)
                    if is_frozen_error(exc):
                        self.accounts.update_profile_metadata(
                            account.id, {"health_status": "frozen", "health_checked_at": utc_now_iso()}
                        )
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Add passkeys failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result

    async def restore_passkeys(
        self, accounts: list[AccountRecord], *, progress: OperationProgress | None = None,
    ) -> ActionResult:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()
        from modules.session_health import SessionHealthService
        health_service = SessionHealthService(self.config)

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                try:
                    restored = await health_service._try_restore_revoked_session(account)
                    if restored:
                        result.add_ok(account.id)
                        if progress:
                            progress.mark_ok(account.id)
                        self.log.info("Restored account %s via passkey", account.id)
                    else:
                        err = "No passkey found or restoration failed"
                        result.add_error(account.id, err)
                        if progress:
                            progress.mark_error(account.id)
                except Exception as exc:
                    err = _security_error(exc)
                    result.add_error(account.id, err)
                    if progress:
                        progress.mark_error(account.id)

        await asyncio.gather(*(worker(account) for account in accounts))
        return result

    async def bind_recovery_emails(
        self,
        accounts: list[AccountRecord],
        domain: str,
        inbox_api_url: str,
        inbox_token: str,
        new_password: str | None = None,
        hint: str = "",
        current_password: str | None = None,
        code_timeout: float = 90.0,
        progress: OperationProgress | None = None,
    ) -> ActionResult:
        inbox = self._email_inbox(domain, inbox_api_url, inbox_token, code_timeout)
        await inbox.healthcheck()
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                email = ""
                started_at = int(time.time())

                async def code_provider(target_email: str, length: int | None) -> str:
                    # HTTP uses unique aliases and a clock-skew allowance;
                    # IMAP/POP3 exclude old mail using a pre-request snapshot.
                    overlap = 60 if getattr(self.config, "email_inbox_backend", "http") == "http" else 0
                    self.log.info("%s: waiting for recovery email code (up to %.0fs)", account.id, code_timeout)
                    code = await inbox.wait_code(target_email, after=max(0, started_at - overlap), length=length)
                    self.log.info("%s: recovery email code received; confirming with Telegram", account.id)
                    return code

                try:
                    email = inbox.address_for(account.id)
                    self.log.info("Binding recovery email for %s", account.id)
                    await inbox.prepare(email)
                    self.log.info("%s: mailbox ready; connecting to Telegram for recovery email", account.id)

                    async def operation() -> None:
                        async with create_client(self.config, account, lock_wait_timeout=EMAIL_SESSION_WAIT_TIMEOUT) as client:
                            await client.set_recovery_email(
                                email=email,
                                code_provider=code_provider,
                                new_password=new_password or None,
                                hint=hint,
                                current_password=current_password or None,
                            )

                    await _run_email_operation(operation, code_timeout)
                    self.accounts.update_profile_metadata(
                        account.id,
                        {"recovery_email": email, "recovery_email_bound_at": str(started_at)},
                    )
                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    result.details[account.id] = email
                    self.log.info("Recovery email bound for %s", account.id)
                except Exception as exc:
                    error = _security_error(exc)
                    result.add_error(account.id, f"{email}: {error}")
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc) or is_frozen_error(exc)
                    if is_frozen_error(exc):
                        self.accounts.update_profile_metadata(
                            account.id, {"health_status": "frozen", "health_checked_at": utc_now_iso()}
                        )
                    if not isinstance(exc, AccountBusyError):
                        self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Recovery email failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result

    async def change_login_emails(
        self,
        accounts: list[AccountRecord],
        domain: str,
        inbox_api_url: str,
        inbox_token: str,
        code_timeout: float = 90.0,
        progress: OperationProgress | None = None,
    ) -> ActionResult:
        inbox = self._email_inbox(domain, inbox_api_url, inbox_token, code_timeout)
        await inbox.healthcheck()
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                email = ""
                started_at = int(time.time())

                async def code_provider(target_email: str, length: int | None) -> str:
                    # HTTP uses unique aliases and a clock-skew allowance;
                    # IMAP/POP3 exclude old mail using a pre-request snapshot.
                    overlap = 60 if getattr(self.config, "email_inbox_backend", "http") == "http" else 0
                    self.log.info("%s: waiting for login email code (up to %.0fs)", account.id, code_timeout)
                    code = await inbox.wait_code(target_email, after=max(0, started_at - overlap), length=length)
                    self.log.info("%s: login email code received; confirming with Telegram", account.id)
                    return code

                try:
                    email = inbox.address_for(account.id)
                    self.log.info("Changing login email for %s", account.id)
                    await inbox.prepare(email)
                    self.log.info("%s: mailbox ready; connecting to Telegram for login email", account.id)

                    async def operation() -> None:
                        async with create_client(self.config, account, lock_wait_timeout=EMAIL_SESSION_WAIT_TIMEOUT) as client:
                            await client.set_login_email(email=email, code_provider=code_provider)

                    await _run_email_operation(operation, code_timeout)
                    self.accounts.update_profile_metadata(
                        account.id,
                        {
                            "login_email": email,
                            "login_email_status": "custom",
                            "login_email_bound_at": str(started_at),
                        },
                    )
                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    result.details[account.id] = email
                    self.log.info("Login email changed for %s", account.id)
                except Exception as exc:
                    error = _security_error(exc)
                    if "EMAIL_NOT_SETUP" in f"{exc} {error}":
                        self.accounts.update_profile_metadata(
                            account.id,
                            {"login_email_status": "absent"},
                        )
                    result.add_error(account.id, f"{email}: {error}")
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc) or is_frozen_error(exc)
                    if is_frozen_error(exc):
                        self.accounts.update_profile_metadata(
                            account.id, {"health_status": "frozen", "health_checked_at": utc_now_iso()}
                        )
                    if not isinstance(exc, AccountBusyError):
                        self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Login email failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result
