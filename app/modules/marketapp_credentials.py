"""Dedicated encrypted Marketapp token; never shares the account-2FA vault."""
from pathlib import Path
import os
import re
import sqlite3

from core.secret_vault import VaultConflict, VaultError, get_vault, unlock_for_service
from modules.marketapp_catalog import CatalogError

KEY = "service/marketapp/api_token"
PASSWORD_FILE_ENV = "MARKETAPP_VAULT_PASSWORD_FILE"


def validate_token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,2048}", value):
        raise VaultError("Enter a valid Marketapp API token (no whitespace/control characters)")
    return value


class MarketappCredentials:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "marketapp_credentials"
        self.vault = get_vault(self.root)

    def __repr__(self):
        return "<MarketappCredentials>"

    def status(self):
        if not self.root.exists():
            raw_env = os.environ.get("MARKETAPP_API_TOKEN")
            valid_env = isinstance(raw_env, str) and bool(re.fullmatch(r"[\x21-\x7e]{1,2048}", raw_env))
            return {"source": "environment", "initialized": False, "unlocked": False,
                    "configured": valid_env, "idle_seconds": 600}
        try:
            state = self.vault.status()
            configured = self.vault.has(KEY) if state["unlocked"] else None
            return {"source": "dedicated_vault", "initialized": state["exists"],
                    "unlocked": state["unlocked"], "configured": configured,
                    "idle_seconds": state["idle_seconds"]}
        except (VaultError, OSError, ValueError, sqlite3.Error):
            return {"source": "dedicated_vault", "initialized": self.vault.path.exists(),
                    "unlocked": False, "configured": None, "idle_seconds": self.vault.idle_seconds,
                    "damaged": True}

    def store(self, token, *, confirmed=False):
        if confirmed is not True:
            raise VaultError("Confirm local token replacement")
        self.vault.put_many({KEY: validate_token(token)}, replace=True)

    def remove(self, *, confirmed=False):
        if confirmed is not True:
            raise VaultError("Confirm local token removal (not provider revocation)")
        self.vault.mutate(lambda records: records.pop(KEY, None))

    def restore(self, path, password):
        if self.vault.path.exists() and self.vault.unlocked and not self.vault.has(KEY):
            self.vault.verify_backup(path, password)
            with self.vault._mutex:
                self.vault.lock(manual=False)
                for suffix in ("", "-wal", "-shm"):
                    candidate = self.vault.path.with_name(self.vault.path.name + suffix)
                    candidate.unlink(missing_ok=True)
        self.vault.restore_new(path, password, allowed_keys={KEY})

    def get(self):
        try:
            if not self.root.exists():
                return validate_token(os.environ.get("MARKETAPP_API_TOKEN"))
            # Once opted in, locked/empty/damaged storage NEVER falls back to env.
            if not self.vault.path.exists():
                raise VaultError("Dedicated credential storage is incomplete")
            external = os.environ.get(PASSWORD_FILE_ENV)
            if external:
                ext_path = Path(external).resolve(strict=True)
                app_dir = Path(__file__).resolve().parents[1]
                repo_dir = Path(__file__).resolve().parents[2]
                if ext_path.is_relative_to(self.data_dir) or ext_path.is_relative_to(app_dir) or ext_path.is_relative_to(repo_dir):
                    raise VaultError("Master password file must be outside repository and runtime data")
            unlock_for_service(self.vault, password_file_env=PASSWORD_FILE_ENV)
            return validate_token(self.vault.get(KEY))
        except (VaultError, OSError, ValueError, sqlite3.Error):
            raise CatalogError("Marketapp token unavailable: unlock/configure its dedicated vault locally or check the external password file; no environment fallback after opt-in") from None
