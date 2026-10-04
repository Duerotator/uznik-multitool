from __future__ import annotations

import json
import copy
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


_WRITE_LOCKS: dict[str, threading.RLock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()
_LOGGER = logging.getLogger(__name__)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    for attempt in range(3):
        try:
            with path.open("r", encoding="utf-8-sig") as file:
                return json.load(file)
        except PermissionError:
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))
                continue
            raise
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            _quarantine_unreadable(path, error)
            return default
    return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _write_lock(path)
    temp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    with lock:
        try:
            with temp_path.open("w", encoding="utf-8") as file:
                json.dump(data, file, ensure_ascii=False, indent=2)
            _replace_with_retries(temp_path, path)
        finally:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def update_json(
    path: Path, default: Any, mutate: Callable[[Any], Any], *, skip_unchanged: bool = False,
) -> Any:
    with _write_lock(path):
        previous = read_json(path, default)
        data = mutate(copy.deepcopy(previous) if skip_unchanged else previous)
        if skip_unchanged and data == previous and path.exists():
            return data
        write_json_atomic(path, data)
        return data


def _write_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _WRITE_LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[key] = lock
        return lock


def _quarantine_unreadable(path: Path, error: Exception) -> None:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    index = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{index}")
        index += 1
    try:
        os.replace(path, target)
    except OSError as move_error:
        _LOGGER.error("Unreadable JSON %s (%s); left in place: %s", path, error, move_error)
        return
    _LOGGER.error("Unreadable JSON %s (%s); moved aside to %s", path, error, target)


def _replace_with_retries(temp_path: Path, path: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(temp_path, path)
            return
        except OSError as exc:
            winerror = getattr(exc, "winerror", None)
            if winerror not in {5, 32} or attempt == 7:
                raise
            time.sleep(0.08 * (attempt + 1))
