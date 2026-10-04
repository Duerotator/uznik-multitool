"""Passkey helpers using the application's configuration and verified clients."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register-passkey", "restore-passkey"))
    parser.add_argument("--accounts", nargs="+", required=True, help="Explicit account IDs from your dashboard")
    parser.add_argument("--execute", action="store_true", help="Perform the action; otherwise preview selected records")
    args = parser.parse_args()
    os.chdir(ROOT)
    from core.config import AppConfig
    from modules.accounts import AccountService
    config = AppConfig.load(ROOT / ".env")
    service = AccountService(config)
    accounts = [service.get_account(account_id) for account_id in dict.fromkeys(args.accounts)]
    for account in accounts:
        print(f"{args.action}: {account.id}")
    if not args.execute:
        print("Preview only. Add --execute to make Telegram requests.")
        return 0
    config.require_telegram_api()
    from modules.account_security import AccountSecurityService
    security = AccountSecurityService(config)
    operation = security.add_passkeys if args.action == "register-passkey" else security.restore_passkeys
    result = asyncio.run(operation(accounts))
    print(f"Completed: {result.ok}; failed: {result.errors}")
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
