from __future__ import annotations

import functools
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.storage import read_json, update_json

logger = logging.getLogger("sleep-scheduler")

DEFAULT_SLEEP_FILE = "data/sleep_zones.json"

COUNTRY_TIMEZONES: dict[str, list[str]] = {
    "1": [
        "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
        "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "America/Vancouver",
    ],
    "7": [
        "Europe/Moscow", "Europe/Samara", "Asia/Yekaterinburg",
        "Asia/Omsk", "Asia/Krasnoyarsk", "Asia/Irkutsk",
        "Asia/Yakutsk", "Asia/Vladivostok", "Asia/Magadan",
        "Asia/Kamchatka", "Asia/Anadyr",
    ],
    "20": ["Africa/Cairo"],
    "27": ["Africa/Johannesburg"],
    "30": ["Europe/Athens"],
    "31": ["Europe/Amsterdam"],
    "32": ["Europe/Brussels"],
    "33": ["Europe/Paris"],
    "34": ["Europe/Madrid", "Atlantic/Canary"],
    "36": ["Europe/Budapest"],
    "39": ["Europe/Rome"],
    "40": ["Europe/Bucharest"],
    "41": ["Europe/Zurich"],
    "44": ["Europe/London"],
    "45": ["Europe/Copenhagen"],
    "46": ["Europe/Stockholm"],
    "47": ["Europe/Oslo"],
    "48": ["Europe/Warsaw"],
    "49": ["Europe/Berlin"],
    "52": ["America/Mexico_City", "America/Tijuana", "America/Cancun"],
    "55": ["America/Sao_Paulo", "America/Manaus", "America/Recife", "America/Fortaleza"],
    "60": ["Asia/Kuala_Lumpur", "Asia/Kuching"],
    "61": ["Australia/Sydney", "Australia/Melbourne", "Australia/Brisbane", "Australia/Perth", "Australia/Adelaide"],
    "62": ["Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura"],
    "63": ["Asia/Manila"],
    "65": ["Asia/Singapore"],
    "66": ["Asia/Bangkok"],
    "81": ["Asia/Tokyo"],
    "82": ["Asia/Seoul"],
    "84": ["Asia/Ho_Chi_Minh"],
    "86": ["Asia/Shanghai", "Asia/Urumqi"],
    "90": ["Europe/Istanbul"],
    "91": ["Asia/Kolkata"],
    "92": ["Asia/Karachi"],
    "93": ["Asia/Kabul"],
    "94": ["Asia/Colombo"],
    "95": ["Asia/Yangon"],
    "98": ["Asia/Tehran"],
    "212": ["Africa/Casablanca"],
    "213": ["Africa/Algiers"],
    "216": ["Africa/Tunis"],
    "234": ["Africa/Lagos"],
    "351": ["Europe/Lisbon", "Atlantic/Azores"],
    "353": ["Europe/Dublin"],
    "354": ["Atlantic/Reykjavik"],
    "358": ["Europe/Helsinki"],
    "359": ["Europe/Sofia"],
    "370": ["Europe/Vilnius"],
    "371": ["Europe/Riga"],
    "372": ["Europe/Tallinn"],
    "373": ["Europe/Chisinau"],
    "374": ["Asia/Yerevan"],
    "375": ["Europe/Minsk"],
    "380": ["Europe/Kyiv"],
    "381": ["Europe/Belgrade"],
    "420": ["Europe/Prague"],
    "421": ["Europe/Bratislava"],
    "852": ["Asia/Hong_Kong"],
    "853": ["Asia/Macau"],
    "855": ["Asia/Phnom_Penh"],
    "856": ["Asia/Vientiane"],
    "880": ["Asia/Dhaka"],
    "971": ["Asia/Dubai"],
    "972": ["Asia/Jerusalem"],
    "998": ["Asia/Tashkent", "Asia/Samarkand"],
}

SLEEP_START_HOUR = 1
SLEEP_END_HOUR = 7


