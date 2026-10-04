"""Interactive creation of a local authorization using the shared desktop backend."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
from scripts.sessions.session_logging import log_session_error, run_session_command


async def login_phone(config, phone: str) -> dict:
    from modules.auth_controller import AuthManager
    auth = AuthManager(config)
    try:
        sent = await auth.send_code(phone)
        if not sent.get("ok"):
            return sent
        code = await asyncio.to_thread(input, "Fresh code from Telegram: ")
        result = await auth.sign_in(phone, sent["phone_code_hash"], code.strip().replace(" ", ""))
        if result.get("status") == "2fa_required":
            password = await asyncio.to_thread(getpass.getpass, "Your 2FA password: ")
            result = await auth.check_password(phone, password)
        if result.get("ok"):
            print(f"Created and imported: {result['session_path']}")
        return result
    finally:
        await auth.close_session(phone)


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and import your own Telegram session through your verified proxy.")
    parser.add_argument("--phone", help="Your phone in international format; prompted if omitted.")
    args = parser.parse_args()
    os.chdir(ROOT)
    from core.config import AppConfig
    config = AppConfig.load(ROOT / ".env")
    config.require_telegram_api()
    phone = args.phone or input("Your phone number: ").strip()
    if not phone:
        return 1
    result = asyncio.run(login_phone(config, phone))
    if not result.get("ok"):
        error = result.get("error", "unknown error")
        print(f"Login failed: {error}")
        path = log_session_error(ROOT, "Single session login failed", str(error))
        if path:
            print(f"Error details saved to: {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run_session_command(ROOT, main, "Session creation failed"))
