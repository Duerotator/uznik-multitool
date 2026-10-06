"""Encrypted backup/restore uses only temporary files and fake session data."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from modules.local_backup import LocalBackup, MAGIC, RestoreRollbackError

PASSWORD = "fixture-password-only"


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = SimpleNamespace(data_dir=self.root / "data", import_dir=self.root / "imports", env_file=self.root / ".env")
        self.config.data_dir.mkdir()
        self.config.import_dir.mkdir()
        self.config.env_file.write_text("TELEGRAM_API_HASH=fixture-private-value\n", encoding="utf-8")
        self.accounts = self.config.data_dir / "accounts.json"
        self.accounts.write_text('{"accounts": []}', encoding="utf-8")
        self.service = LocalBackup(self.config)
        self.archive = self.config.data_dir / "backups/fixture.uzbk"

    def test_round_trip_has_encryption_and_pre_restore_recovery(self):
        source = self.config.import_dir / "emails.txt"
        source.write_text("fixture@example.invalid:private-value", encoding="utf-8")
        self.service.create(self.archive, PASSWORD)
        self.assertNotIn(b"private-value", self.archive.read_bytes())
        self.accounts.write_text('{"accounts": [], "new": true}')
        source.write_text("new content")
        recovery = self.service.restore(self.archive, PASSWORD)
        self.assertTrue(recovery.is_file())
        self.assertEqual("fixture@example.invalid:private-value", source.read_text())
        recovery_zip = self.root / "recovery.zip"
        self.service._decrypt(recovery, PASSWORD, recovery_zip)
        with zipfile.ZipFile(recovery_zip) as bundle:
            self.assertTrue(json.loads(bundle.read("data/accounts.json"))["new"])

    def test_wrong_password_and_tamper_never_change_data(self):
        self.service.create(self.archive, PASSWORD)
        original = self.accounts.read_bytes()
        with self.assertRaisesRegex(ValueError, "Wrong password"):
            self.service.restore(self.archive, "different-password")
        payload = bytearray(self.archive.read_bytes())
        payload[-19] ^= 1
        self.archive.write_bytes(payload)
        with self.assertRaisesRegex(ValueError, "damaged"):
            self.service.restore(self.archive, PASSWORD)
        self.assertEqual(original, self.accounts.read_bytes())
        self.assertEqual([self.archive], list(self.archive.parent.glob("*.uzbk")))

    def test_recursive_backups_browser_cache_logs_and_scripts_are_excluded(self):
        for folder in ("backups", "browser_profiles", "logs", "vpn_gateway"):
            path = self.config.data_dir / folder
            path.mkdir(exist_ok=True)
            (path / "private.txt").write_text("excluded")
        sessions = self.root / "sessions"
        sessions.mkdir()
        (sessions / "batch_phones.txt").write_text("fixture phone")
        (sessions / "evil.py").write_text("not backed up")
        self.service.create(self.archive, PASSWORD)
        decrypted = self.root / "payload.zip"
        self.service._decrypt(self.archive, PASSWORD, decrypted)
        with zipfile.ZipFile(decrypted) as bundle:
            self.assertEqual({"data/accounts.json", "phones/batch_phones.txt", "environment/.env", "manifest.json"}, set(bundle.namelist()))

    def test_sqlite_wal_is_snapshotted_consistently(self):
        database = self.config.data_dir / "fixture.session"
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE fixture(value)")
        connection.execute("INSERT INTO fixture VALUES ('committed')")
        connection.commit()
        self.service.create(self.archive, PASSWORD)
        decrypted = self.root / "payload.zip"
        snapshot = self.root / "snapshot.session"
        self.service._decrypt(self.archive, PASSWORD, decrypted)
        with zipfile.ZipFile(decrypted) as bundle:
            self.assertNotIn("data/fixture.session-wal", bundle.namelist())
            snapshot.write_bytes(bundle.read("data/fixture.session"))
        with closing(sqlite3.connect(snapshot)) as restored:
            self.assertEqual(("committed",), restored.execute("SELECT value FROM fixture").fetchone())

    def test_unsafe_paths_are_rejected(self):
        for name in ("../outside", "data/../outside", "data/a//b", "data/a/./b", "/data/a",
                     "data/C:/a", "data/a\\b", "data/CON.txt", "data/trailing. ",
                     "app/main.py", "phones/run.py", "data/backups/bad", "data/browser_profiles/cache"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.service._target(name)

    def malicious_archive(self, name):
        media = b"fixture"
        manifest = {"format": 1, "roots": {"data": str(self.config.data_dir)},
                    "files": {name: {"size": len(media), "sha256": hashlib.sha256(media).hexdigest()}}}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(name, media)
            bundle.writestr("manifest.json", json.dumps(manifest))
        salt, nonce = os.urandom(16), os.urandom(12)
        header = MAGIC + salt + nonce
        encryptor = Cipher(algorithms.AES(self.service._key(PASSWORD, salt)), modes.GCM(nonce)).encryptor()
        encryptor.authenticate_additional_data(header)
        self.archive.parent.mkdir(exist_ok=True)
        self.archive.write_bytes(header + encryptor.update(buffer.getvalue()) + encryptor.finalize() + encryptor.tag)

    def test_authenticated_archive_still_cannot_escape_destination(self):
        self.malicious_archive("data/../../escaped.txt")
        original = self.accounts.read_bytes()
        with self.assertRaises(ValueError):
            self.service.restore(self.archive, PASSWORD)
        self.assertFalse((self.root / "escaped.txt").exists())
        self.assertEqual(original, self.accounts.read_bytes())

    def test_restore_failure_rolls_back_and_preserves_recovery_backup(self):
        self.service.create(self.archive, PASSWORD)
        self.accounts.write_text('{"accounts": [], "changed": true}')
        original = self.accounts.read_bytes()
        actual = self.service._replace_file
        failed = False
        def fail(source, destination, *args, **kwargs):
            nonlocal failed
            if not failed and Path(destination) == self.config.env_file:
                failed = True
                raise OSError("fixture disk failure")
            return actual(source, destination, *args, **kwargs)
        with patch.object(self.service, "_replace_file", side_effect=fail), self.assertRaises(OSError):
            self.service.restore(self.archive, PASSWORD)
        self.assertEqual(original, self.accounts.read_bytes())
        self.assertTrue(list(self.archive.parent.glob("before-restore-*.uzbk")))

    def test_rebase_session_paths_when_restoring_into_another_install(self):
        old_session = self.config.data_dir / "sessions/pyrogram/fixture.json"
        old_session.parent.mkdir(parents=True)
        old_session.write_text('{"session_string": "fixture"}')
        self.accounts.write_text(json.dumps({"accounts": [{"session_ref": str(old_session), "metadata": {"source_path": "old machine"}}]}))
        self.service.create(self.archive, PASSWORD)
        other_root = self.root / "other"
        other_root.mkdir()
        other = SimpleNamespace(data_dir=other_root / "data", import_dir=other_root / "imports", env_file=other_root / ".env")
        LocalBackup(other).restore(self.archive, PASSWORD)
        restored = json.loads((other.data_dir / "accounts.json").read_text())["accounts"][0]
        self.assertEqual(str(other.data_dir / "sessions/pyrogram/fixture.json"), restored["session_ref"])
        self.assertNotIn("source_path", restored["metadata"])
        self.assertIn(other.data_dir.as_posix(), other.env_file.read_text())

    def test_backup_canonicalizes_managed_session_reference_without_editing_source(self):
        canonical = self.config.data_dir / "sessions/pyrogram/fixture.json"
        canonical.parent.mkdir(parents=True)
        canonical.write_text('{"session_string": "fixture"}')
        # On GitHub's Windows runner the temporary directory contains RUNNER~1;
        # the service's root resolves to runneradmin. Windows is case-insensitive.
        old_ref = str(Path(self.temp.name) / "data/sessions/pyrogram/fixture.json")
        if os.name == "nt":
            old_ref = old_ref.upper()
        self.accounts.write_text(json.dumps({"accounts": [{"session_ref": old_ref}]}))
        original = self.accounts.read_bytes()
        self.service.create(self.archive, PASSWORD)
        self.assertEqual(original, self.accounts.read_bytes())
        decrypted = self.root / "payload.zip"
        self.service._decrypt(self.archive, PASSWORD, decrypted)
        with zipfile.ZipFile(decrypted) as bundle:
            archived = json.loads(bundle.read("data/accounts.json"))["accounts"][0]
        self.assertEqual(str(canonical), archived["session_ref"])
        receiver = SimpleNamespace(data_dir=self.root / "other/data", import_dir=self.root / "other/imports", env_file=self.root / "other/.env")
        LocalBackup(receiver).restore(self.archive, PASSWORD)
        restored = json.loads((receiver.data_dir / "accounts.json").read_text())["accounts"][0]
        self.assertEqual(str(receiver.data_dir / "sessions/pyrogram/fixture.json"), restored["session_ref"])

    def test_incomplete_rollback_reports_recovery_and_attempts_other_files(self):
        self.service.create(self.archive, PASSWORD)
        actual = self.service._replace_file
        rollback_targets = []
        def fail(source, destination):
            source, destination = Path(source), Path(destination)
            if source.parent.name == "undo":
                rollback_targets.append(destination)
                if destination == self.config.env_file:
                    raise OSError("fixture rollback failure")
            elif destination == self.config.env_file:
                raise OSError("fixture restore failure")
            return actual(source, destination)
        with patch.object(self.service, "_replace_file", side_effect=fail), self.assertRaisesRegex(RestoreRollbackError, "before-restore-"):
            self.service.restore(self.archive, PASSWORD)
        self.assertIn(self.accounts, rollback_targets)
        self.assertIn(self.config.env_file, rollback_targets)

    def test_existing_backup_is_never_overwritten(self):
        self.service.create(self.archive, PASSWORD)
        original = self.archive.read_bytes()
        with self.assertRaises(FileExistsError):
            self.service.create(self.archive, PASSWORD)
        self.assertEqual(original, self.archive.read_bytes())

    def test_restore_refuses_database_with_open_wal_connection(self):
        database = self.config.data_dir / "fixture.session"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE fixture(value)")
            connection.execute("INSERT INTO fixture VALUES ('original')")
            connection.commit()
        self.service.create(self.archive, PASSWORD)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("INSERT INTO fixture VALUES ('current')")
            connection.commit()
            with self.assertRaisesRegex(RuntimeError, "still open"):
                self.service.restore(self.archive, PASSWORD)
            self.assertEqual(2, connection.execute("SELECT count(*) FROM fixture").fetchone()[0])
        self.service.restore(self.archive, PASSWORD)
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual([('original',)], connection.execute("SELECT value FROM fixture").fetchall())

    def test_incomplete_copy_never_replaces_destination(self):
        source = self.root / "new.txt"
        source.write_text("new content")
        original = self.accounts.read_bytes()
        def incomplete(_source, destination):
            Path(destination).write_text("partial")
            raise OSError("fixture write failure")
        with patch("modules.local_backup.shutil.copy2", side_effect=incomplete), self.assertRaises(OSError):
            self.service._replace_file(source, self.accounts)
        self.assertEqual(original, self.accounts.read_bytes())
        self.assertFalse(list(self.config.data_dir.glob(".uznik-restore-*.tmp")))


if __name__ == "__main__":
    unittest.main()
