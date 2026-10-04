"""Create new authorizations from supplied .session files, preserving originals."""
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


async def process_queue(config, paths) -> int:
    from modules.session_onboarding import ForeignSessionOnboarding

    async def code_provider(phone, source):
        return (await asyncio.to_thread(input, f"{source.name}: fresh Telegram code (empty = skip): ")).strip()

    async def password_provider(phone, source):
        return await asyncio.to_thread(getpass.getpass, f"{source.name}: your 2FA password (empty = skip): ")

    onboarding = ForeignSessionOnboarding(config)
    failures = 0
    for index, source in enumerate(paths, 1):
        try:
            result = await onboarding.process_one(source, code_provider, password_provider)
            print(f"[{index}/{len(paths)}] {source.name}: {result.status}")
            if result.status != "authorized":
                failures += 1
                print(result.error)
                log_session_error(ROOT, f"External session item {index}/{len(paths)} failed", result.error)
        except Exception as exc:
            failures += 1
            print(f"[{index}/{len(paths)}] {source.name}: failed — {exc}")
            path = log_session_error(ROOT, f"External session item {index}/{len(paths)} raised an exception", exc)
            if path:
                print(f"Full traceback saved to: {path}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Request new Telegram authorizations; otherwise list the queue only")
    args = parser.parse_args()
    os.chdir(ROOT)
    from core.config import AppConfig
    from modules.session_onboarding import pending_external_sessions
    config = AppConfig.load(ROOT / ".env")
    paths = pending_external_sessions(config)
    print(f"Queue: {config.import_dir / 'auth_input'} — {len(paths)} stable file(s)")
    for path in paths:
        print(path.name)
    if not args.execute or not paths:
        print("No Telegram requests made. Add --execute to process the queue." if not args.execute else "Queue is empty.")
        return 0
    config.require_telegram_api()
    return asyncio.run(process_queue(config, paths))


if __name__ == "__main__":
    raise SystemExit(run_session_command(ROOT, main, "External session processing failed"))
