import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Profile safety contracts; no real accounts, media, sessions or network."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from core.models import AccountRecord
from core.telegram_client import PyrogramAccountClient, TelethonAccountClient
from core.storage import write_json_atomic
from modules.accounts import AccountService
from modules.profile_apply import ProfileApplyJournal, apply_profile
from modules.profile_archive import ProfileArchive
from modules.profile_customizer import ProfileCustomizer
from modules.profile_bindings import ProfileBindings
from modules.profile_scraper import ProfileData, ProfileScraperService
from pyrogram import raw  # import SDK before isolated event loops


def account(key="one"):
    return AccountRecord(key, key, "pyrogram", "session_file", "not-a-real-session")


def source(**changes):
    values = dict(user_id=100, username="source", first_name="Own", last_name="Content", bio="fixture",
                  photo_count=0, stories_count=0, is_premium=False)
    values.update(changes)
    return ProfileData(**values)


def config(root):
    return SimpleNamespace(data_dir=root, groups_dir=root/"groups", sessions_dir=root/"sessions",
                           import_dir=root/"imports", accounts_file=root/"accounts.json",
                           min_action_delay=0, max_action_delay=0, max_concurrency=1)


def client():
    return SimpleNamespace(profile_capabilities=PyrogramAccountClient.profile_capabilities,
                           supports_profile_music=AsyncMock(return_value=True), supports_stories=AsyncMock(return_value=True),
                           update_profile=AsyncMock(), set_username=AsyncMock(), set_profile_photos=AsyncMock(),
                           clear_profile_photos=AsyncMock(), set_birthday=AsyncMock(), set_profile_music=AsyncMock(),
                           check_username_available=AsyncMock(return_value=True), upload_story=AsyncMock(return_value=101))


class ApplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = ProfileApplyJournal(self.root)
        self.client = client()
        delay = patch("modules.profile_apply.human_delay", AsyncMock())
        delay.start()
        self.addCleanup(delay.stop)

    async def test_unsupported_fails_before_clear_or_any_profile_change(self):
        self.client.profile_capabilities = TelethonAccountClient.profile_capabilities
        prepare = Mock()
        report = await apply_profile(self.client, account(), source(avatars=[b"own-photo"], stories=[{"bytes": b"own-story"}]),
                                     journal=self.journal, prepare=prepare, clear_first=True, copy_stories=True)
        self.assertFalse(report.ok)
        self.assertEqual("unsupported", report.steps["stories"])
        prepare.assert_not_called()
        self.client.clear_profile_photos.assert_not_awaited()
        self.client.update_profile.assert_not_awaited()

    async def test_invalid_media_or_birthday_validates_whole_plan_before_changes(self):
        for src, options in ((source(stories=[{"bytes": b""}]), {"copy_stories": True}),
                             (source(birthday={"day": 31, "month": 2}), {"copy_birthday": True})):
            with self.assertRaises(ValueError):
                await apply_profile(self.client, account(), src, journal=self.journal, **options)
        self.client.update_profile.assert_not_awaited()

    async def test_each_field_and_story_has_receipt_and_restart_skips_done(self):
        src = source(avatars=[b"own-photo"], birthday={"day": 9, "month": 6, "year": 1992},
                     music_meta={"id": 11, "access_hash": 12}, stories=[{"bytes": b"own-story"}])
        options = dict(clear_first=True, copy_stories=True, copy_music=True, copy_birthday=True)
        report = await apply_profile(self.client, account(), src, journal=self.journal, **options)
        self.assertTrue(report.ok)
        self.assertEqual(1, report.stories_uploaded)
        self.client.set_birthday.assert_awaited_once_with(9, 6, year=1992)
        first_random = self.client.upload_story.call_args.args[0]["random_id"]
        self.assertGreater(first_random, 0)
        self.client.update_profile.reset_mock()
        report2 = await apply_profile(self.client, account(), src, journal=ProfileApplyJournal(self.root), **options)
        self.assertTrue(report2.ok)
        self.client.update_profile.assert_not_awaited()
        self.client.upload_story.assert_awaited_once()
        receipts = self.journal.receipts("one", "source")
        self.assertEqual(101, next(r["story_id"] for r in receipts if r["step"].startswith("story:")))
        self.assertEqual(report.changes["applied_username"], report2.changes["applied_username"])

    async def test_lost_response_stops_later_steps_and_blocks_automatic_retry(self):
        self.client.upload_story.side_effect = TimeoutError("fixture")
        src = source(stories=[{"bytes": b"story-one"}, {"bytes": b"story-two"}])
        report = await apply_profile(self.client, account(), src, journal=self.journal, copy_stories=True)
        self.assertFalse(report.ok)
        self.assertEqual(0, report.stories_uploaded)
        self.client.upload_story.assert_awaited_once()
        again = await apply_profile(self.client, account(), src, journal=self.journal, copy_stories=True)
        self.assertFalse(again.ok)
        self.client.upload_story.assert_awaited_once()
        self.assertIn("automatic retry blocked", again.detail)

    async def test_definitive_rpc_rejection_can_resume_with_same_random_id(self):
        class RPCError(Exception):
            pass
        self.client.upload_story.side_effect = [RPCError("FLOOD_WAIT_30"), 102]
        src = source(stories=[{"bytes": b"story-one"}])
        report = await apply_profile(self.client, account(), src, journal=self.journal, copy_stories=True)
        self.assertFalse(report.ok)
        again = await apply_profile(self.client, account(), src, journal=self.journal, copy_stories=True)
        self.assertTrue(again.ok)
        self.assertEqual(self.client.upload_story.call_args_list[0].args[0]["random_id"],
                         self.client.upload_story.call_args_list[1].args[0]["random_id"])

    async def test_missing_publication_id_is_not_success(self):
        self.client.upload_story.return_value = None
        report = await apply_profile(self.client, account(), source(stories=[{"bytes": b"story"}]),
                                     journal=self.journal, copy_stories=True)
        self.assertFalse(report.ok)
        self.assertEqual(0, report.stories_uploaded)
        self.assertIn("uncertain", report.steps.values())

    async def test_identical_story_bytes_are_not_published_twice(self):
        report = await apply_profile(self.client, account(), source(stories=[{"bytes": b"same"}, {"bytes": b"same"}]),
                                     journal=self.journal, copy_stories=True)
        self.assertTrue(report.ok)
        self.assertEqual(1, report.stories_uploaded)
        self.client.upload_story.assert_awaited_once()

    async def test_partial_avatar_pack_does_not_reupload_confirmed_first_photo(self):
        class RPCError(Exception):
            pass
        self.client.set_profile_photos.side_effect = [None, RPCError("fixture rejected photo"), None]
        src = source(avatars=[b"first-own-photo", b"second-own-photo"])
        first = await apply_profile(self.client, account(), src, journal=self.journal, clear_first=True)
        self.assertFalse(first.ok)
        second = await apply_profile(self.client, account(), src, journal=self.journal, clear_first=True)
        self.assertTrue(second.ok)
        self.assertEqual(3, self.client.set_profile_photos.await_count)
        self.client.clear_profile_photos.assert_awaited_once()

    async def test_empty_plan_does_not_bind_or_mark_profiled(self):
        prepare = Mock()
        report = await apply_profile(self.client, account(), source(), journal=self.journal, prepare=prepare,
                                     copy_name=False, copy_bio=False, copy_username=False, copy_avatars=False)
        self.assertTrue(report.ok)
        prepare.assert_not_called()

    async def test_cancelled_inflight_step_stays_uncertain(self):
        entered = asyncio.Event()
        async def waiting(*args):
            entered.set()
            await asyncio.Event().wait()
        self.client.upload_story.side_effect = waiting
        src = source(stories=[{"bytes": b"story"}])
        task = asyncio.create_task(apply_profile(self.client, account(), src, journal=self.journal, copy_stories=True))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        report = await apply_profile(self.client, account(), src, journal=self.journal, copy_stories=True)
        self.assertFalse(report.ok)
        self.client.upload_story.assert_awaited_once()

    async def test_service_preserves_receipt_when_metadata_write_fails(self):
        cfg = config(self.root)
        write_json_atomic(cfg.accounts_file, {"accounts": [account().to_dict()]})
        service = ProfileScraperService(cfg)
        original = service.accounts.update_profile_metadata
        def update(key, values):
            if "last_profile_apply" in values:
                raise OSError("fixture disk failure after publication")
            original(key, values)
        src = source(stories=[{"bytes": b"story"}])
        with patch.object(service.accounts, "update_profile_metadata", side_effect=update):
            with self.assertRaises(OSError):
                await service._apply_source(self.client, account(), src, copy_stories=True)
        self.assertTrue(ProfileBindings(cfg).get("one"))
        report = await service._apply_source(self.client, account(), src, copy_stories=True)
        self.assertTrue(report.ok)
        self.client.upload_story.assert_awaited_once()
        self.assertEqual(1, service.accounts.get_account("one").metadata["stories_uploaded"])

    async def test_legacy_bound_profiles_block_story_guessing_without_connection(self):
        cfg = config(self.root)
        write_json_atomic(cfg.accounts_file, {"accounts": [account().to_dict()]})
        archive = ProfileArchive(self.root)
        archive.save("source", {"user_id": 100, "stories": [{"bytes": b"story"}]})
        ProfileBindings(cfg).bind("one", "source")
        service = ProfileScraperService(cfg)
        with patch("modules.profile_scraper.human_delay", AsyncMock()), patch("modules.profile_scraper.create_client") as connect:
            result = await service.apply_saved_profiles([account()])
        self.assertEqual(1, result.errors)
        self.assertIn("Legacy", result.details["one"])
        connect.assert_not_called()

    async def test_saved_profile_end_to_end_and_incremental_media_resume(self):
        cfg = config(self.root)
        write_json_atomic(cfg.accounts_file, {"accounts": [account().to_dict()]})
        archive = ProfileArchive(self.root)
        archive.save("source", {"user_id": 100, "first_name": "Own", "stories": [{"bytes": b"story-one"}]})
        service = ProfileScraperService(cfg)
        @asynccontextmanager
        async def connect(*args):
            yield self.client
        self.client.upload_story.side_effect = [101, 102]
        with patch("modules.profile_scraper.human_delay", AsyncMock()), patch("modules.profile_scraper.create_client", connect):
            first = await service.apply_saved_profiles([account()])
            self.assertEqual(1, first.ok)
            before = self.client.update_profile.await_count
            archive.save_stories("source", [{"bytes": b"story-two"}])
            second = await service.apply_saved_profiles([service.accounts.get_account("one")])
        self.assertEqual(1, second.ok)
        self.assertEqual(before, self.client.update_profile.await_count)
        self.assertEqual(2, self.client.upload_story.await_count)
        self.assertEqual(2, service.accounts.get_account("one").metadata["stories_uploaded"])
        self.assertTrue(service.accounts.get_account("one").has_profile())

    async def test_missing_bound_archive_never_assigns_an_unrelated_free_profile(self):
        cfg = config(self.root)
        write_json_atomic(cfg.accounts_file, {"accounts": [account().to_dict()]})
        ProfileArchive(self.root).save("unrelated", {"bio": "fixture"})
        ProfileBindings(cfg).bind("one", "missing")
        service = ProfileScraperService(cfg)
        with patch("modules.profile_scraper.human_delay", AsyncMock()), patch("modules.profile_scraper.create_client") as connect:
            result = await service.apply_saved_profiles([account()])
        self.assertEqual(1, result.errors)
        self.assertEqual("missing", ProfileBindings(cfg).get("one")["profile_key"])
        connect.assert_not_called()

    async def test_require_stories_rejects_missing_files_before_any_mutation(self):
        cfg = config(self.root)
        write_json_atomic(cfg.accounts_file, {"accounts": [account().to_dict()]})
        ProfileArchive(self.root).save("source", {"stories": [{"bytes": b"story"}]})
        (self.root/"profiles_archive"/"source"/"story_0.jpg").unlink()
        service = ProfileScraperService(cfg)
        with patch("modules.profile_scraper.human_delay", AsyncMock()), patch("modules.profile_scraper.create_client") as connect:
            result = await service.apply_saved_profiles([account()], require_stories=True)
        self.assertEqual(1, result.errors)
        connect.assert_not_called()

    async def test_clear_releases_binding_only_after_successful_account(self):
        cfg = config(self.root)
        write_json_atomic(cfg.accounts_file, {"accounts": [account().to_dict()]})
        bindings = ProfileBindings(cfg)
        bindings.bind("one", "source")
        service = ProfileCustomizer(cfg)
        @asynccontextmanager
        async def connect(*args):
            yield self.client
        with patch("modules.profile_customizer.human_delay", AsyncMock()), patch("modules.profile_customizer.create_client", connect):
            self.client.clear_profile_photos.side_effect = RuntimeError("fixture")
            failed = await service.clear_full_profile([account()])
            self.assertEqual(1, failed.errors)
            self.assertIsNotNone(bindings.get("one"))
            self.client.clear_profile_photos.side_effect = None
            success = await service.clear_full_profile([account()])
        self.assertEqual(1, success.ok)
        self.assertIsNone(bindings.get("one"))


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_username_checks_do_not_mask_flood_or_network_errors(self):
        class UsernameOccupiedError(Exception):
            pass
        for cls in (PyrogramAccountClient, TelethonAccountClient):
            item = cls.__new__(cls)
            item._with_flood_wait = AsyncMock(side_effect=RuntimeError("FLOOD_WAIT_60"))
            with self.assertRaises(RuntimeError):
                await item.check_username_available("fixture")
            item._with_flood_wait.side_effect = UsernameOccupiedError("Already taken")
            self.assertFalse(await item.check_username_available("fixture"))
    async def test_telethon_unsupported_methods_and_birthday_fail_loudly(self):
        item = TelethonAccountClient.__new__(TelethonAccountClient)
        with self.assertRaises(NotImplementedError):
            await item.upload_story({"bytes": b"fixture"})
        with self.assertRaises(NotImplementedError):
            await item.set_profile_music({"id": 1})
        item._with_flood_wait = AsyncMock(side_effect=RuntimeError("fixture birthday failure"))
        with self.assertRaises(RuntimeError):
            await item.set_birthday(9, 6, 1992)

    async def test_story_id_public_pinned_and_random_id_from_raw_response(self):
        item = PyrogramAccountClient.__new__(PyrogramAccountClient)
        item.client = SimpleNamespace(save_file=AsyncMock(return_value=raw.types.InputFile(
            id=1, parts=1, name="story.jpg", md5_checksum="")),
            resolve_peer=AsyncMock(return_value=raw.types.InputPeerUser(user_id=42, access_hash=9)))
        story = raw.types.StoryItem(id=101, date=1, expire_date=2, media=raw.types.MessageMediaEmpty())
        response = SimpleNamespace(updates=[raw.types.UpdateStory(peer=raw.types.PeerUser(user_id=42), story=story)])
        item.client.invoke = AsyncMock(return_value=response)
        async def retry(factory):
            await factory()
            return await factory()
        item._with_flood_wait = retry
        self.assertEqual(101, await item.upload_story({"bytes": b"fixture", "random_id": 1234}))
        requests = [call.args[0] for call in item.client.invoke.call_args_list]
        self.assertEqual([1234, 1234], [r.random_id for r in requests])
        self.assertTrue(all(r.pinned for r in requests))
        self.assertTrue(all(isinstance(r.privacy_rules[0], raw.types.InputPrivacyValueAllowAll) for r in requests))
        item.client.invoke.return_value = SimpleNamespace(updates=[])
        with self.assertRaises(RuntimeError):
            await item.upload_story({"bytes": b"fixture"})
        with self.assertRaises(ValueError):
            await item.upload_story({"bytes": b"fixture", "random_id": 0})


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = config(self.root)

    def test_delete_releases_only_target_binding_and_keeps_archive(self):
        write_json_atomic(self.config.accounts_file, {"accounts": [account().to_dict(), account("two").to_dict()]})
        bindings = ProfileBindings(self.config)
        bindings.bind("one", "first")
        bindings.bind("two", "second")
        archive = ProfileArchive(self.root)
        archive.save("first", {"bio": "own"})
        self.assertEqual(1, AccountService(self.config).delete_accounts(["one"], delete_sessions=False))
        self.assertIsNone(bindings.get("one"))
        self.assertIsNotNone(bindings.get("two"))
        self.assertTrue(archive.has("first"))

    def test_profile_claim_cannot_silently_steal_owner(self):
        bindings = ProfileBindings(self.config)
        bindings.bind("one", " @Source ")
        with self.assertRaises(ValueError):
            bindings.bind("two", "source", replace_owner=False)
        self.assertEqual("one", bindings.owner_of("source"))

    def test_deduplication_releases_only_removed_account(self):
        first = replace(account(), user_id=42, created_at="2026-01-01T00:00:00+00:00")
        second = replace(account("two"), user_id=42, created_at="2026-01-02T00:00:00+00:00")
        write_json_atomic(self.config.accounts_file, {"accounts": [first.to_dict(), second.to_dict()]})
        bindings = ProfileBindings(self.config)
        bindings.bind("one", "first")
        bindings.bind("two", "second")
        self.assertEqual(1, AccountService(self.config).deduplicate_accounts(delete_sessions=False))
        self.assertIsNotNone(bindings.get("one"))
        self.assertIsNone(bindings.get("two"))

    def test_generation_does_not_regenerate_device(self):
        from ui.qt_app import QtDesktopApp
        window = Mock()
        window.group_accounts.return_value = [account()]
        window.config.data_dir = self.root
        window.active_group.return_value = "inbox"
        with patch("ui.qt_app.write_profile_plan", return_value=[]), patch("ui.qt_app.export_plan_text_files"), \
                patch("modules.fingerprint_generator.FingerprintGenerator") as fingerprints:
            QtDesktopApp.generate_profiles(window)
        fingerprints.assert_not_called()


if __name__ == "__main__":
    unittest.main()
