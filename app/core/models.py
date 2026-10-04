from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


Backend = Literal["pyrogram", "telethon"]
SessionKind = Literal["session_file", "session_string", "json_dump", "tdata"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ProxyConfig:
    scheme: str
    hostname: str
    port: int
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_url(cls, url: str | None) -> "ProxyConfig | None":
        if not url:
            return None
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.hostname or not parsed.port:
            raise ValueError(f"Invalid proxy URL: {url}")
        return cls(
            scheme=parsed.scheme,
            hostname=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
        )

    def to_pyrogram(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "scheme": self.scheme.replace("socks5h", "socks5"),
            "hostname": self.hostname,
            "port": self.port,
        }
        if self.username:
            result["username"] = self.username
        if self.password:
            result["password"] = self.password
        return result

    def to_telethon(self) -> tuple[Any, ...]:
        import socks

        proxy_type = socks.SOCKS5 if self.scheme.startswith("socks5") else socks.HTTP
        return (proxy_type, self.hostname, self.port, True, self.username, self.password)


@dataclass
class AccountRecord:
    id: str
    label: str
    backend: Backend
    session_kind: SessionKind
    session_ref: str
    group: str = "default"
    groups: list[str] = field(default_factory=list)
    enabled: bool = True
    proxy: str | None = None
    phone: str | None = None
    user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    persona: str | None = None
    profiled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data["groups"] and data["group"] not in {"", "inbox", "default"}:
            data["groups"] = [data["group"]]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountRecord":
        data = dict(data)
        groups = data.get("groups")
        if groups is None:
            legacy_group = str(data.get("group") or "inbox")
            data["groups"] = [] if legacy_group in {"", "inbox", "default"} else [legacy_group]
        elif not isinstance(groups, list):
            data["groups"] = [str(groups)] if str(groups).strip() else []
        data["groups"] = [str(group) for group in data.get("groups", []) if str(group).strip()]
        if not data.get("group"):
            data["group"] = data["groups"][0] if data["groups"] else "inbox"
        return cls(**data)

    def has_profile(self) -> bool:
        return bool(self.profiled or self.metadata.get("profiled"))

    def mark_as_profiled(self) -> None:
        self.profiled = True
        self.metadata["profiled"] = True


@dataclass
class TaskSnapshot:
    id: str
    name: str
    status: Literal["pending", "running", "stopped", "failed", "done"]
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
