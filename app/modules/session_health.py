from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

from core.config import AppConfig
from core.models import AccountRecord, ProxyConfig, utc_now_iso
from core.results import ActionResult
from core.telegram_client import create_client, resolve_session_path, session_lock
from core.storage import write_json_atomic
from modules.accounts import AccountService
from utils.telegram_errors import is_invalid_auth_error, short_error


class SessionHealthService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("session-health")
        self.invalid_session_ids: set[str] = set()
        self.frozen_session_ids: set[str] = set()

    async def _try_restore_revoked_session(self, account: AccountRecord) -> bool:
        """Verify a replacement session before touching the original; never login direct."""
        if account.backend != "pyrogram" or account.session_kind not in {"session_file", "session_string"}:
            self.log.warning("Passkey restore does not support %s/%s for %s", account.backend, account.session_kind, account.id)
            return False
        passkeys_dir = os.path.join(str(self.config.data_dir), "passkeys")
        key_file = os.path.join(passkeys_dir, f"{account.id}.json")
        if not os.path.exists(key_file):
            self.log.info("No passkey file found for revoked account %s", account.id)
            return False

        async with session_lock(account):
            try:
                with open(key_file, "r", encoding="utf-8") as f:
                    pk_data = json.load(f)
                expected_id = int(account.user_id or pk_data.get("user_id") or 0)
                if not expected_id or int(pk_data.get("user_id") or 0) != expected_id:
                    raise ValueError("Saved Passkey identity does not match the account")
                if pk_data.get("account_id") not in (None, account.id):
                    raise ValueError("Saved Passkey belongs to another account")

                transport = create_client(self.config, account)
                proxy = None
                for attempt in range(3):
                    proxy = ProxyConfig.from_url(account.proxy or self.config.global_proxy)
                    if proxy and await transport._proxy_usable(proxy):
                        break
                    if attempt == 2:
                        raise RuntimeError("No working proxy for Passkey restore; direct connection is forbidden")
                    await transport._replace_proxy()

                from core.passkey_service import PasskeyService
                service = PasskeyService(self.config.api_id, self.config.api_hash, passkeys_dir=passkeys_dir)
                original = resolve_session_path(self.config, account.session_ref).resolve()
                original.parent.mkdir(parents=True, exist_ok=True)
                two_fa = account.metadata.get("cloud_password_val") or getattr(self.config, "two_fa_password", None)
                with tempfile.TemporaryDirectory(prefix=".passkey-restore-", dir=original.parent) as temp_dir:
                    client = None
                    try:
                        client = await service.login_with_passkey(
                            passkey_data=pk_data, two_fa_password=two_fa,
                            session_name="replacement", save_session_dir=temp_dir,
                            proxy=proxy.to_pyrogram(),
                        )
                        me = await asyncio.wait_for(client.get_me(), timeout=30)
                        if int(me.id) != expected_id:
                            raise ValueError("Restored session belongs to another Telegram user")
                        replacement = Path(temp_dir) / "replacement.session"
                        if account.session_kind == "session_string":
                            replacement = Path(temp_dir) / "replacement.json"
                            session_string = await client.export_session_string()
                            write_json_atomic(replacement, {
                                "backend": "pyrogram", "session_string": session_string,
                                "phone": me.phone_number or account.phone or "",
                            })
                    finally:
                        if client is not None:
                            await asyncio.wait_for(client.disconnect(), timeout=15)

                    # The client/storage must be closed before replacement (Windows/SQLite).
                    if not replacement.is_file():
                        raise RuntimeError("Passkey client did not produce a replacement session")
                    backup_suffix = f".pre-passkey-{uuid4().hex}.bak"
                    if original.exists():
                        shutil.copy2(original, original.with_name(original.name + backup_suffix))
                    # Retain stale SQLite sidecars as backups rather than letting them attach
                    # to the newly authorized database. This happens only after verification.
                    moved_sidecars = []
                    try:
                        for suffix in ("-wal", "-shm", "-journal") if account.session_kind == "session_file" else ():
                            sidecar = Path(str(original) + suffix)
                            if sidecar.exists():
                                backup = sidecar.with_name(sidecar.name + backup_suffix)
                                os.replace(sidecar, backup)
                                moved_sidecars.append((sidecar, backup))
                        os.replace(replacement, original)
                    except BaseException:
                        for sidecar, backup in reversed(moved_sidecars):
                            os.replace(backup, sidecar)
                        raise

                self.accounts.update_account_runtime_identity(
                    account.id, username=me.username, first_name=me.first_name,
                    last_name=me.last_name, user_id=me.id, phone=me.phone_number or account.phone,
                )
                self.accounts.update_profile_metadata(account.id, {
                    "health_status": "valid", "health_checked_at": utc_now_iso(),
                    "restored_via_passkey_at": utc_now_iso(),
                })
                self.accounts.clear_error(account.id)
                self.accounts.set_enabled([account.id], True)
                self.log.info("Restored session %s via Passkey; original retained as backup", account.id)
                return True
            except Exception as exc:
                self.log.error("Failed to restore session %s via Passkey: %s", account.id, short_error(exc))
                return False

    async def validate(self, accounts: list[AccountRecord], progress=None) -> ActionResult:
        self.invalid_session_ids.clear()
        self.frozen_session_ids.clear()
        result = ActionResult()
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))

        async def worker(account: AccountRecord) -> None:
            async with semaphore:
                try:
                    async with create_client(self.config, account) as client:
                        identity = await client.get_me()
                        self.accounts.update_account_runtime_identity(
                            account.id,
                            username=identity.get("username"),
                            first_name=identity.get("first_name"),
                            last_name=identity.get("last_name"),
                            user_id=identity.get("user_id"),
                            phone=identity.get("phone"),
                        )
                        try:
                            await client.probe_frozen()
                        except Exception as probe_err:
                            if "FROZEN" in str(probe_err).upper():
                                raise
                        security = await client.get_security_state()
                        self.accounts.update_profile_metadata(
                            account.id,
                            {
                                "health_status": "valid",
                                "health_checked_at": utc_now_iso(),
                                "cloud_password": security.get("cloud_password"),
                                "recovery_email_hint": security.get("recovery_email_hint"),
                                "language_code": identity.get("language_code"),
                            },
                        )
                        self.accounts.clear_error(account.id)
                    result.add_ok(account.id)
                    if progress: progress.mark_ok(account.id)
                except Exception as exc:
                    error = short_error(exc)
                    msg = str(exc).upper()
                    if "FROZEN" in msg:
                        self.frozen_session_ids.add(account.id)
                        self.accounts.update_profile_metadata(
                            account.id,
                            {"health_status": "frozen", "health_checked_at": utc_now_iso()},
                        )
                        self.accounts.mark_error(account.id, error, disable=True)
                        self.log.warning("Frozen session %s: %s", account.id, error)
                        result.add_error(account.id, error)
                        if progress:
                            progress.mark_error(account.id, error)
                    else:
                        is_revoked = any(k in msg for k in ("REVOKED", "AUTH_KEY_UNREGISTERED", "SESSION_EXPIRED", "DEACTIVATED"))
                        if is_revoked:
                            self.log.info("Session %s appears revoked (%s). Attempting passkey restoration...", account.id, error)
                            restored = await self._try_restore_revoked_session(account)
                            if restored:
                                result.add_ok(account.id)
                                if progress:
                                    progress.mark_ok(account.id)
                                return

                        disable = is_invalid_auth_error(exc)
                        if disable:
                            self.invalid_session_ids.add(account.id)
                        self.accounts.update_profile_metadata(
                            account.id,
                            {"health_status": "invalid" if disable else "error", "health_checked_at": utc_now_iso()},
                        )
                        self.accounts.mark_error(account.id, error, disable=disable)
                        result.add_error(account.id, error)
                        if progress:
                            progress.mark_error(account.id, error)
                        if disable:
                            self.log.warning("Disabled invalid session %s: %s", account.id, error)
                        else:
                            self.log.warning("Session check failed for %s: %s", account.id, error)

        await asyncio.gather(*(worker(account) for account in accounts))
        return result