class SleepScheduler:
    def __init__(self, storage_path: str = DEFAULT_SLEEP_FILE):
        self.storage_path = storage_path
        self._zones: dict[str, str] = {}
        self.settings = {"timezone": "UTC", "start": 1, "end": 7, "enabled": True}
        self._version = None
        self._warned: set[str] = set()
        self._load()

    def _load(self) -> None:
        path = Path(self.storage_path)
        # Capture the version before reading: a replacement during the read must
        # trigger another refresh, not mark older data as the newest version.
        version = self._file_version(path)
        data = read_json(path, {})
        zones = data.get("zones", data)
        self._zones = {str(k): str(v) for k, v in zones.items()}
        self.settings = {"timezone": "UTC", "start": 1, "end": 7, "enabled": True}
        if "zones" in data:
            self.settings.update(data.get("settings", {}))
        self._version = version

    @staticmethod
    def _file_version(path: Path) -> tuple[int, int, int, int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        # JSON writes use atomic replacement. File identity detects same-size
        # replacements even when the filesystem preserves/coarsens mtime.
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _save(self) -> None:
        def merge(data):
            zones = dict(data.get("zones", data))
            zones.update(self._zones)
            return {"zones": zones, "settings": dict(self.settings)}
        update_json(Path(self.storage_path), {}, merge)
        self._load()

    def _refresh(self) -> None:
        path = Path(self.storage_path)
        version = self._file_version(path)
        if version != self._version:
            self._load()

    def configure(self, accounts: list[Any], *, timezone: str, start: int, end: int, enabled: bool) -> None:
        ZoneInfo(timezone)  # Reject missing tzdata / invalid names before writing.
        if not (0 <= start <= 23 and 0 <= end <= 23) or (enabled and start == end):
            raise ValueError("Sleep start/end must be different hours between 0 and 23.")
        self._refresh()
        self.settings = {"timezone": timezone, "start": start, "end": end, "enabled": enabled}
        for account in accounts:
            self._zones[str(getattr(account, "id", account))] = timezone
        self._save()

    @staticmethod
    def _country_code(phone: str | None) -> str:
        if not phone:
            return "7"
        digits = "".join(ch for ch in str(phone) if ch.isdigit())
        for length in range(4, 0, -1):
            code = digits[:length]
            if code in COUNTRY_TIMEZONES:
                return code
        return "7"

    def assign_timezone(self, account_id: str, phone: str | None = None) -> str:
        self._refresh()
        if account_id in self._zones:
            return self._zones[account_id]
        tz = self.settings["timezone"]
        self._zones[account_id] = tz
        self._save()
        logger.debug("Assigned %s timezone %s", account_id, tz)
        return tz

    def get_timezone(self, account_id: str) -> str | None:
        self._refresh()
        return self._zones.get(account_id)

    def assign_all(self, accounts: list[Any]) -> int:
        self._refresh()
        count = 0
        for acc in accounts:
            aid = str(getattr(acc, "id", acc))
            phone = getattr(acc, "phone", None)
            if aid not in self._zones:
                self._zones[aid] = self.settings["timezone"]
                count += 1
        if count:
            self._save()
        return count

    def state(self, account_id: str, *, now: datetime | None = None) -> str:
        self._refresh()
        if not self.settings["enabled"]:
            return "awake"
        tz_name = self._zones.get(account_id)
        if not tz_name:
            return "unknown"
        try:
            tz = ZoneInfo(tz_name)
            current = now.astimezone(tz) if now else datetime.now(tz)
            start, end = int(self.settings["start"]), int(self.settings["end"])
            if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
                raise ValueError("Invalid sleep hours")
            sleeping = start <= current.hour < end if start < end else current.hour >= start or current.hour < end
            return "sleeping" if sleeping else "awake"
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            if tz_name not in self._warned:
                logger.error("Cannot determine sleep state for timezone %s; check timezone settings and tzdata.", tz_name)
                self._warned.add(tz_name)
            return "unknown"

    def is_sleeping(self, account_id: str) -> bool:
        # Background automation must not treat an invalid schedule as Awake.
        return self.state(account_id) != "awake"

    def wake_at(self, account_id: str) -> float | None:
        if self.state(account_id) != "sleeping":
            return None
        tz_name = self._zones.get(account_id)
        try:
            tz = ZoneInfo(tz_name)
            now = datetime.now(tz)
            wake_hour = int(self.settings["end"])
            wake = now.replace(hour=wake_hour, minute=0, second=0, microsecond=0)
            if wake <= now:
                wake += timedelta(days=1)
            return wake.timestamp() - now.timestamp()
        except Exception:
            return None

    def sleep_status(self, account_id: str) -> str:
        state = self.state(account_id)
        if state == "sleeping":
            tz = self._zones.get(account_id, "?")
            return f"sleeping ({tz})"
        return state


def is_account_sleeping(scheduler: SleepScheduler, account: Any) -> bool:
    return scheduler.is_sleeping(str(getattr(account, "id", account)))


def check_awake(scheduler: SleepScheduler):
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(account: Any, *args: Any, **kwargs: Any) -> Any:
            if is_account_sleeping(scheduler, account):
                logger.debug("Skipping %s: account %s is sleeping", func.__name__, getattr(account, "id", account))
                return None
            return await func(account, *args, **kwargs)
        return wrapper
    return decorator
