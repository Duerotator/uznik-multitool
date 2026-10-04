"""Read-only local configuration check. Never displays keys, tokens or proxy URLs."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from scripts.check_install import find_native_tool, installed_version


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
    print(f"Telegram client: Kurigram {installed_version('Kurigram') or 'not installed'} (module: pyrogram)")
    print(f"MTProto acceleration: TgCrypto-pyrofork {installed_version('TgCrypto-pyrofork') or 'not installed'}")
    for name in ("ffmpeg", "tesseract", "xray"):
        print(f"{name}: {'found' if find_native_tool(name) else 'not found; run setup.bat'}")
    print("Full installation check: .venv\\Scripts\\python.exe scripts/check_install.py --require-native")
    print("No network requests made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
