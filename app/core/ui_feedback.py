"""Shared outcome vocabulary for UI, Mini App and bot responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


FeedbackKind = Literal["success", "warning", "error", "progress", "action_required"]


@dataclass(frozen=True)
class Feedback:
    kind: FeedbackKind
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "message": self.message}
