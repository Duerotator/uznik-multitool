"""Atomic UI snapshot shared by desktop, web/Mini App and bot."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from core.models import utc_now_iso
from core.storage import read_json, update_json


@dataclass
class UiStateSnapshot:
    active_group: str = "inbox"
    selected_accounts: list[str] = field(default_factory=list)
    active_task: str = ""
    online_running: bool = False
    warmup_running: bool = False
    proxy_pool: dict[str, int] = field(default_factory=dict)
    session_health: dict[str, int] = field(default_factory=dict)
    operation_progress: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class UiStateStore:
    def __init__(self, config) -> None:
        self.path = config.data_dir / "ui_state.json"

    def get(self) -> UiStateSnapshot:
        raw = read_json(self.path, {})
        allowed = {key: raw[key] for key in UiStateSnapshot.__dataclass_fields__ if key in raw}
        return UiStateSnapshot(**allowed)

    def update(self, **changes: Any) -> UiStateSnapshot:
        allowed = set(UiStateSnapshot.__dataclass_fields__)
        invalid = set(changes) - allowed
        if invalid:
            raise ValueError(f"Unsupported UI state fields: {sorted(invalid)}")

        def mutate(raw: dict[str, Any]) -> dict[str, Any]:
            next_state = UiStateSnapshot(**{key: raw[key] for key in allowed if key in raw}).to_dict()
            if all(next_state[key] == value for key, value in changes.items()):
                return next_state
            next_state.update(changes)
            next_state["updated_at"] = utc_now_iso()
            return next_state

        return UiStateSnapshot(**update_json(self.path, {}, mutate, skip_unchanged=True))
