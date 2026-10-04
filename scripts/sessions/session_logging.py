"""Small local error log for interactive session-creation commands."""
from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path
import traceback
from collections.abc import Callable


def log_session_error(root: Path, operation: str, error: BaseException | str) -> Path | None:
    """Append an exception or failed-login message without recording phone inputs."""
    path = root / "data" / "logs" / "session_creation.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(error, BaseException):
            details = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        else:
            details = str(error).strip() or "Unknown error"
        with path.open("a", encoding="utf-8") as log_file:
            log_file.write(
                f"\n{'=' * 72}\n"
                f"{datetime.now().astimezone().isoformat(timespec='seconds')} | {operation}\n"
                f"{details.rstrip()}\n"
            )
        return path
    except OSError:
        return None


def run_session_command(root: Path, command: Callable[[], int | None], operation: str) -> int:
    """Keep configuration, dependency and runtime failures diagnosable for every entry point."""
    handler = None
    root_logger = logging.getLogger()
    try:
        log_path = root / "data" / "logs" / "session_creation.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.WARNING)
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
        root_logger.addHandler(handler)
    except OSError:
        print("Could not open the session log. Check the project folder's write permissions.")
    try:
        return command() or 0
    except KeyboardInterrupt:
        print("\nCancelled; existing sessions were preserved.")
        return 130
    except Exception as exc:
        path = log_session_error(root, operation, exc)
        print(f"{operation}: {type(exc).__name__}: {exc}")
        if path:
            print(f"Full traceback saved to: {path}")
        else:
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            print("Could not write the error log. Check the project folder's write permissions.")
        return 1
    finally:
        if handler is not None:
            root_logger.removeHandler(handler)
            handler.close()
