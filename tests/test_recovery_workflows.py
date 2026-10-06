"""Offline regressions for receipts, deletion, terminal task states and batches."""
from __future__ import annotations

import asyncio
import ast
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import asynccontextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from core.models import AccountRecord
from core.results import ActionResult
from core.storage import read_json, write_json_atomic
from core.task_runner import TaskRunner
from core.telegram_client import PyrogramAccountClient, TelethonAccountClient
from modules.accounts import AccountService
from modules.profile_bindings import ProfileBindings
from modules.story_publication import StoryPublicationStore, publish_stories
from modules.resumable_jobs import ResumableJobs
from modules.profile_archive import ProfileArchive
from modules.profile_scraper import ProfileScraperService
from modules.direct_access import DirectAccessService


def fixture(root):
    return SimpleNamespace(data_dir=root, accounts_file=root / "accounts.json", groups_dir=root / "groups", tasks_file=root / "tasks.json")


def account(key="fixture"):
    return AccountRecord(key, key, "pyrogram", "session_file", "unused.session")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = fixture(self.root)
        self.account = account()
        self.stories = [{"bytes": letter.encode(), "label": letter} for letter in "ABC"]
        self.store = StoryPublicationStore(self.root)

    async def test_receipts_handle_failure_reordering_and_restart(self):
        sent, attempts = [], []
        failed = False
        async def upload(story):
            nonlocal failed
            attempts.append((story["label"], story["random_id"]))
            if story["label"] == "B" and not failed:
                failed = True
                raise TimeoutError("lost reply")
            sent.append(story["label"])
            return 100 + len(sent)
        client = SimpleNamespace(supports_stories=AsyncMock(return_value=True), upload_story=upload)
        with patch("modules.story_publication.human_delay", new=AsyncMock()):
            with self.assertRaises(TimeoutError):
                await publish_stories(client, self.account, "source", self.stories, self.store)
            store = StoryPublicationStore(self.root)
            self.assertEqual(3, await publish_stories(client, self.account, "source", list(reversed(self.stories)), store))
            self.assertEqual(3, await publish_stories(client, self.account, "source", self.stories, store))
        self.assertEqual(["A", "C", "B"], sent)
        self.assertEqual(attempts[1][1], attempts[3][1])
        self.assertTrue(all(item["story_id"] > 0 for item in store.records(self.account.id, "source").values()))

    async def test_noop_does_not_count_as_upload(self):
        client = SimpleNamespace(supports_stories=AsyncMock(return_value=True), upload_story=AsyncMock(return_value=None))
        with self.assertRaisesRegex(RuntimeError, "publication ID"):
            await publish_stories(client, self.account, "source", self.stories, self.store)
        self.assertEqual("uncertain", next(iter(self.store.records(self.account.id, "source").values()))["status"])

    async def test_kurigram_returns_story_id_and_keeps_profile_privacy_flags(self):
        from pyrogram import raw
        for updates in ([SimpleNamespace(story=SimpleNamespace(id=77))],
                        [SimpleNamespace(random_id=123, id=77)],
                        [SimpleNamespace(id=999)]):
            client = PyrogramAccountClient.__new__(PyrogramAccountClient)
            sent = []
            async def invoke(request):
                sent.append(request)
                return SimpleNamespace(updates=updates)
            client.client = SimpleNamespace(invoke=invoke, resolve_peer=AsyncMock(return_value=raw.types.InputPeerSelf()))
            async def no_wait(factory):
                return await factory()
            client._with_flood_wait = no_wait
            if hasattr(updates[0], "random_id") or hasattr(updates[0], "story"):
                self.assertEqual(77, await client.upload_story({"bytes": b"fixture", "random_id": 123}))
            else:
                with self.assertRaisesRegex(RuntimeError, "no story ID"):
                    await client.upload_story({"bytes": b"fixture", "random_id": 123})
            request = sent[-1]
            self.assertIsInstance(request, raw.functions.stories.SendStory)
            self.assertEqual(123, request.random_id)
            self.assertTrue(request.pinned)
            self.assertIsInstance(request.privacy_rules[0], raw.types.InputPrivacyValueAllowAll)

    async def test_telethon_explicitly_rejects_stories_and_music(self):
        client = TelethonAccountClient.__new__(TelethonAccountClient)
        with self.assertRaises(NotImplementedError):
            await client.upload_story(self.stories[0])
        with self.assertRaises(NotImplementedError):
            await client.set_profile_music({"id": 123})
        with self.assertRaises(NotImplementedError):
            await publish_stories(client, self.account, "source", self.stories, self.store)
        self.assertFalse(self.store.path.exists())

    async def test_legacy_counter_requires_explicit_confirmation(self):
        client = SimpleNamespace(supports_stories=AsyncMock(return_value=True), upload_story=AsyncMock(return_value=5))
        with self.assertRaisesRegex(RuntimeError, "Legacy"):
            await publish_stories(client, self.account, "source", self.stories, self.store, legacy_count=2)
        self.assertFalse(self.store.path.exists())
        with patch("modules.story_publication.human_delay", new=AsyncMock()):
            self.assertEqual(3, await publish_stories(client, self.account, "source", self.stories, self.store, legacy_count=2, accept_legacy=True))
        self.assertEqual(1, client.upload_story.await_count)

    async def test_stop_preserves_failure_and_natural_completion(self):
        for fails in (True, False):
            runner = TaskRunner(self.config)
            entered = asyncio.Event()
            async def work(stop):
                entered.set()
                await stop.wait()
                if fails:
                    raise RuntimeError("failure during stop")
            task_id = runner.start("fixture", work)
            await entered.wait()
            if fails:
                with self.assertRaisesRegex(RuntimeError, "failure during stop"):
                    await runner.stop(task_id)
            else:
                await runner.stop(task_id)
            snapshot = next(item for item in runner.snapshots() if item.id == task_id)
            self.assertEqual("failed" if fails else "done", snapshot.status)
            if fails:
                self.assertEqual("failure during stop", snapshot.detail)

    async def test_previous_running_task_is_interrupted_on_restart(self):
        write_json_atomic(self.config.tasks_file, {"tasks": [{"id": "old", "name": "old", "status": "running"}]})
        runner = TaskRunner(self.config)
        self.assertEqual("interrupted", runner.snapshots()[0].status)

    async def test_resume_skips_success_and_does_not_expand_original_accounts(self):
        first, second = account("first"), account("second")
        write_json_atomic(self.config.accounts_file, {"accounts": [a.to_dict() for a in (first, second)]})
        jobs = ResumableJobs(self.config)
        job_id = jobs.create("check-sessions", [first, second])
        stop, seen = asyncio.Event(), []
        async def execute(name, item, options):
            seen.append(item.id)
            stop.set()
            result = ActionResult()
            result.add_ok(item.id)
            return result
        await jobs.run(job_id, stop, execute=execute)
        self.assertEqual(["second"], jobs.pending_ids(job_id))
        write_json_atomic(self.config.accounts_file, {"accounts": [a.to_dict() for a in (first, second, account("new"))]})
        stop.clear()
        await ResumableJobs(self.config).run(job_id, stop, execute=execute)
        self.assertEqual(["first", "second"], seen)
        self.assertEqual([], jobs.pending_ids(job_id))

    async def test_failed_and_cancelled_accounts_remain_pending(self):
        write_json_atomic(self.config.accounts_file, {"accounts": [self.account.to_dict()]})
        jobs = ResumableJobs(self.config)
        job_id = jobs.create("check-sessions", [self.account])
        async def cancelled(*args):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await jobs.run(job_id, asyncio.Event(), execute=cancelled)
        self.assertEqual([self.account.id], jobs.pending_ids(job_id))
        async def failed(*args):
            result = ActionResult()
            result.add_error(self.account.id, "network failure")
            return result
        outcome = await jobs.run(job_id, asyncio.Event(), execute=failed)
        self.assertEqual(1, outcome.errors)
        self.assertEqual("failed", jobs.get(job_id)["accounts"][self.account.id]["status"])

    async def test_partial_saved_profile_keeps_assignment_and_receipts(self):
        self.config.min_action_delay = self.config.max_action_delay = 0
        write_json_atomic(self.config.accounts_file, {"accounts": [self.account.to_dict()]})
        archive = ProfileArchive(self.root)
        archive.save("one", {"user_id": 1, "stories": self.stories})
        archive.save("two", {"user_id": 2, "stories": [{"bytes": b"other"}]})
        seen, failed = [], False
        async def upload(story):
            nonlocal failed
            label = story["bytes"].decode()
            seen.append(label)
            if label == "B" and not failed:
                failed = True
                raise RuntimeError("fixture Telegram failure")
            return len(seen)
        client = SimpleNamespace(supports_stories=AsyncMock(return_value=True), clear_profile_photos=AsyncMock(), upload_story=upload)
        @asynccontextmanager
        async def fake_client(*args):
            yield client
        options = dict(copy_name=False, copy_bio=False, copy_username=False, copy_avatars=False,
                       copy_music=False, copy_birthday=False, max_stories_per_account=None)
        with patch("modules.profile_scraper.create_client", fake_client), \
             patch("modules.profile_scraper.human_delay", new=AsyncMock()), \
             patch("modules.story_publication.human_delay", new=AsyncMock()):
            service = ProfileScraperService(self.config)
            first = await service.apply_saved_profiles([self.account], **options)
            self.assertEqual(1, first.errors)
            refreshed = service.accounts.get_account(self.account.id)
            self.assertTrue(refreshed.metadata["profile_apply_pending"])
            second = await service.apply_saved_profiles([refreshed], **options)
        self.assertEqual(1, second.ok)
        self.assertEqual("one", ProfileBindings(self.config).get(self.account.id)["profile_key"])
        self.assertIsNone(ProfileBindings(self.config).owner_of("two"))
        self.assertEqual(["A", "B", "B", "C"], seen)
        self.assertEqual(3, service.accounts.get_account(self.account.id).metadata["stories_uploaded"])

    async def test_account_removal_closes_browser_context_and_removes_cache(self):
        write_json_atomic(self.config.accounts_file, {"accounts": [self.account.to_dict()]})
        direct = DirectAccessService(self.config)
        direct._loop = asyncio.get_running_loop()
        cache = direct._profile_dir(self.account.id)
        (cache / "fixture.txt").write_text("fake browser state")
        context = SimpleNamespace(close=AsyncMock())
        direct._contexts[self.account.id] = context
        AccountService(self.config).delete_accounts([self.account.id], delete_sessions=False)
        for _ in range(20):
            if not cache.exists():
                break
            await asyncio.sleep(0.01)
        context.close.assert_awaited_once()
        self.assertFalse(cache.exists())
        with self.assertRaisesRegex(RuntimeError, "deleted"):
            await direct.open_account(self.account)

    def test_secret_or_unimplemented_job_is_rejected(self):
        jobs = ResumableJobs(self.config)
        for operation, options in (("change-login-email", {}), ("check-sessions", {"password": "secret"}),
                                   ("apply-saved-profiles", {"copy_name": "not-a-bool"})):
            with self.assertRaises(ValueError):
                jobs.create(operation, [self.account], options=options)
        self.assertFalse(jobs.path.exists())

    def test_account_removal_releases_profile_and_receipts_not_archive(self):
        write_json_atomic(self.config.accounts_file, {"accounts": [self.account.to_dict()]})
        bindings = ProfileBindings(self.config)
        bindings.bind(self.account.id, "source")
        self.store.record(self.account.id, "source", "hash", status="published", story_id=1)
        archive = self.root / "profiles_archive/source"
        archive.mkdir(parents=True)
        (archive / "meta.json").write_text("{}")
        service = AccountService(self.config)
        service.delete_accounts([self.account.id], delete_sessions=False)
        self.assertIsNone(bindings.owner_of("source"))
        self.assertEqual({}, read_json(self.store.path, {}))
        self.assertTrue((archive / "meta.json").exists())

    def test_generating_profile_does_not_regenerate_devices(self):
        tree = ast.parse((ROOT / "app/ui/qt_app.py").read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "generate_profiles")
        self.assertFalse(any(isinstance(node, ast.Attribute) and node.attr == "regenerate" for node in ast.walk(method)))


if __name__ == "__main__":
    unittest.main()
