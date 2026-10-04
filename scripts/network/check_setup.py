"""Read-only local configuration check. Never displays keys, tokens or proxy URLs."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.chdir(ROOT)
    from core.config import AppConfig
    config = AppConfig.load(ROOT / ".env")
    print(f"Telegram API: {'configured' if config.api_id and config.api_hash else 'not configured'}")
    print(f"Global proxy: {'configured' if config.global_proxy else 'not configured; requires a verified pool proxy for new authorizations'}")
    print(f"Proxy mode: {os.getenv('PROXY_MODE', 'manual')}")
    print(f"auth_input folder: {(config.import_dir / 'auth_input').is_dir()}")
    print(f"Window icon: {(ROOT / 'assets/branding/uznik-multitool.ico').is_file()}")
    for name in ("ffmpeg", "tesseract"):
        print(f"{name}: {'available' if shutil.which(name) else 'not in PATH (optional)'}")
    print("No network requests made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
