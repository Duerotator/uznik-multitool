"""Account credential adoption is explicit; no Telegram writes or guessed passwords."""
from __future__ import annotations

import hashlib
import json
import time

from core.secret_vault import VaultConflict, VaultError, get_vault, unlock_for_service
from modules.accounts import AccountService

FIELD = "cloud_password_val"
FIELDS = "vault_fields"
MANAGED = "vault_2fa_managed"
PENDING = "vault_2fa_pending"


def protected_fields(account):
    value = account.metadata.get(FIELDS, [])
    if not isinstance(value, list) or any(item != FIELD for item in value):
        raise VaultError("Unsupported account vault references")
    return set(value)


def managed_2fa(account):
    value = account.metadata.get(MANAGED, False)
    if type(value) is not bool:
        raise VaultError("Invalid account vault marker")
    return value or FIELD in protected_fields(account)


def account_key(account_id, field=FIELD):
    return "account/"+hashlib.sha256(account_id.encode()).hexdigest()+"/"+field


def get_account_password(config, account):
    if not managed_2fa(account):
        value = account.metadata.get(FIELD)
        return value if isinstance(value, str) and value else None
    vault = get_vault(config.data_dir)
    unlock_for_service(vault)
    if account.metadata.get(PENDING) or vault.get(account_key(account.id, "2fa_change")):
        raise VaultConflict("2FA change is unresolved; review the encrypted candidate before autofill")
    if FIELD not in protected_fields(account):
        return None  # opted-in account: no fallback to a shared/default password
    value = vault.get(account_key(account.id))
    if not value:
        raise VaultError("Account references a missing vault credential")
    return value


