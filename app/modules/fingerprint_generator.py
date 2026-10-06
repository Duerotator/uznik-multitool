from __future__ import annotations

import random
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from core.storage import read_json, update_json
from modules.device_catalog import DEVICES, DEVICE_TYPES, PLATFORM_LANG_PACKS


def _app_version(backend: str) -> str:
    # A device label does not turn the library into an official mobile client.
    # Report the actual implementation, rather than inventing Android builds
    # for iOS/macOS/desktop connections. Existing stored values stay untouched.
    package = "Telethon" if backend == "telethon" else "Kurigram"
    try:
        installed = version(package)
    except PackageNotFoundError:
        installed = "unknown"
    return f"Uznik MultiTool ({package} {installed})"


LANGUAGES = [
    ("en", "en"),
    ("ru", "ru"),
    ("en", "ru"),
    ("en", "en"),
    ("de", "de"),
    ("fr", "fr"),
    ("es", "es"),
    ("en", "en"),
    ("ar", "ar"),
    ("pt", "pt"),
    ("tr", "tr"),
    ("en", "en"),
    ("ru", "en"),
    ("en", "ru"),
    ("en", "en"),
]

DEFAULT_FINGERPRINT_FILE = "data/fingerprints.json"


@dataclass
class SessionFingerprint:
    device_model: str
    system_version: str
    app_version: str
    lang_code: str
    system_lang_code: str
    lang_pack: str = "android"
    platform: str = "android"

    def to_dict(self) -> dict[str, str]:
        return {
            "device_model": self.device_model,
            "system_version": self.system_version,
            "app_version": self.app_version,
            "lang_code": self.lang_code,
            "system_lang_code": self.system_lang_code,
            "lang_pack": self.lang_pack,
            "platform": self.platform,
        }

    @classmethod
    def from_dict(cls, data: dict[str, str]) -> "SessionFingerprint":
        return cls(
            device_model=data.get("device_model", ""),
            system_version=data.get("system_version", ""),
            app_version=data.get("app_version", ""),
            lang_code=data.get("lang_code", "en"),
            system_lang_code=data.get("system_lang_code", "en"),
            lang_pack=data.get("lang_pack", "android"),
            platform=data.get("platform", "android"),
        )


class FingerprintGenerator:
    def __init__(self, storage_path: str = DEFAULT_FINGERPRINT_FILE):
        self.storage_path = storage_path
        self._cache: dict[str, SessionFingerprint] = {}
        self._load()

    def _load(self) -> None:
        data = read_json(Path(self.storage_path), {})
        self._cache = {
            str(key): SessionFingerprint.from_dict(val)
            for key, val in (data.items() if isinstance(data, dict) else ())
            if self._valid(val)
        }

    @staticmethod
    def _valid(data: Any) -> bool:
        return isinstance(data, dict) and all(
            isinstance(data.get(key), str) and bool(data[key].strip())
            for key in ("device_model", "system_version", "app_version")
        )

    def _store(
        self, key: str, fingerprint: SessionFingerprint, *, replace: bool = False,
    ) -> dict[str, str]:
        def merge(data: Any) -> dict[str, Any]:
            if not isinstance(data, dict):
                data = {}
            if replace or not self._valid(data.get(key)):
                data[key] = fingerprint.to_dict()
            return data

        # Merge one record under the storage lock: two generator instances must
        # not erase each other's newly created sessions with stale caches.
        data = update_json(Path(self.storage_path), {}, merge, skip_unchanged=True)
        self._cache[key] = SessionFingerprint.from_dict(data[key])
        return self._cache[key].to_dict()

    @staticmethod
    def generate_random(
        *, device_type: str | None = None, platform: str | None = None,
        backend: str = "pyrogram",
    ) -> SessionFingerprint:
        if device_type is not None and device_type not in DEVICE_TYPES:
            raise ValueError(f"Unknown device type: {device_type}")
        if platform is not None and platform not in PLATFORM_LANG_PACKS:
            raise ValueError(f"Unknown device platform: {platform}")
        candidates = [
            device for device in DEVICES
            if (device_type is None or device.device_type == device_type)
            and (platform is None or device.platform == platform)
        ]
        if not candidates:
            raise ValueError(f"No devices for {device_type}/{platform}")
        device = random.choice(candidates)
        lang_code, system_lang_code = random.choice(LANGUAGES)
        return SessionFingerprint(
            device_model=device.model,
            system_version=random.choice(device.systems),
            app_version=_app_version(backend),
            lang_code=lang_code,
            system_lang_code=system_lang_code,
            lang_pack=PLATFORM_LANG_PACKS[device.platform],
            platform=device.platform,
        )

    def get_params(self, account_id: str, *, backend: str = "pyrogram") -> dict[str, Any]:
        account_id = str(account_id)
        if account_id not in self._cache:
            self._load()
        if account_id not in self._cache:
            return self._store(account_id, self.generate_random(backend=backend))
        return self._cache[account_id].to_dict()

    def regenerate(self, account_id: str) -> dict[str, Any]:
        return self._store(str(account_id), self.generate_random(), replace=True)

    def params_for_account(self, account: Any) -> dict[str, Any]:
        key = str(account.id)
        if key not in self._cache:
            self._load()
        if key in self._cache:
            return self._cache[key].to_dict()
        # Authorization is keyed by phone; the imported account gets a new ID.
        # Keep the exact descriptor saved by AuthManager instead of generating
        # a second, unrelated device at its first desktop action.
        metadata = getattr(account, "metadata", None) or {}
        saved = metadata.get("fingerprint")
        if self._valid(saved):
            return self._store(key, SessionFingerprint.from_dict(saved))
        return self.get_params(key, backend=getattr(account, "backend", "pyrogram"))
