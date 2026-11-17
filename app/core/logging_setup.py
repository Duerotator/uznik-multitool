from __future__ import annotations

import copy
import logging
import re
from collections import deque
from logging.handlers import RotatingFileHandler

from core.config import AppConfig

_SHORT_FMT = "%(asctime)s | %(levelname).1s | %(name).12s | %(message)s"
_FULL_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DEBUG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(message)s"

_URL_USERINFO_PASS_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s?#;@:]+):([^/\s?#;@]*)@"
)
_URL_USERINFO_ONLY_RE = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s?#;@:]+)@"
)
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:api[_-]?key|apikey|key|token|access_token|refresh_token|bot_token|secret|password|passwd|pass|auth|sig|signature)=)([^&\s'\"]+)",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]{6,})",
    re.IGNORECASE,
)
_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b")
_KV_SECRET_RE = re.compile(
    r"(?i)(\b(?:api_hash|api_key|session_string|private_key(?:_pem)?|cloud_password(?:_val)?|"
    r"password|passwd|passphrase|secret_key|bot_token)\b\s*[:=]\s*)"
    r"(?:(['\"])([^\r\n'\"]{4,})\2|([^\s,;'\")}\]]{4,}))"
)
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-/])([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b"
)
_SESSION_BLOB_RE = re.compile(r"\b[A-Za-z0-9_-]{140,}={0,2}\b")
_SAFE_PLACEHOLDERS = frozenset({"none", "null", "true", "false", "missing", "configured", "required", "***"})


def _mask_email(match: re.Match[str]) -> str:
    local, domain = match.group(1), match.group(2)
    return f"{local[0]}***@{domain}"


def _mask_kv(match: re.Match[str]) -> str:
    prefix, quote = match.group(1), match.group(2) or ""
    value = match.group(3) if match.group(3) is not None else (match.group(4) or "")
    if value.lower() in _SAFE_PLACEHOLDERS:
        return match.group(0)
    return f"{prefix}{quote}***{quote}"


def redact_secrets(text: str) -> str:
    """Strip credentials, tokens, proxy passwords, query secrets, emails, and session blobs from log text."""
    if not text:
        return text
    result = _URL_USERINFO_PASS_RE.sub(r"\1***:***@", text)
    result = _URL_USERINFO_ONLY_RE.sub(r"\1***@", result)
    result = _QUERY_SECRET_RE.sub(r"\1***", result)
    result = _AUTH_HEADER_RE.sub(r"\1 ***", result)
    result = _BOT_TOKEN_RE.sub("<redacted-bot-token>", result)
    result = _KV_SECRET_RE.sub(_mask_kv, result)
    result = _SESSION_BLOB_RE.sub("<redacted-session>", result)
    result = _EMAIL_RE.sub(_mask_email, result)
    return result


class SecretRedactionFilter(logging.Filter):
    """Logging filter scrubbing sensitive tokens, proxy credentials, and PII from log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            redacted = redact_secrets(rendered)
            if redacted != rendered:
                record.msg = redacted
                record.args = ()
            elif isinstance(record.msg, str):
                redacted_msg = redact_secrets(record.msg)
                if redacted_msg != record.msg:
                    record.msg = redacted_msg
        except Exception:
            if isinstance(record.msg, str):
                record.msg = redact_secrets(record.msg)
        if isinstance(record.exc_text, str):
            record.exc_text = redact_secrets(record.exc_text)
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


class ShortFormatter(_RedactingFormatter):
    _MAP = {"DEBUG": "D", "INFO": "I", "WARNING": "W", "ERROR": "E", "CRITICAL": "C"}

    def format(self, record: logging.LogRecord) -> str:
        cloned = copy.copy(record)
        cloned.levelname = self._MAP.get(cloned.levelname, cloned.levelname[:1])
        name = cloned.name
        if len(name) > 12:
            cloned.name = ".".join(p[:3] for p in name.split(".")) if name.count(".") > 1 else name[-12:]
        cloned.asctime = self.formatTime(cloned, "%H:%M:%S")
        return super().format(cloned)


class RingFileHandler(logging.Handler):
    def __init__(self, path, max_lines: int = 1000):
        super().__init__()
        self.path = path
        self.max_lines = max_lines
        self.lines: deque[str] = deque(maxlen=max_lines)
        self._written_lines = 0
        self._compact_at = max(max_lines + 1, max_lines + max(100, max_lines // 2))
        self.path.write_text("", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record).replace("\r", " ").replace("\n", "\\n")
            self.lines.append(line)
            self._written_lines += 1
            if self._written_lines >= self._compact_at:
                self._rewrite()
            else:
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(line + "\n")
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        self.acquire()
        try:
            if self._written_lines > len(self.lines):
                self._rewrite()
        finally:
            self.release()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            super().close()

    def _rewrite(self) -> None:
        text = "\n".join(self.lines) + "\n" if self.lines else ""
        self.path.write_text(text, encoding="utf-8")
        self._written_lines = len(self.lines)


def configure_logging(config: AppConfig) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    try:
        from core.diagnostics import set_diagnostics_logs_dir
        set_diagnostics_logs_dir(config.logs_dir)
    except Exception:
        pass

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for old in list(root.handlers):
        try:
            old.close()
        except Exception:
            pass
    root.handlers.clear()

    redaction = SecretRedactionFilter()

    stream = logging.StreamHandler()
    stream.addFilter(redaction)
    stream.setFormatter(ShortFormatter(_SHORT_FMT, datefmt="%H:%M:%S"))
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        config.logs_dir / "multitool.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.addFilter(redaction)
    file_handler.setFormatter(_RedactingFormatter(_FULL_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(file_handler)

    session_handler = RingFileHandler(config.logs_dir / "current_session.log", max_lines=1000)
    session_handler.addFilter(redaction)
    session_handler.setFormatter(ShortFormatter(_SHORT_FMT, datefmt="%H:%M:%S"))
    root.addHandler(session_handler)

    debug_handler = RotatingFileHandler(
        config.logs_dir / "debug.log",
        maxBytes=10_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    debug_handler.addFilter(redaction)
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(_RedactingFormatter(_DEBUG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(debug_handler)

    crash_handler = RotatingFileHandler(
        config.logs_dir / "crash.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    crash_handler.addFilter(redaction)
    crash_handler.setLevel(logging.CRITICAL)
    crash_handler.setFormatter(_RedactingFormatter(_DEBUG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(crash_handler)

    # Pyrogram is very chatty at INFO level (PingTask/NetworkTask messages).
    # Keep our own logs readable and show Pyrogram only when something matters.
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("proxy-diag").setLevel(logging.DEBUG)
    logging.getLogger("diagnostics").setLevel(logging.DEBUG)
    logging.getLogger("resource-monitor").setLevel(logging.DEBUG)
    logging.getLogger("ps").setLevel(logging.DEBUG)
    logging.getLogger("session-health").setLevel(logging.DEBUG)
    logging.getLogger("scrape_cache").setLevel(logging.DEBUG)


def account_logger(account_id: str) -> logging.Logger:
    return logging.getLogger(f"account.{account_id}")
