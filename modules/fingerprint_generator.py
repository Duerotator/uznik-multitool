from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.storage import read_json, write_json_atomic

DEVICES = [
    ("Samsung Galaxy S24 Ultra", "Android 14.0", "14.0", "SM-S928B"),
    ("Samsung Galaxy S24", "Android 14.0", "14.0", "SM-S921B"),
    ("Samsung Galaxy S23 Ultra", "Android 14.0", "14.0", "SM-S918B"),
    ("Samsung Galaxy S23", "Android 13.0", "13.0", "SM-S911B"),
    ("Samsung Galaxy S22", "Android 13.0", "13.0", "SM-S901B"),
    ("Samsung Galaxy A55", "Android 14.0", "14.0", "SM-A556B"),
    ("Samsung Galaxy A54", "Android 13.0", "13.0", "SM-A546B"),
    ("Samsung Galaxy A35", "Android 14.0", "14.0", "SM-A356B"),
    ("Samsung Galaxy A25", "Android 14.0", "14.0", "SM-A256B"),
    ("Samsung Galaxy A15", "Android 14.0", "14.0", "SM-A155F"),
    ("iPhone 15 Pro Max", "iOS 17.4", "17.4", "iPhone15,3"),
    ("iPhone 15 Pro", "iOS 17.4", "17.4", "iPhone15,2"),
    ("iPhone 15", "iOS 17.3", "17.3", "iPhone15,4"),
    ("iPhone 14 Pro Max", "iOS 16.5", "16.5", "iPhone15,3"),
    ("iPhone 14", "iOS 16.5", "16.5", "iPhone14,7"),
    ("iPhone 13", "iOS 16.5", "16.5", "iPhone14,5"),
    ("Xiaomi 14 Ultra", "Android 14.0", "14.0", "24030PN60G"),
    ("Xiaomi 14", "Android 14.0", "14.0", "23127PN0CG"),
    ("Xiaomi 13 Pro", "Android 13.0", "13.0", "2210132G"),
    ("Xiaomi Redmi Note 13 Pro", "Android 13.0", "13.0", "2312DRA50G"),
    ("Xiaomi Redmi Note 12", "Android 13.0", "13.0", "23021RAA2Y"),
    ("Xiaomi Poco X6 Pro", "Android 14.0", "14.0", "2311DRK48G"),
    ("OnePlus 12", "Android 14.0", "14.0", "CPH2573"),
    ("OnePlus 11", "Android 13.0", "13.0", "CPH2449"),
    ("Google Pixel 8 Pro", "Android 14.0", "14.0", "GC3VE"),
    ("Google Pixel 8", "Android 14.0", "14.0", "GKWS6"),
    ("Google Pixel 7", "Android 13.0", "13.0", "GVU6C"),
    ("Huawei P60 Pro", "Android 13.0", "13.0", "MNA-LX9"),
    ("Huawei Mate 60 Pro", "Android 13.0", "13.0", "ALN-AL00"),
    ("Oppo Find X7 Ultra", "Android 14.0", "14.0", "PHY120"),
    ("Vivo X100 Pro", "Android 14.0", "14.0", "V2309"),
    ("Realme GT 5", "Android 13.0", "13.0", "RMX3823"),
]

TELEGRAM_APP_VERSIONS = [
    "11.1.1 (4928)",
    "11.1.0 (4927)",
    "11.0.1 (4890)",
    "11.0.0 (4880)",
    "10.15.0 (4850)",
    "10.14.0 (4800)",
    "10.13.0 (4750)",
    "10.12.0 (4700)",
    "10.11.0 (4650)",
    "10.10.1 (4601)",
    "10.10.0 (4600)",
    "10.9.0 (4550)",
]

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
            key: SessionFingerprint.from_dict(val) for key, val in data.items()
        }

    def _save(self) -> None:
        write_json_atomic(
            Path(self.storage_path),
            {key: fp.to_dict() for key, fp in self._cache.items()},
        )

    @staticmethod
    def generate_random() -> SessionFingerprint:
        model, android_ver, raw_ver, _device_code = random.choice(DEVICES)
        is_iphone = "iPhone" in model or "iOS" in android_ver
        system_version = android_ver
        platform = "ios" if is_iphone else "android"
        lang_pack = "ios" if is_iphone else "android"
        lang_code, system_lang_code = random.choice(LANGUAGES)
        return SessionFingerprint(
            device_model=model,
            system_version=system_version,
            app_version=random.choice(TELEGRAM_APP_VERSIONS),
            lang_code=lang_code,
            system_lang_code=system_lang_code,
            lang_pack=lang_pack,
            platform=platform,
        )

    def get_params(self, account_id: str) -> dict[str, Any]:
        if account_id not in self._cache:
            self._cache[account_id] = self.generate_random()
            self._save()
        return self._cache[account_id].to_dict()

    def regenerate(self, account_id: str) -> dict[str, Any]:
        self._cache[account_id] = self.generate_random()
        self._save()
        return self._cache[account_id].to_dict()

    def params_for_account(self, account: Any) -> dict[str, Any]:
        return self.get_params(str(account.id))
