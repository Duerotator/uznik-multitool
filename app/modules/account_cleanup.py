"""Account removal releases local references without deleting archived profiles."""
from __future__ import annotations

import hashlib
import shutil
import weakref
from pathlib import Path

from core.storage import update_json


_BROWSERS: dict[str, weakref.WeakSet] = {}


def register_browser_service(data_dir: Path, service) -> None:
    _BROWSERS.setdefault(str(Path(data_dir).resolve()), weakref.WeakSet()).add(service)


def remove_browser_profile(data_dir: Path, account_id: str) -> None:
    root = (Path(data_dir) / "browser_profiles").resolve()
    target = root / hashlib.sha256(account_id.encode()).hexdigest()[:24]
    if target.is_symlink() or target.resolve().parent != root:
        raise RuntimeError("Unsafe browser profile path")
    if target.is_dir():
        shutil.rmtree(target)


def cleanup_removed_accounts(config, account_ids: list[str]) -> None:
    wanted = set(account_ids)
    if not wanted:
        return
    for filename, container in (
        ("profile_bindings.json", "bindings"), ("fingerprints.json", None),
        ("story_publications.json", None),
    ):
        path = Path(config.data_dir) / filename
        if not path.exists():
            continue
        def mutate(data):
            records = data.get(container, {}) if container else data
            for key in wanted:
                records.pop(key, None)
            return data
        update_json(path, {}, mutate, skip_unchanged=True)
    services = list(_BROWSERS.get(str(Path(config.data_dir).resolve()), ()))
    for account_id in wanted:
        closing = any([service.account_removed(account_id) for service in services])
        if not closing:
            remove_browser_profile(config.data_dir, account_id)
