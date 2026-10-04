from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from collections import deque

from core.config import AppConfig

_SHORT_FMT = "%(asctime)s | %(levelname).1s | %(name).12s | %(message)s"
_FULL_FMT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DEBUG_FORMAT = "%(asctime)s.%(msecs)03d | %(levelname)-8s | %(name)s | %(message)s"


class ShortFormatter(logging.Formatter):
    _MAP = {"DEBUG": "D", "INFO": "I", "WARNING": "W", "ERROR": "E", "CRITICAL": "C"}

    def format(self, record: logging.LogRecord) -> str:
        record.levelname = self._MAP.get(record.levelname, record.levelname[:1])
        name = record.name
        if len(name) > 12:
            parts = name.rsplit(".", 1)
            record.name = ".".join(p[:3] for p in name.split(".")) if name.count(".") > 1 else name[-12:]
        record.asctime = self.formatTime(record, "%H:%M:%S")
        return super().format(record)


class RingFileHandler(logging.Handler):
    def __init__(self, path, max_lines: int = 1000):
        super().__init__()
        self.path = path
        self.max_lines = max_lines
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.path.write_text("", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            rolled = len(self.lines) == self.max_lines
            self.lines.append(line)
            if rolled:
                self._rewrite()
            else:
                with self.path.open("a", encoding="utf-8") as file:
                    file.write(line + "\n")
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        self.acquire()
        try:
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


def configure_logging(config: AppConfig) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    stream = logging.StreamHandler()
    stream.setFormatter(ShortFormatter(_SHORT_FMT, datefmt="%H:%M:%S"))
    root.addHandler(stream)

    file_handler = RotatingFileHandler(
        config.logs_dir / "multitool.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FULL_FMT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(file_handler)

    session_handler = RingFileHandler(config.logs_dir / "current_session.log", max_lines=1000)
    session_handler.setFormatter(ShortFormatter(_SHORT_FMT, datefmt="%H:%M:%S"))
    root.addHandler(session_handler)

    debug_handler = RotatingFileHandler(
        config.logs_dir / "debug.log",
        maxBytes=10_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(logging.Formatter(_DEBUG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(debug_handler)

    crash_handler = RotatingFileHandler(
        config.logs_dir / "crash.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    crash_handler.setLevel(logging.CRITICAL)
    crash_handler.setFormatter(logging.Formatter(_DEBUG_FORMAT, datefmt="%Y-%m-%d %H:%M:%S"))
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
