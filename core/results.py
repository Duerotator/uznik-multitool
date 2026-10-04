from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ActionResult:
    ok: int = 0
    errors: int = 0
    details: dict[str, str] = field(default_factory=dict)
    error_messages: list[str] = field(default_factory=list)

    def add_ok(self, account_id: str) -> None:
        self.ok += 1
        self.details[account_id] = "ok"

    def add_error(self, account_id: str, error: str) -> None:
        self.errors += 1
        self.details[account_id] = error
        self.error_messages.append(str(error))

    @property
    def total(self) -> int:
        return self.ok + self.errors

    def summary(self, label: str) -> str:
        return f"{label} finished: ok={self.ok}, errors={self.errors}, total={self.total}"
