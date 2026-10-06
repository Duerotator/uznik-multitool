"""Password-encrypted local backups with verified, staged and reversible restore."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from contextlib import closing

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.exceptions import InvalidTag


MAGIC = b"UZBK1"
CHUNK = 1024 * 1024
MAX_BYTES = 8 * 1024**3
MAX_FILES = 50000
EXCLUDED_DATA = {"backups", "logs", "browser_profiles", "vpn_gateway"}


class RestoreRollbackError(RuntimeError):
    """Some files could not be rolled back; do not continue with mixed state."""


class LocalBackup:
    def __init__(self, config):
        self.config = config
        self.root = (config.env_file.parent if config.env_file else Path.cwd()).resolve()
        self.roots = {"data": Path(config.data_dir).resolve(), "imports": Path(config.import_dir).resolve(),
                      "templates": self.root / "templates", "phones": self.root / "sessions",
                      "avatars": self.root / "assets/avatar_packs"}

    @staticmethod
    def _key(password: str, salt: bytes) -> bytes:
        if len(password) < 10:
            raise ValueError("Backup password must contain at least 10 characters")
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600000, 32)

    def _files(self):
        for prefix, root in self.roots.items():
            if not root.is_dir():
                continue
            for folder, dirs, files in os.walk(root, followlinks=False):
                base = Path(folder)
                dirs[:] = [name for name in dirs if not (base / name).is_symlink()
                           and not (base / name).is_junction()
                           and not (prefix == "data" and base == root and name in EXCLUDED_DATA)]
                for name in files:
                    path = base / name
                    if path.is_symlink() or path.is_junction() or name.endswith(("-wal", "-shm", ".tmp")):
                        continue
                    if prefix == "phones" and name != "batch_phones.txt":
                        continue
                    if prefix == "data" and name == "tasks.json":
                        continue
                    yield prefix + "/" + path.relative_to(root).as_posix(), path
        env = self.config.env_file
        if env and env.is_file() and not env.is_symlink():
            yield "environment/.env", env

    def create(self, output: Path, password: str) -> Path:
        output = Path(output).resolve()
        salt, nonce = os.urandom(16), os.urandom(12)
        key = self._key(password, salt)
        if output.exists():
            raise FileExistsError("Backup already exists; choose a new file name")
        files = list(self._files())
        if len(files) > MAX_FILES or sum(path.stat().st_size for _, path in files) > MAX_BYTES:
            raise ValueError("Backup exceeds the supported 8 GiB / 50000 file limit")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="uznik-backup-") as temp:
            stage = Path(temp)
            archive = stage / "payload.zip"
            manifest = {"format": 1, "roots": {key: str(root) for key, root in self.roots.items()}, "files": {}}
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for index, (name, source) in enumerate(files):
                    if source == output:
                        continue
                    if name == "data/accounts.json":
                        # Capture a canonical reference while the source machine
                        # can still resolve Windows 8.3 aliases. On another PC a
                        # lexical prefix comparison cannot expand RUNNER~1.
                        data = json.loads(source.read_text(encoding="utf-8-sig"))
                        session_root = self.roots["data"] / "sessions"
                        for account in data.get("accounts", []):
                            ref = account.get("session_ref")
                            if not ref or not Path(ref).is_absolute():
                                continue
                            try:
                                relative = Path(ref).resolve().relative_to(session_root)
                            except ValueError:
                                continue  # External sessions are not managed here.
                            account["session_ref"] = str(session_root / relative)
                        snapshot = stage / "accounts-snapshot.json"
                        snapshot.write_text(json.dumps(data), encoding="utf-8")
                        source = snapshot
                    with source.open("rb") as stream:
                        is_sqlite = stream.read(16) == b"SQLite format 3\0"
                    if is_sqlite:
                        snapshot = stage / f"sqlite-{index}"
                        deadline = time.monotonic() + 30
                        def check_deadline(*_args):
                            if time.monotonic() > deadline:
                                raise TimeoutError("Database snapshot timed out; close other application instances")
                        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as src, closing(sqlite3.connect(snapshot)) as dst:
                            src.backup(dst, pages=256, progress=check_deadline)
                        source = snapshot
                    # Stream and hash the exact bytes written to the ZIP. This
                    # also avoids Windows ZipInfo.from_file separator quirks.
                    digest = hashlib.sha256()
                    with source.open("rb") as stream, bundle.open(name, "w") as entry:
                        for block in iter(lambda: stream.read(CHUNK), b""):
                            entry.write(block)
                            digest.update(block)
                    manifest["files"][name] = {"sha256": digest.hexdigest(), "size": bundle.getinfo(name).file_size}
                bundle.writestr("manifest.json", json.dumps(manifest).encode())
            header = MAGIC + salt + nonce
            encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(header)
            owned_output = False
            try:
                with output.open("xb") as target, archive.open("rb") as source:
                    owned_output = True
                    target.write(header)
                    for block in iter(lambda: source.read(CHUNK), b""):
                        target.write(encryptor.update(block))
                    target.write(encryptor.finalize())
                    target.write(encryptor.tag)
            except BaseException:
                if owned_output:
                    output.unlink(missing_ok=True)
                raise
        return output

    def _decrypt(self, source: Path, password: str, archive: Path) -> None:
        size = source.stat().st_size
        if size < 49 or size > MAX_BYTES + 64 * 1024**2:
            raise ValueError("Invalid or oversized backup")
        with source.open("rb") as stream:
            header = stream.read(33)
            if not header.startswith(MAGIC):
                raise ValueError("Not an Uznik backup")
            key = self._key(password, header[5:21])
            stream.seek(-16, 2)
            tag = stream.read(16)
            stream.seek(33)
            decryptor = Cipher(algorithms.AES(key), modes.GCM(header[21:33], tag)).decryptor()
            decryptor.authenticate_additional_data(header)
            remaining = size - 49
            try:
                with archive.open("wb") as target:
                    while remaining:
                        block = stream.read(min(CHUNK, remaining))
                        if not block:
                            raise ValueError("Truncated backup")
                        remaining -= len(block)
                        target.write(decryptor.update(block))
                    target.write(decryptor.finalize())
            except InvalidTag:
                raise ValueError("Wrong password or damaged backup; nothing restored") from None

    def _target(self, name: str) -> Path:
        path = PurePosixPath(name)
        if (path.is_absolute() or "\\" in name or ":" in name or "\x00" in name
                or any(not part or part in {".", ".."} or part.rstrip(" .") != part for part in name.split("/"))):
            raise ValueError("Unsafe backup path")
        if name == "environment/.env":
            target = Path(self.config.env_file or self.root / ".env")
            if target.is_symlink():
                raise ValueError("Environment destination is a link")
            return target
        if len(path.parts) < 2 or path.parts[0] not in self.roots:
            raise ValueError("Unexpected backup content")
        prefix, *parts = path.parts
        if prefix == "data" and (parts[0] in EXCLUDED_DATA or parts == ["tasks.json"]):
            raise ValueError("Runtime/cache content cannot be restored")
        if prefix == "phones" and parts != ["batch_phones.txt"]:
            raise ValueError("Executable session tools cannot be restored")
        reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
        if any(part.split(".")[0].upper() in reserved for part in parts):
            raise ValueError("Reserved Windows backup path")
        root = self.roots[prefix].resolve()
        target = root.joinpath(*parts)
        if not target.resolve().is_relative_to(root):
            raise ValueError("Backup path escapes through a link")
        return target

    def restore(self, source: Path, password: str) -> Path:
        source = Path(source).resolve()
        with tempfile.TemporaryDirectory(prefix="uznik-restore-") as temp:
            stage = Path(temp)
            archive = stage / "payload.zip"
            self._decrypt(source, password, archive)
            with zipfile.ZipFile(archive) as bundle:
                infos = bundle.infolist()
                names = [item.filename for item in infos]
                if len(infos) > MAX_FILES + 1 or len(set(name.casefold() for name in names)) != len(names):
                    raise ValueError("Duplicate or excessive backup entries")
                if sum(item.file_size for item in infos) > MAX_BYTES or bundle.getinfo("manifest.json").file_size > 16 * CHUNK:
                    raise ValueError("Oversized backup content")
                manifest = json.loads(bundle.read("manifest.json"))
                if manifest.get("format") != 1 or set(names) != {"manifest.json", *manifest["files"]}:
                    raise ValueError("Invalid backup manifest")
                files = []
                for index, (name, receipt) in enumerate(manifest["files"].items()):
                    target = self._target(name)
                    payload = stage / str(index)
                    digest = hashlib.sha256()
                    with bundle.open(name) as reader, payload.open("wb") as writer:
                        for block in iter(lambda: reader.read(CHUNK), b""):
                            digest.update(block)
                            writer.write(block)
                    if digest.hexdigest() != receipt["sha256"] or payload.stat().st_size != receipt["size"]:
                        raise ValueError("Backup file failed integrity verification")
                    if name == "data/accounts.json":
                        data = json.loads(payload.read_text(encoding="utf-8-sig"))
                        old_sessions = manifest["roots"]["data"].replace("\\", "/").rstrip("/") + "/sessions/"
                        for account in data.get("accounts", []):
                            ref = account.get("session_ref", "").replace("\\", "/")
                            if ref.startswith(old_sessions):
                                suffix = ref[len(old_sessions):]
                                destination = self._target("data/sessions/" + suffix)
                                account["session_ref"] = str(destination)
                            # Imported source paths belong to the original machine.
                            account.get("metadata", {}).pop("source_path", None)
                        payload.write_text(json.dumps(data), encoding="utf-8")
                    if name == "environment/.env":
                        # Retain the receiving installation's folder layout.
                        content = payload.read_text(encoding="utf-8-sig")
                        for key, value in (("TELEGRAM_DATA_DIR", self.config.data_dir),
                                           ("TELEGRAM_IMPORT_DIR", self.config.import_dir)):
                            replacement = f'{key}="{Path(value).as_posix()}"'
                            pattern = rf"(?m)^\s*{key}\s*=.*$"
                            content = re.sub(pattern, lambda _: replacement, content) if re.search(pattern, content) else content + "\n" + replacement + "\n"
                        payload.write_text(content, encoding="utf-8")
                    files.append((target, payload))
            # Authenticate and stage EVERYTHING before touching existing data.
            recovery = Path(self.config.data_dir) / "backups" / ("before-restore-" + os.urandom(8).hex() + ".uzbk")
            self.create(recovery, password)
            undo = stage / "undo"
            undo.mkdir()
            applied = []
            try:
                for index, (target, payload) in enumerate(files):
                    with payload.open("rb") as stream:
                        is_sqlite = stream.read(16) == b"SQLite format 3\0"
                    if is_sqlite and target.is_file():
                        self._prepare_database(target)
                    previous = undo / str(index)
                    existed = target.exists()
                    if existed:
                        shutil.copy2(target, previous)
                    applied.append((target, previous, existed))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._replace_file(payload, target)
            except BaseException as failure:
                rollback_failures = []
                for target, previous, existed in reversed(applied):
                    try:
                        if existed:
                            self._replace_file(previous, target)
                        else:
                            target.unlink(missing_ok=True)
                    except Exception as exc:
                        rollback_failures.append(exc)
                if rollback_failures:
                    raise RestoreRollbackError(
                        f"Restore rollback was incomplete. Close the application and recover from {recovery} after fixing the disk error."
                    ) from failure
                raise
        return recovery

    @staticmethod
    def _prepare_database(target: Path) -> None:
        """Checkpoint committed WAL before replacing a closed SQLite database."""
        with closing(sqlite3.connect(target, timeout=1)) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0]:
                raise RuntimeError("Database is busy; close other application instances before restoring")
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()
        # An open reader/writer can retain these files after our connection closes.
        # Never put a restored base file beneath another process's WAL.
        if any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            raise RuntimeError("Database is still open; close other application instances before restoring")

    @staticmethod
    def _replace_file(source: Path, target: Path) -> None:
        """Readers see the old or the complete new file, never a partial copy."""
        fd, name = tempfile.mkstemp(prefix=".uznik-restore-", suffix=".tmp", dir=target.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
