"""Source packaging must never pick up private files in a working directory."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.releases.build_source_release import build, validate_path


@unittest.skipUnless(shutil.which("git"), "Git is required for source releases")
class SourceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "--quiet")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Test fixture")
        (self.root / "README.md").write_text("committed source", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "--quiet", "-m", "fixture")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True).stdout

    def test_only_committed_content_and_deterministic_checksum(self):
        (self.root / ".env").write_text("fixture secret", encoding="utf-8")
        (self.root / "README.md").write_text("dirty working tree", encoding="utf-8")
        first = build(self.root, "HEAD", self.root / "one")
        second = build(self.root, "HEAD", self.root / "two")
        self.assertEqual(first, second)
        source = self.root / "one" / first["archive"]
        self.assertEqual(first["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        with zipfile.ZipFile(source) as archive:
            self.assertEqual(b"committed source", archive.read("uznik-multitool/README.md"))
            self.assertNotIn("uznik-multitool/.env", archive.namelist())
            self.assertEqual(first["commit"], json.loads(archive.read("uznik-multitool/BUILD_INFO.json"))["commit"])

    def test_tracked_private_input_refuses_release_before_output(self):
        folder = self.root / "imports/emails"
        folder.mkdir(parents=True)
        (folder / "accounts.txt").write_text("fixture secret", encoding="utf-8")
        self.git("add", "imports/emails/accounts.txt")
        self.git("commit", "--quiet", "-m", "private fixture")
        with self.assertRaisesRegex(ValueError, "Personal data"):
            build(self.root, "HEAD", self.root / "release")
        self.assertFalse((self.root / "release").exists())

    def test_scaffolding_and_template_are_allowed(self):
        for name in ("data/.gitkeep", "imports/emails/README.md", "config/.env.example", "sessions/create_session.bat"):
            validate_path(name)

    def test_private_paths_and_traversal_are_refused(self):
        for name in (".env", "source.session", "source.session-journal", "private.key", "private.uzbk",
                     "data/accounts.json", "sessions/batch_phones.txt", "templates/bios.txt",
                     "../outside.txt", "/outside.txt", "C:/outside.txt", "bad\\path"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_path(name)

    def test_symlinks_are_refused(self):
        # Git plumbing works on Windows without requiring symlink privileges.
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=self.root,
                              input=b"README.md", check=True, capture_output=True).stdout.decode().strip()
        self.git("update-index", "--add", "--cacheinfo", f"120000,{blob},link")
        self.git("commit", "--quiet", "-m", "symlink fixture")
        with self.assertRaisesRegex(ValueError, "Links"):
            build(self.root, "HEAD", self.root / "release")


if __name__ == "__main__":
    unittest.main()
