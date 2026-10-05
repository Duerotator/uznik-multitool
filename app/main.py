"""Standalone desktop entry point for Uznik MultiTool."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    root = app_dir.parent
    sys.path.insert(0, str(app_dir))
    os.chdir(root)
    from core.config import AppConfig
    from core.diagnostics import install_global_exception_handler
    from core.logging_setup import configure_logging
    from core.desktop_identity import initialize_desktop_identity
    initialize_desktop_identity()  # Before importing Qt / creating any windows.
    config = AppConfig.load(root / ".env")
    configure_logging(config)
    install_global_exception_handler()
    from ui.qt_app import run_desktop_app

    run_desktop_app(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
