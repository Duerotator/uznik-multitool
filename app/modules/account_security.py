from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.storage import update_json
from core.telegram_client import create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from modules.email_inbox import EmailInboxClient, generate_recovery_email
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error


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
            await asyncio.sleep(base_delay * (attempt + 1))


class AccountSecurityService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("security")

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
                        {"cloud_password": True, "cloud_password_checked_at": str(int(time.time()))},
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
                    disable = is_invalid_auth_error(exc)
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
                        {"cloud_password": False, "cloud_password_checked_at": str(int(time.time()))},
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
                    disable = is_invalid_auth_error(exc)
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
                    self.log.info("Registered passkey for %s (ID: %s)", account.id, res["passkey_id"][:16])
                except Exception as exc:
                    error = _security_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc)
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
        if not domain:
            raise RuntimeError("Email domain is required.")
        if not inbox_api_url:
            raise RuntimeError("Inbox API URL is required.")
        inbox = EmailInboxClient(inbox_api_url, inbox_token, timeout=code_timeout)
        await inbox.healthcheck()
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                email = generate_recovery_email(domain, account.id)
                started_at = int(time.time())

                async def code_provider(target_email: str, length: int | None) -> str:
                    # The generated address is unique for this attempt.  A
                    # small overlap is therefore safe and avoids losing a
                    # just-arrived code when the Inbox worker clock trails
                    # the desktop clock by a few seconds.
                    return await inbox.wait_code(target_email, after=max(0, started_at - 60), length=length)

                try:
                    self.log.info("Binding recovery email for %s: %s", account.id, email)

                    async def operation() -> None:
                        async with create_client(self.config, account) as client:
                            await client.set_recovery_email(
                                email=email,
                                code_provider=code_provider,
                                new_password=new_password or None,
                                hint=hint,
                                current_password=current_password or None,
                            )

                    await _with_network_retries(operation)
                    self.accounts.update_profile_metadata(
                        account.id,
                        {"recovery_email": email, "recovery_email_bound_at": str(started_at)},
                    )
                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                    result.details[account.id] = email
                    self.log.info("Recovery email bound for %s: %s", account.id, email)
                except Exception as exc:
                    error = _security_error(exc)
                    result.add_error(account.id, f"{email}: {error}")
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc)
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
        if not domain:
            raise RuntimeError("Email domain is required.")
        if not inbox_api_url:
            raise RuntimeError("Inbox API URL is required.")
        inbox = EmailInboxClient(inbox_api_url, inbox_token, timeout=code_timeout)
        await inbox.healthcheck()
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        result = ActionResult()

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                email = generate_recovery_email(domain, account.id)
                started_at = int(time.time())

                async def code_provider(target_email: str, length: int | None) -> str:
                    # Every generated address is unique for this attempt, so a
                    # small overlap is safe and absorbs clock skew between the
                    # desktop and Cloudflare Worker.
                    return await inbox.wait_code(target_email, after=max(0, started_at - 60), length=length)

                try:
                    self.log.info("Changing login email for %s: %s", account.id, email)

                    async def operation() -> None:
                        async with create_client(self.config, account) as client:
                            await client.set_login_email(email=email, code_provider=code_provider)

                    await _with_network_retries(operation)
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
                    self.log.info("Login email changed for %s: %s", account.id, email)
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
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    self.log.error("Login email failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts if account.enabled))
        return result
