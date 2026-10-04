from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.models import utc_now_iso
from core.storage import read_json, write_json_atomic


@dataclass
class ScenarioStep:
    action: str
    label: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "label": self.label,
            "params": self.params,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScenarioStep":
        return cls(
            action=str(data.get("action") or ""),
            label=str(data.get("label") or data.get("action") or ""),
            params=dict(data.get("params") or {}),
        )


@dataclass
class Scenario:
    name: str
    steps: list[ScenarioStep]
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scenario":
        return cls(
            name=str(data.get("name") or "scenario"),
            description=str(data.get("description") or ""),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            steps=[ScenarioStep.from_dict(item) for item in data.get("steps", [])],
        )


class ScenarioStore:
    def __init__(self, config: AppConfig):
        self.config = config
        self.scenarios_dir = config.data_dir / "scenarios"
        self.scenarios_dir.mkdir(parents=True, exist_ok=True)

    def list_names(self) -> list[str]:
        names: list[str] = []
        for path in sorted(self.scenarios_dir.glob("*.json")):
            try:
                scenario = self.load(path.stem)
            except (OSError, ValueError, TypeError):
                continue
            names.append(scenario.name)
        return names

    def load(self, name: str) -> Scenario:
        path = self.path_for(name)
        raw = read_json(path, {})
        if not raw:
            raise FileNotFoundError(path)
        return Scenario.from_dict(raw)

    def save(self, scenario: Scenario) -> Path:
        scenario.updated_at = utc_now_iso()
        path = self.path_for(scenario.name)
        write_json_atomic(path, scenario.to_dict())
        return path

    def delete(self, name: str) -> bool:
        path = self.path_for(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def path_for(self, name: str) -> Path:
        return self.scenarios_dir / f"{self.slug(name)}.json"

    def slug(self, name: str) -> str:
        value = re.sub(r"[^A-Za-z0-9_.-]+", "_", name.strip())
        value = value.strip("._-")
        return value or "scenario"
