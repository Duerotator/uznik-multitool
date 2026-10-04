"""Small console launcher for desktop helper tools."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))


def main() -> None:
    os.chdir(ROOT)
    from core.config import AppConfig
    config = AppConfig.load(ROOT / ".env")
    actions = {
        "1": ("Create one session (requests a login code)", ["scripts/sessions/create_session.py"]),
        "2": ("Process auth_input (requests login codes)", ["scripts/sessions/process_auth_input.py", "--execute"]),
        "3": ("Batch login: show instructions", ["scripts/sessions/batch_create_sessions.py", "--help"]),
        "4": ("Account cleanup: show instructions", ["scripts/accounts/cleanup.py", "--help"]),
        "5": ("Duplicate records: preview only", ["scripts/accounts/dedupe.py"]),
        "6": ("Passkeys: show instructions", ["scripts/accounts/security.py", "--help"]),
        "7": ("Avatar packs: show instructions", ["scripts/profiles/download_avatar_pack.py", "--help"]),
        "8": ("Check local setup (no network)", ["scripts/network/check_setup.py"]),
    }
    while True:
        print("\nUznik MultiTool — helper tools")
        for key, (label, _) in actions.items():
            print(f"{key}. {label}")
        print("9. Open auth_input folder\n0. Exit")
        choice = input("> ").strip()
        if choice == "0":
            return
        if choice == "9":
            os.startfile(str(config.import_dir / "auth_input"))
        elif choice in actions:
            subprocess.run([sys.executable, *actions[choice][1]], cwd=ROOT, check=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