class AccountSecretManager:
    def __init__(self, config):
        self.config = config
        self.accounts = AccountService(config)
        self.vault = get_vault(config.data_dir)

    def _targets(self, ids):
        if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or not key for key in ids):
            raise VaultError("Select explicit account IDs")
        wanted = set(ids)
        accounts = [account for account in self.accounts.list_accounts() if account.id in wanted]
        if {account.id for account in accounts} != wanted:
            raise VaultConflict("An account was removed; refresh the selection")
        for account in accounts:
            protected_fields(account)
        return accounts

    def copy_legacy(self, ids):
        targets = self._targets(ids)
        entries = {account_key(account.id): account.metadata[FIELD] for account in targets
                   if isinstance(account.metadata.get(FIELD), str) and account.metadata[FIELD]}
        if entries:
            self.vault.put_many(entries)
        return {"copied": len(entries), "skipped": len(targets)-len(entries),
                "detail": "Encrypted copies saved. Plaintext and runtime selection were not changed."}

    def move_legacy(self, ids, backup, *, confirmed=False):
        if confirmed is not True:
            raise VaultError("Explicit plaintext removal confirmation is required")
        targets = self._targets(ids)
        selected = {account.id: account.metadata[FIELD] for account in targets
                    if isinstance(account.metadata.get(FIELD), str) and account.metadata[FIELD]}
        if not selected:
            return {"moved": 0, "detail": "No selected legacy plaintext values found."}
        entries = {account_key(key): value for key, value in selected.items()}
        with self.vault.backed_entries(entries, backup):
            def mutate(accounts):
                found = set()
                for account in accounts:
                    if account.id not in selected:
                        continue
                    if account.metadata.get(FIELD) != selected[account.id] or account.metadata.get(PENDING):
                        raise VaultConflict("A selected credential changed or needs review; no plaintext was removed")
                    fields = protected_fields(account)
                    account.metadata[FIELDS] = sorted(fields | {FIELD})
                    account.metadata[MANAGED] = True
                    account.metadata.pop(FIELD)
                    found.add(account.id)
                if found != set(selected):
                    raise VaultConflict("Selected accounts changed; no plaintext was removed")
                return accounts
            self.accounts._update(mutate)
        return {"moved": len(selected), "detail": "Selected current metadata now references the vault. Old backups/imports were not erased."}

    def store_current(self, ids, value, *, confirmed=False):
        if confirmed is not True or not isinstance(value, str) or not value:
            raise VaultError("Explicit confirmation and a password are required")
        targets = self._targets(ids)
        if any(account.metadata.get(FIELD) for account in targets):
            raise VaultConflict("Copy and move legacy plaintext through a verified backup first")
        def operation(records):
            for account in targets:
                if account_key(account.id, "2fa_change") in records or account.metadata.get(PENDING):
                    raise VaultConflict("Resolve the pending 2FA candidate first")
            for account in targets:
                records[account_key(account.id)] = value
        self.vault.mutate(operation)
        for account in targets:
            self.accounts.update_profile_metadata(account.id, {
                FIELDS: sorted(protected_fields(account) | {FIELD}), MANAGED: True,
            })
        return {"stored": len(targets), "detail": "Stored locally only; Telegram password was not changed or verified."}

    def prepare_change(self, account, *, new_password=None, remove=False):
        unlock_for_service(self.vault)
        def operation(records):
            key = account_key(account.id, "2fa_change")
            if key in records or account.metadata.get(PENDING):
                raise VaultConflict("A previous 2FA change needs review")
            if FIELD in protected_fields(account) and account_key(account.id) not in records:
                raise VaultError("Current protected credential is missing")
            if not remove and (not isinstance(new_password, str) or not new_password):
                raise VaultError("New password is required")
            records[key] = json.dumps({"operation": "remove" if remove else "set",
                                       "candidate": None if remove else new_password, "at": time.time()})
        self.vault.mutate(operation)
        self.accounts.update_profile_metadata(account.id, {PENDING: True})

    def finish_change(self, account):
        self.resolve_pending([account.id], "promote", confirmed=True)

    def resolve_pending(self, ids, decision, *, confirmed=False):
        if confirmed is not True or decision not in {"promote", "discard"}:
            raise VaultError("Confirm whether the pending change was verified or rejected")
        targets = self._targets(ids)
        outcomes = {}
        vault_snapshot = {}

        def operation(records):
            vault_snapshot.clear()
            outcomes.clear()
            for account in targets:
                pending_key = account_key(account.id, "2fa_change")
                cred_key = account_key(account.id)
                if pending_key in records:
                    vault_snapshot[pending_key] = records[pending_key]
                if cred_key in records:
                    vault_snapshot[cred_key] = records[cred_key]
                raw = records.get(pending_key)
                if not raw:
                    if decision == "promote":
                        raise VaultConflict("No pending candidate is stored")
                    outcomes[account.id] = "discard"
                    continue
                pending = json.loads(raw)
                op = pending.get("operation")
                if op not in {"set", "remove"}:
                    raise VaultError("Invalid pending change")
                if decision == "promote":
                    if op == "set":
                        value = pending.get("candidate")
                        if not isinstance(value, str) or not value:
                            raise VaultError("Pending password is invalid")
                        records[cred_key] = value
                    else:
                        records.pop(cred_key, None)
                records.pop(pending_key)
                outcomes[account.id] = op if decision == "promote" else "discard"

        self.vault.mutate(operation)
        target_ids = {account.id for account in targets}
        try:
            def mutate_accounts(accounts):
                found = set()
                for account in accounts:
                    if account.id not in target_ids:
                        continue
                    fields = protected_fields(account)
                    if outcomes[account.id] == "remove":
                        fields.discard(FIELD)
                    elif outcomes[account.id] == "set":
                        fields.add(FIELD)
                    account.metadata.pop(PENDING, None)
                    account.metadata[FIELDS] = sorted(fields)
                    account.metadata[MANAGED] = True
                    found.add(account.id)
                if found != target_ids:
                    raise VaultConflict("Selected accounts changed while resolving pending 2FA")
                return accounts

            self.accounts._update(mutate_accounts)
        except BaseException:
            # Roll back SecretVault state if persisting accounts.json failed so
            # vault_2fa_pending and 2fa_change never diverge across crash/disk errors.
            try:
                def rollback_vault(records):
                    for account in targets:
                        pending_key = account_key(account.id, "2fa_change")
                        cred_key = account_key(account.id)
                        if pending_key in vault_snapshot:
                            records[pending_key] = vault_snapshot[pending_key]
                        else:
                            records.pop(pending_key, None)
                        if cred_key in vault_snapshot:
                            records[cred_key] = vault_snapshot[cred_key]
                        else:
                            records.pop(cred_key, None)
                self.vault.mutate(rollback_vault)
            except Exception:
                pass
            raise
        return {"resolved": len(targets), "detail": "Local credential state updated; no Telegram request was made."}
