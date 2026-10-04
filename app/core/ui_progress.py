"""Thread-safe, frontend-neutral progress for observable bulk operations."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class OperationProgress:
    operation: str
    total: int
    completed: int = 0
    ok: int = 0
    errors: int = 0
    current_account: str = ""
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    on_update: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def set_total(self, total: int) -> None:
        """Switch an indeterminate operation to its discovered work count."""
        with self._lock:
            self.total = max(self.completed, int(total), 0)
            snapshot = self._snapshot()
        if self.on_update:
            self.on_update(snapshot)

    @classmethod
    def indeterminate(
        cls,
        operation: str,
        *,
        on_update: Callable[[dict[str, Any]], None] | None = None,
    ) -> "OperationProgress":
        """Represent a running operation whose amount of work is not yet known."""
        return cls(operation=operation, total=0, on_update=on_update)

    @property
    def determinate(self) -> bool:
        return self.total > 0

    def mark_ok(self, account_id: str, *args: Any, **kwargs: Any) -> None:
        self._mark(account_id, success=True)

    def mark_error(self, account_id: str, error: str = "", *args: Any, **kwargs: Any) -> None:
        self._mark(account_id, success=False)

    def _mark(self, account_id: str, *, success: bool) -> None:
        snapshot: dict[str, Any] | None = None
        with self._lock:
            if self.completed >= self.total:
                return
            self.current_account = str(account_id)
            self.completed += 1
            if success:
                self.ok += 1
            else:
                self.errors += 1
            snapshot = self._snapshot()
        if self.on_update and snapshot is not None:
            self.on_update(snapshot)

    def to_dict(self, *, now: float | None = None) -> dict[str, Any]:
        with self._lock:
            return self._snapshot(now=now)

    def _snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        elapsed = max(0.0, (time.monotonic() if now is None else now) - self.started_monotonic)
        remaining = max(0, self.total - self.completed)
        eta_seconds = round((elapsed / self.completed) * remaining) if self.completed else None
        return {
            "mode": "determinate" if self.determinate else "indeterminate",
            "operation": self.operation,
            "total": self.total,
            "completed": self.completed,
            "ok": self.ok,
            "errors": self.errors,
            "current_account": self.current_account,
            "eta_seconds": eta_seconds,
        }
