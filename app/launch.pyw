"""Double-click launcher; all paths are relative to this copy of the project."""
from __future__ import annotations

import ctypes
import os
import subprocess
from pathlib import Path


def show_error(message: str) -> None:
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "Uznik MultiTool", 0x10)
    else:
        print(message)


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    executable = root / ".venv" / "Scripts" / "pythonw.exe"
    if not executable.is_file():
        show_error("Сначала запустите setup.bat в папке Uznik MultiTool.\nОн установит зависимости и создаст ярлык.")
        return 1
    logs = root / "data" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / "launcher.log"
    try:
        with log_path.open("a", encoding="utf-8") as output:
            result = subprocess.run(
                [str(executable), str(root / "app/main.py")],
                cwd=root,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        if result.returncode:
            show_error(f"Приложение завершилось с ошибкой.\nПодробности: {log_path}\nПопробуйте scripts/windows/start_debug.bat.")
        return result.returncode
    except OSError as exc:
        show_error(f"Не удалось запустить приложение: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
