from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.models import AccountRecord
from modules.account_filters import AccountFilter, filter_accounts
from modules.profile_archive import ProfileArchive
from modules.profile_bindings import ProfileBindings


class RetainedFeatureTests(unittest.TestCase):
    def test_account_filters_remain_scoped(self):
        def account(key, enabled=True):
            return AccountRecord(id=key, label=key, backend="pyrogram", session_kind="session_file",
                                 session_ref="fixture.session", enabled=enabled)
        accounts = [account("alpha"), account("beta", False)]
        self.assertEqual(["alpha"], [a.id for a in filter_accounts(accounts, AccountFilter(query="alpha"))])
        self.assertEqual(["alpha"], [a.id for a in filter_accounts(accounts, AccountFilter(enabled="enabled"))])

    def test_archive_preserves_and_deduplicates_story_media(self):
        with tempfile.TemporaryDirectory() as temp:
            archive = ProfileArchive(Path(temp))
            archive.save("example", {"user_id": 123, "first_name": "Fixture"})
            stories = [{"bytes": b"photo-fixture", "is_video": False},
                       {"bytes": b"video-fixture", "is_video": True}]
            self.assertEqual(2, archive.save_stories("example", stories))
            self.assertEqual(0, archive.save_stories("example", stories))
            archive.save("example", {"user_id": 123, "first_name": "Updated"})
            self.assertTrue(archive.has_stories("example"))
            self.assertEqual(2, len(archive.load("example")["stories"]))

    def test_profile_bindings_have_one_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            bindings = ProfileBindings(SimpleNamespace(data_dir=Path(temp)))
            bindings.bind("first", "@Example", user_id=123)
            bindings.bind("second", "example", user_id=123)
            self.assertIsNone(bindings.get("first"))
            self.assertEqual("second", bindings.owner_of("@EXAMPLE"))
            self.assertEqual({"example"}, bindings.profile_keys_in_use())


if __name__ == "__main__":
    unittest.main()
