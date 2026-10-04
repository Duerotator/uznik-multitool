"""Frontend-neutral structured entries for user-visible operation logs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from core.models import utc_now_iso


Severity = Literal["debug", "info", "warning", "error"]


@dataclass(frozen=True)
class UiLogEntry:
    time: str
    severity: Severity
    group: str
    account: str
    operation: str
    message: str

    @classmethod
    def create(
        cls,
        message: str,
        *,
        severity: Severity = "info",
        group: str = "inbox",
        account: str = "",
        operation: str = "ui",
    ) -> "UiLogEntry":
        return cls(utc_now_iso(), severity, group or "inbox", account, operation or "ui", message)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def format_text(self) -> str:
        stamp = self.time[11:19] if len(self.time) >= 19 else self.time
        context = " / ".join(item for item in (self.operation, self.account) if item and item != "ui")
        return f"{stamp} | {self.severity.upper()}" + (f" | {context}" if context else "") + f" | {self.message}"
