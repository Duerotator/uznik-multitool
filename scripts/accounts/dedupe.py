"""Preview duplicate account records; removal needs --execute and keeps sessions."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def duplicate_ids(service) -> list[str]:
    seen = set()
    duplicates = []
    for account in sorted(service.list_accounts(), key=lambda item: item.created_at):
        key = service._duplicate_key(account)
        if not key:
            continue
        if key in seen:
            duplicates.append(account.id)
        else:
            seen.add(key)
    return duplicates


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Remove duplicate records; session files are kept")
    args = parser.parse_args()
    os.chdir(ROOT)
    from core.config import AppConfig
    from modules.accounts import AccountService
    config = AppConfig.load(ROOT / ".env")
    service = AccountService(config)
    ids = duplicate_ids(service)
    print(f"Duplicate records: {len(ids)}")
    for account_id in ids:
        print(account_id)
    if not args.execute or not ids:
        print("Preview only; no account records or sessions removed.")
        return 0
    backup = config.data_dir / "backups" / f"accounts_before_dedupe_{time.time_ns()}.json"
    shutil.copy2(config.accounts_file, backup)
    count = service.deduplicate_accounts(delete_sessions=False)
    print(f"Removed {count} duplicate record(s). Session files kept. Recover records from: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
