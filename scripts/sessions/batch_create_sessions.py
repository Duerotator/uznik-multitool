"""Sequential phone-list login; codes and passwords are never stored in the list."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))
from scripts.sessions.create_session import login_phone
from scripts.sessions.session_logging import log_session_error, run_session_command


def load_phones(path: Path) -> list[str]:
    return list(dict.fromkeys(
        line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ))


async def run(config, phones: list[str], delay: float) -> int:
    failed = 0
    for index, phone in enumerate(phones, 1):
        print(f"[{index}/{len(phones)}] {phone}")
        exception_logged = False
        try:
            result = await login_phone(config, phone)
        except Exception as exc:
            path = log_session_error(ROOT, f"Batch item {index}/{len(phones)} raised an exception", exc)
            if path:
                print(f"Full traceback saved to: {path}")
            exception_logged = True
            result = {"ok": False, "error": str(exc)}
        if not result.get("ok"):
            error = result.get("error", "login failed")
            print(f"Skipped: {error}")
            if not exception_logged:
                path = log_session_error(ROOT, f"Batch item {index}/{len(phones)} failed", str(error))
                if path:
                    print(f"Error details saved to: {path}")
            failed += 1
        if index < len(phones):
            await asyncio.sleep(delay)
    return int(bool(failed))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create local sessions from your own phone list, one at a time.")
    parser.add_argument("--phones", type=Path, help="Defaults to sessions/batch_phones.txt.")
    parser.add_argument("--delay", type=float, default=8)
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must be nonnegative")
    os.chdir(ROOT)
    from core.config import AppConfig
    config = AppConfig.load(ROOT / ".env")
    phone_file = args.phones or config.batch_phones_file
    phones = load_phones(phone_file)
    if not phones:
        print(f"No phone numbers found in: {phone_file}")
        print("Enter one international-format number per line, save the file, then try again.")
        return 1
    config.require_telegram_api()
    return asyncio.run(run(config, phones, args.delay))


if __name__ == "__main__":
    raise SystemExit(run_session_command(ROOT, main, "Batch session creation failed"))
