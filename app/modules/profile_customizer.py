from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.models import AccountRecord
from core.results import ActionResult
from core.storage import read_json, write_json_atomic
from core.telegram_client import create_client
from core.ui_progress import OperationProgress
from modules.accounts import AccountService
from utils.profile_generator import generate_username_candidates
from utils.rate_limit import human_delay
from utils.telegram_errors import is_invalid_auth_error, short_error


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass
class ProfileUpdatePlan:
    first_names: list[str] | None = None
    last_names: list[str] | None = None
    bios: list[str] | None = None
    avatars_dir: Path | None = None
    avatar_mode: str = "random"
    usernames: list[str | None] | None = None
    clear_avatars: bool = False
    profile_plan: list[dict[str, Any]] | None = None


class ProfileCustomizer:
    def __init__(self, config: AppConfig):
        self.config = config
        self.accounts = AccountService(config)
        self.log = logging.getLogger("profile")

    async def clear_usernames(self, accounts: list[AccountRecord]) -> ActionResult:
        return await self.apply(accounts, ProfileUpdatePlan(usernames=[None] * len(accounts)))

    async def clear_bios(self, accounts: list[AccountRecord]) -> ActionResult:
        return await self.apply(accounts, ProfileUpdatePlan(bios=[""]))

    async def clear_avatars(self, accounts: list[AccountRecord]) -> ActionResult:
        return await self.apply(accounts, ProfileUpdatePlan(clear_avatars=True))

    async def clear_full_profile(self, accounts: list[AccountRecord]) -> ActionResult:
        return await self.apply(
            accounts,
            ProfileUpdatePlan(
                usernames=[None] * len(accounts),
                last_names=[""],
                bios=[""],
                clear_avatars=True,
            ),
        )

    async def apply(
        self,
        accounts: list[AccountRecord],
        plan: ProfileUpdatePlan,
        *,
        progress: OperationProgress | None = None,
    ) -> ActionResult:
        semaphore = asyncio.Semaphore(max(1, self.config.max_concurrency))
        avatars = self._load_avatars(plan.avatars_dir)
        if plan.profile_plan and not avatars:
            avatars = self._load_avatars(Path("assets/avatar_packs"))
        profiles = self._profiles_by_account(plan.profile_plan)
        used_avatars = self._load_used_avatars()
        reserved_avatars: set[str] = set()
        avatar_lock = asyncio.Lock()
        result = ActionResult()

        async def reserve_avatar(index: int, profile: dict[str, Any]) -> Path | None:
            avatar = self._profile_avatar(profile)
            if avatar is None:
                async with avatar_lock:
                    avatar = self._pick_avatar(avatars, index, plan.avatar_mode, used_avatars)
                    if avatar is None:
                        return None
                    key = self._avatar_key(avatar)
                    used_avatars.add(key)
                    reserved_avatars.add(key)
                    return avatar
            async with avatar_lock:
                key = self._avatar_key(avatar)
                used_avatars.add(key)
                reserved_avatars.add(key)
            return avatar

        async def reserve_fallback_avatar(index: int) -> tuple[Path | None, str | None]:
            async with avatar_lock:
                avatar = self._pick_avatar(avatars, index, plan.avatar_mode, used_avatars)
                if avatar is None:
                    return None, None
                key = self._avatar_key(avatar)
                used_avatars.add(key)
                reserved_avatars.add(key)
                return avatar, key

        async def release_reserved_avatar(key: str | None) -> None:
            if key and key in reserved_avatars:
                async with avatar_lock:
                    used_avatars.discard(key)
                    reserved_avatars.discard(key)

        async def worker(index: int, account: AccountRecord) -> None:
            async with semaphore:
                await human_delay(self.config.min_action_delay, self.config.max_action_delay)
                profile = profiles.get(account.id, {})
                first_name = profile.get("first_name") or self._pick(plan.first_names, index)
                last_name = profile.get("last_name") or self._pick(plan.last_names, index)
                bio = profile.get("bio") or self._pick(plan.bios, index)
                if profile:
                    username = (profile.get("username") or "").lstrip("@")
                else:
                    username = self._pick(plan.usernames, index) if plan.usernames is not None else "__skip__"
                avatar = await reserve_avatar(index, profile)
                avatar_key = self._avatar_key(avatar) if avatar is not None else None
                applied_avatar: Path | None = None
                applied_avatar_key: str | None = None
                has_changes = (
                    first_name is not None
                    or last_name is not None
                    or bio is not None
                    or username != "__skip__"
                    or plan.clear_avatars
                    or avatar is not None
                )
                if not has_changes:
                    error = "No profile changes. Check avatar folder/profile plan paths."
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    self.accounts.mark_error(account.id, error, disable=False)
                    self.log.error("Profile update skipped for %s: %s", account.id, error)
                    return

                try:
                    async with create_client(self.config, account) as client:
                        if first_name is not None or last_name is not None or bio is not None:
                            await client.update_profile(first_name, last_name, bio)
                        if plan.clear_avatars:
                            await client.clear_profile_photos()
                        if avatar is not None:
                            current_avatar = avatar
                            current_key = avatar_key
                            last_avatar_error: str | None = None
                            for attempt in range(8):
                                try:
                                    await client.set_profile_photo(current_avatar)
                                    applied_avatar = current_avatar
                                    applied_avatar_key = current_key
                                    break
                                except Exception as exc:
                                    last_avatar_error = short_error(exc)
                                    await release_reserved_avatar(current_key)
                                    if not self._is_retryable_avatar_error(exc):
                                        raise
                                    self.log.warning(
                                        "Avatar candidate failed for %s: %s (%s)",
                                        account.id,
                                        current_avatar,
                                        last_avatar_error,
                                    )
                                    self._delete_bad_avatar_file(current_avatar)
                                    current_avatar, current_key = await reserve_fallback_avatar(index + attempt + 1)
                                    if current_avatar is None:
                                        break
                            if applied_avatar is None and last_avatar_error:
                                self.accounts.update_profile_metadata(
                                    account.id,
                                    {"last_avatar_error": last_avatar_error},
                                )
                                self.log.warning(
                                    "Avatar skipped for %s after fallback attempts: %s",
                                    account.id,
                                    last_avatar_error,
                                )
                        if username != "__skip__":
                            applied_username = await self._set_username_with_retries(
                                client,
                                account,
                                username,
                                first_name,
                                last_name,
                            )
                            self.accounts.set_account_username(account.id, applied_username)
                            self.accounts.update_profile_metadata(
                                account.id,
                                {"applied_username": applied_username},
                            )
                        identity_kwargs: dict[str, str] = {}
                        if first_name is not None:
                            identity_kwargs["first_name"] = first_name
                        if last_name is not None:
                            identity_kwargs["last_name"] = last_name
                        if identity_kwargs:
                            self.accounts.update_account_runtime_identity(account.id, **identity_kwargs)
                    if profile:
                        self.accounts.update_profile_metadata(
                            account.id,
                            {
                                "profile_gender": profile.get("gender"),
                                "planned_username": profile.get("username"),
                                "planned_avatar": profile.get("avatar"),
                            },
                        )
                    metadata: dict[str, object] = {}
                    if bio is not None:
                        metadata["last_known_bio"] = bio
                    if applied_avatar is not None:
                        metadata["last_applied_avatar"] = str(applied_avatar)
                    if metadata:
                        self.accounts.update_profile_metadata(account.id, metadata)
                    if applied_avatar_key:
                        self._save_used_avatars(used_avatars)
                    self.log.info("Profile updated for %s", account.id)
                    result.add_ok(account.id)
                    if progress:
                        progress.mark_ok(account.id)
                except Exception as exc:
                    if applied_avatar_key is None:
                        await release_reserved_avatar(avatar_key)
                    error = short_error(exc)
                    result.add_error(account.id, error)
                    if progress:
                        progress.mark_error(account.id)
                    disable = is_invalid_auth_error(exc)
                    self.accounts.mark_error(account.id, error, disable=disable)
                    if disable:
                        self.log.error("Disabled invalid session %s: %s", account.id, error)
                    else:
                        self.log.error("Profile update failed for %s: %s", account.id, error)

        await asyncio.gather(
            *(worker(index, account) for index, account in enumerate(accounts) if account.enabled)
        )
        return result

    def load_lines(self, path: Path) -> list[str]:
        return [
            cleaned
            for line in path.read_text(encoding="utf-8").splitlines()
            if (cleaned := line.strip()) and not cleaned.startswith("#")
        ]

    def load_optional_lines(self, path: Path) -> list[str] | None:
        return self.load_lines(path) if path.exists() else None

    def load_profile_plan(self, path: Path) -> list[dict[str, Any]]:
        from core.private_storage import read_private_json, write_private_json

        raw, encrypted = read_private_json(path)
        if isinstance(raw, dict):
            profiles = raw.get("profiles", [])
        else:
            profiles = raw
        if not isinstance(profiles, list):
            raise ValueError("Profile plan must contain a list named 'profiles'.")
        if not encrypted:
            write_private_json(path, raw)
        return [item for item in profiles if isinstance(item, dict)]

    def _pick(self, values: list[str | None] | None, index: int) -> str | None:
        if not values:
            return None
        if len(values) == 1:
            return values[0]
        return values[index % len(values)]

    def _load_avatars(self, avatars_dir: Path | None) -> list[Path]:
        if not avatars_dir:
            return []
        return [
            path
            for path in sorted(avatars_dir.rglob("*"))
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        ]

    def _pick_avatar(
        self,
        avatars: list[Path],
        index: int,
        mode: str,
        used_avatars: set[str] | None = None,
    ) -> Path | None:
        avatars = [avatar for avatar in avatars if avatar.exists()]
        if not avatars:
            return None
        if used_avatars is not None:
            available = [avatar for avatar in avatars if self._avatar_key(avatar) not in used_avatars]
            if available:
                avatars = available
        if mode == "ordered":
            return avatars[index % len(avatars)]
        return random.choice(avatars)

    def _profiles_by_account(
        self,
        profiles: list[dict[str, Any]] | None,
    ) -> dict[str, dict[str, Any]]:
        if not profiles:
            return {}
        return {
            str(profile["account_id"]): profile
            for profile in profiles
            if profile.get("account_id")
        }

    def _profile_avatar(self, profile: dict[str, Any]) -> Path | None:
        avatar = profile.get("avatar")
        if not avatar:
            return None
        path = Path(str(avatar))
        if path.exists() and path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
            return path
        return None

    def _avatar_usage_file(self) -> Path:
        return self.config.data_dir / "avatar_usage.json"

    def _load_used_avatars(self) -> set[str]:
        raw = read_json(self._avatar_usage_file(), {"used": []})
        return {str(item) for item in raw.get("used", []) if str(item).strip()}

    def _save_used_avatars(self, used: set[str]) -> None:
        write_json_atomic(self._avatar_usage_file(), {"used": sorted(used)})

    def _avatar_key(self, avatar: Path) -> str:
        try:
            return str(avatar.resolve())
        except OSError:
            return str(avatar)

    def _delete_bad_avatar_file(self, avatar: Path) -> bool:
        if not avatar.exists() or not avatar.is_file() or avatar.suffix.lower() not in IMAGE_EXTENSIONS:
            return False
        try:
            resolved = avatar.resolve()
            roots = [Path("assets/avatar_packs"), Path("assets/avatar_packs_raw")]
            if not any(resolved.is_relative_to(root.resolve()) for root in roots):
                return False
            avatar.unlink()
            self.log.warning("Deleted bad avatar file: %s", avatar)
            return True
        except Exception as exc:
            self.log.warning("Could not delete bad avatar file %s: %s", avatar, exc)
            return False

    async def _set_username_with_retries(
        self,
        client,
        account: AccountRecord,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> str | None:
        if username is None:
            try:
                await client.set_username(None)
            except Exception as exc:
                if "USERNAME_NOT_MODIFIED" in str(exc).upper():
                    return None
                raise
            return None

        candidates = self._username_candidates(account, username, first_name, last_name)
        seen: set[str] = set()
        last_error: Exception | None = None
        for candidate in candidates:
            candidate = candidate.lstrip("@")
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                await client.set_username(candidate)
                if candidate != username:
                    self.log.info("Username fallback for %s: %s", account.id, candidate)
                return candidate
            except Exception as exc:
                if "USERNAME_NOT_MODIFIED" in str(exc).upper():
                    return candidate
                if not self._is_retryable_username_error(exc):
                    raise
                last_error = exc
                self.log.warning(
                    "Username candidate failed for %s: %s (%s)",
                    account.id,
                    candidate,
                    short_error(exc),
                )
        if last_error:
            raise last_error
        raise RuntimeError("No username candidates generated.")

    def _username_candidates(
        self,
        account: AccountRecord,
        username: str,
        first_name: str | None,
        last_name: str | None,
    ) -> list[str]:
        first = first_name or account.first_name
        last = last_name or account.last_name
        base = username.lstrip("@")
        candidates = [base]
        candidates.extend(generate_username_candidates(first, last, count=44))

        digits = "".join(ch for ch in f"{account.phone or account.label or account.id}" if ch.isdigit())
        suffixes = [
            digits[-4:],
            digits[-5:],
            digits[-6:],
            str(random.randrange(1000, 9999)),
            str(random.randrange(10000, 99999)),
        ]
        stems = [
            self._fit_username_part(first or "user"),
            self._fit_username_part(f"{first or 'user'}{last or ''}"),
            self._fit_username_part(f"{first or 'user'}_{last or 'id'}"),
        ]
        for stem in stems:
            for suffix in suffixes:
                if suffix:
                    candidates.append(self._fit_username(f"{stem}{suffix}"))
                    candidates.append(self._fit_username(f"{stem}_{suffix}"))
        candidates.extend(generate_username_candidates(first, last, count=24))
        return candidates

    def _fit_username_part(self, value: str) -> str:
        cleaned = "".join(ch.lower() if ch.isalnum() or ch == "_" else "_" for ch in value)
        cleaned = "_".join(part for part in cleaned.split("_") if part)
        if not cleaned:
            cleaned = "user"
        if not cleaned[0].isalpha():
            cleaned = f"u_{cleaned}"
        return cleaned[:24].strip("_") or "user"

    def _fit_username(self, value: str) -> str:
        value = self._fit_username_part(value)
        if len(value) < 5:
            value = f"{value}_id"
        return value[:32].strip("_")

    def _is_retryable_username_error(self, exc: Exception) -> bool:
        text = str(exc).upper()
        return any(
            marker in text
            for marker in (
                "USERNAME_OCCUPIED",
                "USERNAME_INVALID",
                "USERNAME_PURCHASE_AVAILABLE",
                "USERNAME_NOT_MODIFIED",
                "USERNAME_TOO_LONG",
                "USERNAMES_UNAVAILABLE",
            )
        )

    def _is_retryable_avatar_error(self, exc: Exception) -> bool:
        text = str(exc).upper()
        return any(
            marker in text
            for marker in (
                "PHOTO_FILE_MISSING",
                "PHOTO_EXT_INVALID",
                "PHOTO_INVALID_DIMENSIONS",
                "PHOTO_SAVE_FILE_INVALID",
                "WEBPAGE_CURL_FAILED",
                "FILE_PART",
            )
        )
