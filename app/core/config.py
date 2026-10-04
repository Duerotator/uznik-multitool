from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import dotenv_values, load_dotenv
except ImportError:  # pragma: no cover - fallback for bootstrap commands
    dotenv_values = None

    def load_dotenv(*_args, **_kwargs) -> bool:
        return False


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class AppConfig:
    api_id: int
    api_hash: str
    default_backend: str
    data_dir: Path
    accounts_file: Path
    tasks_file: Path
    logs_dir: Path
    sessions_dir: Path
    groups_dir: Path
    import_dir: Path
    global_proxy: str | None
    max_concurrency: int
    min_action_delay: float
    max_action_delay: float
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    ai_allowed_chats: frozenset[str]
    email_domain: str
    email_inbox_api_url: str
    email_inbox_token: str
    proxy_pool_db: Path
    proxy_max_concurrency: int
    proxy_interval_minutes: int
    giveaway_browser_max_concurrency: int = 1
    giveaway_referral_batch_size: int = 1
    email_inbox_backend: str = "http"
    email_mailboxes_file: Path | None = None
    email_mailbox_host: str = ""
    email_mailbox_port: int = 0
    email_mailbox_folder: str = "INBOX"
    email_mailbox_socket_timeout: float = 15.0
    email_mailbox_poll_interval: float = 4.0
    batch_phones_file: Path | None = None
    env_file: Path | None = None

    @classmethod
    def load(cls, env_path: Path | str = ".env") -> "AppConfig":
        env_path = Path(env_path).resolve()
        project_root = env_path.parent
        if dotenv_values is None and env_path.is_file():
            raise RuntimeError("python-dotenv is missing. Run setup.bat to install the dependencies.")
        load_dotenv(env_path, encoding="utf-8-sig")
        file_env = dotenv_values(env_path, encoding="utf-8-sig") if dotenv_values is not None else {}

        # An empty Windows/user environment variable must not mask a populated
        # value in the project's .env file. Non-empty environment variables
        # remain the preferred override, matching python-dotenv's default.
        api_id_raw = os.getenv("TELEGRAM_API_ID")
        if not api_id_raw or not api_id_raw.strip():
            api_id_raw = file_env.get("TELEGRAM_API_ID") or "0"
        api_hash = os.getenv("TELEGRAM_API_HASH")
        if not api_hash or not api_hash.strip():
            api_hash = file_env.get("TELEGRAM_API_HASH") or ""
        try:
            api_id = int(api_id_raw)
        except ValueError:
            raise RuntimeError(f"TELEGRAM_API_ID must be a positive integer in {env_path}") from None

        def project_path(value: str | Path) -> Path:
            path = Path(value)
            return (path if path.is_absolute() else project_root / path).resolve()

        data_dir = project_path(os.getenv("TELEGRAM_DATA_DIR") or "data")
        sessions_dir = data_dir / "sessions"
        logs_dir = data_dir / "logs"
        groups_dir = data_dir / "groups"
        import_dir = project_path(os.getenv("TELEGRAM_IMPORT_DIR") or "imports")
        data_dir.mkdir(parents=True, exist_ok=True)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
        groups_dir.mkdir(parents=True, exist_ok=True)
        import_dir.mkdir(parents=True, exist_ok=True)

        batch_phones_file = project_path(os.getenv("TELEGRAM_BATCH_PHONES_FILE") or
                                        "sessions/batch_phones.txt")

        from core.project_layout import ensure_project_layout
        ensure_project_layout(project_root, data_dir, import_dir, batch_phones_file)

        allowed = frozenset(
            item.strip()
            for item in os.getenv("AI_ALLOWED_CHATS", "").split(",")
            if item.strip()
        )

        return cls(
            api_id=api_id,
            api_hash=api_hash.strip(),
            default_backend=os.getenv("TELEGRAM_DEFAULT_BACKEND", "pyrogram").lower(),
            data_dir=data_dir,
            accounts_file=data_dir / "accounts.json",
            tasks_file=data_dir / "tasks.json",
            logs_dir=logs_dir,
            sessions_dir=sessions_dir,
            groups_dir=groups_dir,
            import_dir=import_dir,
            global_proxy=os.getenv("TELEGRAM_GLOBAL_PROXY") or None,
            max_concurrency=_env_int("TELEGRAM_MAX_CONCURRENCY", 3),
            min_action_delay=_env_float("TELEGRAM_MIN_ACTION_DELAY", 1.0),
            max_action_delay=_env_float("TELEGRAM_MAX_ACTION_DELAY", 4.0),
            llm_provider=os.getenv("LLM_PROVIDER", "openai"),
            llm_api_key=os.getenv("LLM_API_KEY", ""),
            llm_model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            ai_allowed_chats=allowed,
            email_domain=os.getenv("EMAIL_DOMAIN", ""),
            email_inbox_api_url=os.getenv("EMAIL_INBOX_API_URL", "").rstrip("/"),
            email_inbox_token=os.getenv("EMAIL_INBOX_TOKEN", ""),
            email_inbox_backend=os.getenv("EMAIL_INBOX_BACKEND", "http").strip().lower(),
            email_mailboxes_file=project_path(os.getenv("EMAIL_MAILBOXES_FILE") or (import_dir / "emails/accounts.txt")),
            email_mailbox_host=os.getenv("EMAIL_MAILBOX_HOST", "").strip(),
            email_mailbox_port=_env_int("EMAIL_MAILBOX_PORT", 0),
            email_mailbox_folder=os.getenv("EMAIL_MAILBOX_FOLDER", "INBOX"),
            email_mailbox_socket_timeout=_env_float("EMAIL_MAILBOX_SOCKET_TIMEOUT", 15.0),
            email_mailbox_poll_interval=_env_float("EMAIL_MAILBOX_POLL_INTERVAL", 4.0),
            batch_phones_file=batch_phones_file,
            env_file=env_path,
            proxy_pool_db=data_dir / os.getenv("PROXY_POOL_DB", "proxy_pool.db"),
            proxy_max_concurrency=_env_int("PROXY_MAX_CONCURRENCY", 50),
            proxy_interval_minutes=_env_int("PROXY_INTERVAL_MINUTES", 30),
            giveaway_browser_max_concurrency=_env_int("GIVEAWAY_BROWSER_MAX_CONCURRENCY", 1),
            giveaway_referral_batch_size=_env_int("GIVEAWAY_REFERRAL_BATCH_SIZE", 1),
        )

    def require_telegram_api(self) -> None:
        missing = []
        if self.api_id <= 0:
            missing.append("TELEGRAM_API_ID")
        if not self.api_hash:
            missing.append("TELEGRAM_API_HASH")
        if missing:
            raise RuntimeError(
                f"Set {', '.join(missing)} in {self.env_file or '.env'} before connecting accounts."
            )
