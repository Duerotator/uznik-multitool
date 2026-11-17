"""Local-only inventory/create/encrypted-backup administration. No key import/RPC."""
from __future__ import annotations

import argparse
import getpass
import json
import sqlite3
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / "app"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from modules.financial_vault import FinancialVault, FinancialVaultError
from modules.financial_backup import backup, verify_backup, restore_new


def _confirm(phrase):
    if input("Type " + phrase + " to confirm: ") != phrase:
        raise FinancialVaultError("Cancelled; confirmation did not match")


def _passphrase(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise FinancialVaultError("Hidden terminal input unavailable; echo fallback forbidden") from None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline financial vault administration; no seed/password command-line arguments."
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("create")
    for name in ("backup", "verify", "restore-new"):
        command = commands.add_parser(name)
        command.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    vault = FinancialVault(args.data_dir)
    try:
        if args.command == "status":
            result = vault.status()
        else:
            if not sys.stdin.isatty():
                raise FinancialVaultError("Use an interactive local terminal; no piped passwords/confirmation")
            if args.command == "create":
                _confirm("CREATE FINANCIAL VAULT")
                vault.create(confirmed=True)
                result = vault.status()
            else:
                password = _passphrase("Unique financial backup passphrase (16+ characters): ")
                if args.command == "backup":
                    if _passphrase("Confirm backup passphrase: ") != password:
                        raise FinancialVaultError("Backup passphrases do not match")
                    _confirm("ENCRYPTED BACKUP")
                    result = backup(vault, args.file, password, confirmed=True)
                else:
                    result = verify_backup(args.file, password)
                    if args.command == "restore-new":
                        print(json.dumps(result, ensure_ascii=False))
                        print("Preserves old context/revisions; chain key verification remains required. No signer/replay.")
                        _confirm("RESTORE " + result["manifest_hash"])
                        result = restore_new(
                            vault,
                            args.file,
                            password,
                            confirmed=True,
                            expected_file_hash=result["file_hash"],
                            expected_manifest_hash=result["manifest_hash"],
                        )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (KeyboardInterrupt, EOFError):
        print("Financial vault operation cancelled.", file=sys.stderr)
        return 130
    except (OSError, sqlite3.Error, ValueError, FinancialVaultError) as exc:
        detail = str(exc) if isinstance(exc, FinancialVaultError) else type(exc).__name__
        print("Financial vault operation stopped: " + detail, file=sys.stderr)
        return 1
    finally:
        vault.lock()


if __name__ == "__main__":
    raise SystemExit(main())
