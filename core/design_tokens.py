"""Semantic design tokens shared by desktop and web renderers.

Renderers intentionally map these semantic names to their native styling
systems (QSS or CSS); no UI should introduce a second palette.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class DesignTokens:
    background: str = "#0f131a"
    panel: str = "#171c24"
    panel_secondary: str = "#1d2430"
    border: str = "#2b3747"
    text: str = "#e9f0f8"
    muted: str = "#93a1b4"
    primary: str = "#2f73bc"
    success: str = "#2c6c4c"
    warning: str = "#6d5520"
    danger: str = "#6d3630"
    radius: int = 8
    spacing: int = 8

    def css_variables(self) -> str:
        values = asdict(self)
        return ";".join(
            f"--token-{key.replace('_', '-')}: {value}px" if key in {"radius", "spacing"}
            else f"--token-{key.replace('_', '-')}: {value}"
            for key, value in values.items()
        ) + ";"


TOKENS = DesignTokens()
