"""User-facing launcher for all session-creation workflows."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
from scripts.sessions.session_logging import run_session_command


def main() -> None:
    os.chdir(ROOT)
    from core.config import AppConfig
    config = AppConfig.load(ROOT / ".env")
    print(f"Project: {ROOT}\nSettings: {ROOT / '.env'}")
    actions = {
        "1": ["scripts/sessions/create_session.py"],
        "2": ["scripts/sessions/process_auth_input.py", "--execute"],
        "3": ["scripts/sessions/batch_create_sessions.py"],
    }
    while True:
        print("\nUznik MultiTool — create sessions")
        print("1. Login by phone\n2. Create new sessions from auth_input\n3. Login from batch_phones.txt")
        print("4. Open auth_input folder\n5. Open sessions folder\n6. Open batch_phones.txt\n0. Exit")
        choice = input("> ").strip()
        if choice == "0":
            return
        if choice == "4":
            os.startfile(str(config.import_dir / "auth_input"))
        elif choice == "5":
            os.startfile(str(config.batch_phones_file.parent))
        elif choice == "6":
            os.startfile(str(config.batch_phones_file))
        elif choice in actions:
            completed = subprocess.run([sys.executable, "-X", "utf8", *actions[choice]], cwd=ROOT, check=False)
            if completed.returncode:
                input("Session command stopped. Read the message above; press Enter to return to the menu.")


if __name__ == "__main__":
    raise SystemExit(run_session_command(ROOT, main, "Session menu failed"))
