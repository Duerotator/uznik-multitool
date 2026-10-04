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


def load_phones(path: Path) -> list[str]:
    return list(dict.fromkeys(
        line.strip() for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ))


async def run(config, phones: list[str], delay: float) -> int:
    failed = 0
    for index, phone in enumerate(phones, 1):
        print(f"[{index}/{len(phones)}] {phone}")
        try:
            result = await login_phone(config, phone)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        if not result.get("ok"):
            print(f"Skipped: {result.get('error', 'login failed')}")
            failed += 1
        if index < len(phones):
            await asyncio.sleep(delay)
    return int(bool(failed))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create local sessions from your own phone list, one at a time.")
    parser.add_argument("--phones", type=Path, help="Defaults to imports/batch_phones.txt.")
    parser.add_argument("--delay", type=float, default=8)
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay must be nonnegative")
    os.chdir(ROOT)
    from core.config import AppConfig
    config = AppConfig.load(ROOT / ".env")
    phones = load_phones(args.phones or config.import_dir / "batch_phones.txt")
    if not phones:
        print(f"Add your phones to {config.import_dir / 'batch_phones.txt'}, one per line.")
        return 0
    config.require_telegram_api()
    return asyncio.run(run(config, phones, args.delay))


if __name__ == "__main__":
    raise SystemExit(main())
