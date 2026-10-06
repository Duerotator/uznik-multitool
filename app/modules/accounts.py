from __future__ import annotations

import re
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from core.config import AppConfig
from core.models import AccountRecord, utc_now_iso
from core.storage import read_json, update_json, write_json_atomic


class _NoChange(Exception):
    pass


class AccountService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.config.groups_dir.mkdir(parents=True, exist_ok=True)
        self._sync_group_folders(self._load())

    def list_accounts(
        self,
        group: str | None = None,
        enabled_only: bool = False,
        *,
        snapshot: Iterable[AccountRecord] | None = None,
    ) -> list[AccountRecord]:
        accounts = self._load() if snapshot is None else list(snapshot)
        if group and group != "inbox":
            normalized = self._normalize_group(group)
            accounts = [account for account in accounts if normalized in self.account_groups(account)]
        if enabled_only:
            accounts = [account for account in accounts if account.enabled]
        return accounts

    def get_account(self, account_id: str) -> AccountRecord:
        for account in self._load():
            if account.id == account_id:
                return account
        raise KeyError(f"Account not found: {account_id}")

    def groups(self, *, snapshot: Iterable[AccountRecord] | None = None) -> list[str]:
        from_accounts: set[str] = set()
        for account in self._load() if snapshot is None else snapshot:
            from_accounts.update(self.account_groups(account))
        from_folders = {
            folder.name
            for folder in self.config.groups_dir.iterdir()
            if folder.is_dir()
        } if self.config.groups_dir.exists() else set()
        groups = from_accounts | from_folders | {"inbox"}
        return self._ordered_groups(groups)

    def create_group(self, group: str) -> str:
        group = self._normalize_group(group)
        (self.config.groups_dir / group).mkdir(parents=True, exist_ok=True)
        self._remember_group_order(group)
        self._sync_group_folders(self._load())
        return group

    def set_enabled(self, account_ids: list[str], enabled: bool) -> None:
        wanted = set(account_ids)

        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id in wanted:
                    account.enabled = enabled
                    account.updated_at = utc_now_iso()
            return accounts

        self._update(mutate)

    def set_proxy(self, account_id: str, proxy: str | None) -> None:
        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id == account_id:
                    account.proxy = proxy
                    account.updated_at = utc_now_iso()
                    return accounts
            raise _NoChange

        self._update(mutate)

    def mark_error(self, account_id: str, error: str, disable: bool = False) -> None:
        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id == account_id:
                    account.metadata["last_error"] = error
                    account.metadata["last_error_at"] = utc_now_iso()
                    if disable:
                        account.enabled = False
                        account.metadata["disabled_reason"] = error
                    account.updated_at = utc_now_iso()
                    return accounts
            raise _NoChange

        self._update(mutate)

    def clear_error(self, account_id: str) -> None:
        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id == account_id:
                    account.metadata.pop("last_error", None)
                    account.metadata.pop("last_error_at", None)
                    account.metadata.pop("disabled_reason", None)
                    account.updated_at = utc_now_iso()
                    return accounts
            raise _NoChange

        self._update(mutate)

    def clear_all_errors(self) -> int:
        changed = 0

        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            nonlocal changed
            for account in accounts:
                had_error = any(
                    key in account.metadata
                    for key in ("last_error", "last_error_at", "disabled_reason")
                )
                if had_error:
                    account.metadata.pop("last_error", None)
                    account.metadata.pop("last_error_at", None)
                    account.metadata.pop("disabled_reason", None)
                    account.updated_at = utc_now_iso()
                    changed += 1
            if not changed:
                raise _NoChange
            return accounts

        self._update(mutate)
        return changed

    def delete_accounts(self, account_ids: list[str], delete_sessions: bool = True) -> int:
        wanted = set(account_ids)
        deleted = 0

        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            nonlocal deleted
            kept: list[AccountRecord] = []
            removed: list[AccountRecord] = []
            for account in accounts:
                if account.id in wanted:
                    removed.append(account)
                else:
                    kept.append(account)

            if delete_sessions:
                for account in removed:
                    self._delete_owned_session_file(account)

            deleted = len(removed)
            removed_ids.extend(account.id for account in removed)
            return kept

        removed_ids: list[str] = []
        self._update(mutate)
        from modules.account_cleanup import cleanup_removed_accounts
        cleanup_removed_accounts(self.config, removed_ids)
        return deleted

    def deduplicate_accounts(self, delete_sessions: bool = True) -> int:
        deleted = 0

        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            nonlocal deleted
            seen: dict[str, AccountRecord] = {}
            kept: list[AccountRecord] = []
            removed: list[AccountRecord] = []
            for account in sorted(accounts, key=lambda item: item.created_at):
                key = self._duplicate_key(account)
                if not key:
                    kept.append(account)
                    continue
                if key in seen:
                    removed.append(account)
                else:
                    seen[key] = account
                    kept.append(account)

            if not removed:
                raise _NoChange

            if delete_sessions:
                for account in removed:
                    self._delete_owned_session_file(account)

            deleted = len(removed)
            removed_ids.extend(account.id for account in removed)
            return kept

        removed_ids: list[str] = []
        self._update(mutate)
        from modules.account_cleanup import cleanup_removed_accounts
        cleanup_removed_accounts(self.config, removed_ids)
        return deleted

    def find_duplicate_user_ids(self) -> dict[int, list[AccountRecord]]:
        by_user_id: dict[int, list[AccountRecord]] = {}
        for account in self._load():
            if account.user_id is None:
                continue
            by_user_id.setdefault(account.user_id, []).append(account)
        duplicates: dict[int, list[AccountRecord]] = {}
        for user_id, items in by_user_id.items():
            if len(items) > 1:
                duplicates[user_id] = sorted(items, key=lambda item: item.created_at)
        return duplicates

    def _delete_owned_session_file(self, account: AccountRecord) -> None:
        self._delete_if_inside(account.session_ref, self.config.sessions_dir)
        source_path = account.metadata.get("source_path")
        if source_path:
            self._delete_if_inside(source_path, self.config.import_dir)
        self._delete_matching_import_files(account)

    def _delete_if_inside(self, raw_path: str, root: Path) -> None:
        try:
            path = Path(raw_path).resolve()
            root = root.resolve()
        except OSError:
            return
        try:
            path.relative_to(root)
        except ValueError:
            return
        try:
            if path.exists() and path.is_file():
                path.unlink()
            elif path.exists() and path.is_dir():
                shutil.rmtree(path)
        except OSError:
            return

    def _delete_matching_import_files(self, account: AccountRecord) -> None:
        if not self.config.import_dir.exists():
            return
        names = {
            Path(account.session_ref).name,
            Path(account.session_ref).stem,
            account.id,
            account.label,
        }
        names = {name for name in names if name}
        allowed_suffixes = {".session", ".json", ".txt", ".zip"}
        for path in self.config.import_dir.rglob("*"):
            if not path.exists():
                continue
            if path.is_file():
                if path.name in names or path.stem in names:
                    if path.suffix.lower() in allowed_suffixes or path.suffix == "":
                        self._delete_if_inside(str(path), self.config.import_dir)
            elif path.is_dir() and (path.name in names or path.stem in names):
                self._delete_if_inside(str(path), self.config.import_dir)

    def _duplicate_key(self, account: AccountRecord) -> str | None:
        if account.user_id is not None:
            return f"user:{account.user_id}"
        phone = self._normalize_phone(account.phone)
        if phone:
            return f"phone:{phone}"
        fingerprint = str(account.metadata.get("source_fingerprint") or "").strip()
        if fingerprint:
            return f"fingerprint:{fingerprint}"
        return None

    def set_group(self, account_ids: list[str], group: str) -> None:
        group = self.create_group(group)
        wanted = set(account_ids)

        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id in wanted:
                    if group != "inbox":
                        groups = self.account_groups(account)
                        if group not in groups:
                            account.groups = [*groups, group]
                            account.group = account.groups[0] if account.groups else "inbox"
                            account.updated_at = utc_now_iso()
            return accounts

        self._update(mutate)

    def remove_from_group(self, account_ids: list[str], group: str) -> int:
        group = self._normalize_group(group)
        if group == "inbox":
            return 0
        wanted = set(account_ids)
        changed = 0

        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            nonlocal changed
            for account in accounts:
                if account.id not in wanted:
                    continue
                groups = [item for item in self.account_groups(account) if item != group]
                if groups != self.account_groups(account):
                    account.groups = groups
                    account.group = groups[0] if groups else "inbox"
                    account.updated_at = utc_now_iso()
                    changed += 1
            if not changed:
                raise _NoChange
            return accounts

        self._update(mutate)
        return changed

    def clear_group(self, group: str, target_group: str = "inbox") -> int:
        group = self._normalize_group(group)
        target_group = self._normalize_group(target_group)
        if group == "inbox" or group == target_group:
            return 0
        changed = 0

        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            nonlocal changed
            for account in accounts:
                groups = [item for item in self.account_groups(account) if item != group]
                if groups != self.account_groups(account):
                    if target_group != "inbox" and target_group not in groups:
                        groups.append(target_group)
                        self.create_group(target_group)
                    account.groups = groups
                    account.group = groups[0] if groups else "inbox"
                    account.updated_at = utc_now_iso()
                    changed += 1
            if not changed:
                self._sync_group_folders(accounts)
                raise _NoChange
            return accounts

        self._update(mutate)
        return changed

    def delete_group(self, group: str, target_group: str = "inbox") -> int:
        group = self._normalize_group(group)
        if group == "inbox":
            return 0
        moved = self.clear_group(group, target_group=target_group)
        folder = self.config.groups_dir / group
        groups_root = self.config.groups_dir.resolve()
        try:
            resolved = folder.resolve()
        except OSError:
            return moved
        if (
            folder.exists()
            and folder.is_dir()
            and str(resolved).startswith(str(groups_root) + "\\")
        ):
            shutil.rmtree(resolved)
        self._forget_group_order(group)
        return moved

    def set_persona(self, account_id: str, persona: str) -> None:
        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id == account_id:
                    account.persona = persona
                    account.updated_at = utc_now_iso()
                    return accounts
            raise KeyError(f"Account not found: {account_id}")

        self._update(mutate)

    def update_account_runtime_identity(
        self,
        account_id: str,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        user_id: int | None = None,
        phone: str | None = None,
    ) -> None:
        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id == account_id:
                    account.username = username if username is not None else account.username
                    account.first_name = first_name if first_name is not None else account.first_name
                    account.last_name = last_name if last_name is not None else account.last_name
                    account.user_id = user_id if user_id is not None else account.user_id
                    account.phone = phone if phone is not None else account.phone
                    account.updated_at = utc_now_iso()
                    return accounts
            raise _NoChange

        self._update(mutate)

    def set_account_username(self, account_id: str, username: str | None) -> None:
        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id == account_id:
                    account.username = username
                    account.updated_at = utc_now_iso()
                    return accounts
            raise _NoChange

        self._update(mutate)

    def update_profile_metadata(self, account_id: str, metadata: dict[str, object]) -> None:
        def mutate(accounts: list[AccountRecord]) -> list[AccountRecord]:
            for account in accounts:
                if account.id == account_id:
                    for key, value in metadata.items():
                        if value is None:
                            account.metadata.pop(key, None)
                        else:
                            account.metadata[key] = value
                    if metadata.get("profiled") is True:
                        account.profiled = True
                    elif metadata.get("profiled") is False:
                        account.profiled = False
                    account.updated_at = utc_now_iso()
                    return accounts
            raise _NoChange

        self._update(mutate)

    def _load(self) -> list[AccountRecord]:
        return self._decode(read_json(self.config.accounts_file, {"accounts": []}))

    def _decode(self, raw: dict) -> list[AccountRecord]:
        return [self._normalize_account(AccountRecord.from_dict(item)) for item in raw.get("accounts", [])]

    def _update(self, mutate: Callable[[list[AccountRecord]], list[AccountRecord]]) -> None:
        saved: list[AccountRecord] = []

        def apply(raw: dict) -> dict:
            accounts = [self._normalize_account(account) for account in mutate(self._decode(raw))]
            saved.extend(accounts)
            return {"accounts": [account.to_dict() for account in accounts]}

        try:
            update_json(self.config.accounts_file, {"accounts": []}, apply)
        except _NoChange:
            return
        self._sync_group_folders(saved)

    def _group_order_file(self) -> Path:
        return self.config.groups_dir / "order.json"

    def _load_group_order(self) -> list[str]:
        raw = read_json(self._group_order_file(), {"groups": ["inbox"]})
        groups = raw.get("groups", [])
        return [self._normalize_group(str(group)) for group in groups if str(group).strip()]

    def _normalized_group_order(self, groups: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for group in ["inbox", *groups]:
            normalized = self._normalize_group(group)
            if normalized not in seen:
                ordered.append(normalized)
                seen.add(normalized)
        return ordered

    def _save_group_order(self, groups: list[str]) -> None:
        write_json_atomic(
            self._group_order_file(),
            {"groups": self._normalized_group_order(groups)},
        )

    def _ordered_groups(self, groups: set[str]) -> list[str]:
        normalized = {self._normalize_group(group) for group in groups if group}
        order = self._load_group_order()
        ordered = [group for group in order if group in normalized]
        for group in sorted(normalized - set(ordered)):
            ordered.append(group)
        if "inbox" in ordered:
            ordered = ["inbox", *[group for group in ordered if group != "inbox"]]
        else:
            ordered.insert(0, "inbox")
        if self._normalized_group_order(ordered) != order:
            self._save_group_order(ordered)
        return ordered

    def _remember_group_order(self, group: str) -> None:
        order = self._load_group_order()
        if group not in order:
            order.append(group)
            self._save_group_order(order)

    def _forget_group_order(self, group: str) -> None:
        order = [item for item in self._load_group_order() if item != group]
        self._save_group_order(order)

    def _save(self, accounts: list[AccountRecord]) -> None:
        accounts = [self._normalize_account(account) for account in accounts]
        write_json_atomic(
            self.config.accounts_file,
            {"accounts": [account.to_dict() for account in accounts]},
        )
        self._sync_group_folders(accounts)

    def _sync_group_folders(self, accounts: list[AccountRecord]) -> None:
        self.config.groups_dir.mkdir(parents=True, exist_ok=True)
        by_group: dict[str, list[AccountRecord]] = {}
        for account in accounts:
            for group in ["inbox", *self.account_groups(account)]:
                by_group.setdefault(group, []).append(account)
        for folder in self.config.groups_dir.iterdir():
            if folder.is_dir():
                by_group.setdefault(self._normalize_group(folder.name), [])

        for group, items in by_group.items():
            folder = self.config.groups_dir / self._normalize_group(group)
            folder.mkdir(parents=True, exist_ok=True)
            write_json_atomic(
                folder / "accounts.json",
                {
                    "group": group,
                    "accounts": [
                        {
                            "id": account.id,
                            "label": account.label,
                            "backend": account.backend,
                            "enabled": account.enabled,
                        }
                        for account in items
                    ],
                },
            )

    def _normalize_group(self, group: str) -> str:
        group = group.strip() or "inbox"
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", group)

    def _normalize_phone(self, phone: str | None) -> str:
        if not phone:
            return ""
        return re.sub(r"\D+", "", str(phone))

    def account_groups(self, account: AccountRecord) -> list[str]:
        groups: list[str] = []
        raw_groups = list(getattr(account, "groups", []) or [])
        legacy = str(getattr(account, "group", "") or "")
        if legacy not in {"", "inbox", "default"}:
            raw_groups.insert(0, legacy)
        seen: set[str] = set()
        for group in raw_groups:
            normalized = self._normalize_group(str(group))
            if normalized in {"", "inbox", "default"} or normalized in seen:
                continue
            groups.append(normalized)
            seen.add(normalized)
        return groups

    def _normalize_account(self, account: AccountRecord) -> AccountRecord:
        account.groups = self.account_groups(account)
        account.group = account.groups[0] if account.groups else "inbox"
        return account
