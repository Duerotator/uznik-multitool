"""Offline device catalogue, persistence and authorization identity contracts."""
from __future__ import annotations

import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from core.storage import read_json, write_json_atomic
from modules.device_catalog import DEVICES, DEVICE_TYPES, PLATFORM_LANG_PACKS
from modules.fingerprint_generator import FingerprintGenerator, SessionFingerprint


class CatalogueTests(unittest.TestCase):
    def test_catalogue_is_broad_unique_and_filename_safe(self):
        self.assertGreaterEqual(len(DEVICES), 200)
        self.assertEqual(len(DEVICES), len({device.model for device in DEVICES}))
        self.assertEqual(set(DEVICE_TYPES), {device.device_type for device in DEVICES})
        self.assertEqual(set(PLATFORM_LANG_PACKS), {device.platform for device in DEVICES})
        for device in DEVICES:
            with self.subTest(model=device.model):
                self.assertTrue(device.systems)
                self.assertFalse(set(device.model) & set('<>:"/\\|?*'))

    def test_every_device_generates_its_own_os_and_language_pack(self):
        for device in DEVICES:
            with self.subTest(model=device.model), \
                 patch("modules.fingerprint_generator.random.choice", side_effect=[
                     device, ("ru", "en"), device.systems[-1],
                 ]), patch("modules.fingerprint_generator.version", return_value="2.2.26"):
                fp = FingerprintGenerator.generate_random(
                    device_type=device.device_type, platform=device.platform,
                )
                self.assertEqual(device.model, fp.device_model)
                self.assertIn(fp.system_version, device.systems)
                self.assertEqual(device.platform, fp.platform)
                self.assertEqual(PLATFORM_LANG_PACKS[device.platform], fp.lang_pack)
                self.assertEqual("Uznik MultiTool (Kurigram 2.2.26)", fp.app_version)
                self.assertEqual(("ru", "en"), (fp.lang_code, fp.system_lang_code))

    def test_os_family_and_device_type_are_compatible(self):
        prefixes = {"android": "Android ", "ios": ("iOS ", "iPadOS "),
                    "macos": "macOS ", "windows": "Windows ", "linux": "Ubuntu "}
        for device in DEVICES:
            for system in device.systems:
                with self.subTest(model=device.model, system=system):
                    self.assertTrue(system.startswith(prefixes[device.platform]))
                    if device.model.startswith("iPad"):
                        self.assertEqual("tablet", device.device_type)
                        self.assertTrue(system.startswith("iPadOS "))
                    if device.model.startswith("MacBook"):
                        self.assertEqual("laptop", device.device_type)
                        self.assertEqual("macos", device.platform)
                    if device.device_type == "phone":
                        self.assertIn(device.platform, ("android", "ios"))
                    if device.device_type in ("desktop", "laptop"):
                        self.assertNotIn(device.platform, ("android", "ios"))

    def test_filters_select_only_matching_devices(self):
        for device_type in DEVICE_TYPES:
            for platform in PLATFORM_LANG_PACKS:
                matching = [d for d in DEVICES if d.device_type == device_type and d.platform == platform]
                if not matching:
                    with self.assertRaises(ValueError):
                        FingerprintGenerator.generate_random(device_type=device_type, platform=platform)
                    continue
                fp = FingerprintGenerator.generate_random(device_type=device_type, platform=platform)
                self.assertIn(fp.device_model, {d.model for d in matching})
        for kwargs in ({"device_type": "fridge"}, {"platform": "unsupported"}):
            with self.assertRaises(ValueError):
                FingerprintGenerator.generate_random(**kwargs)

    def test_backend_version_is_not_an_official_mobile_build(self):
        with patch("modules.fingerprint_generator.version", return_value="1.42") as metadata:
            fp = FingerprintGenerator.generate_random(backend="telethon")
        metadata.assert_called_once_with("Telethon")
        self.assertEqual("Uznik MultiTool (Telethon 1.42)", fp.app_version)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "custom-data" / "fingerprints.json"
        self.generator = FingerprintGenerator(str(self.path))
        self.original = SessionFingerprint(
            "iPhone 15 Pro", "iOS 17.4", "11.1.1 (4928)", "ru", "en", "ios", "ios",
        ).to_dict()

    def test_stable_parameters_survive_restart(self):
        first = self.generator.get_params("phone")
        self.assertEqual(first, self.generator.get_params("phone"))
        self.assertEqual(first, FingerprintGenerator(str(self.path)).get_params("phone"))

    def test_creation_metadata_survives_new_account_id(self):
        account = SimpleNamespace(id="imported-id", backend="pyrogram", metadata={"fingerprint": self.original})
        with patch.object(self.generator, "generate_random") as generate:
            self.assertEqual(self.original, self.generator.params_for_account(account))
        generate.assert_not_called()
        self.assertEqual(self.original, FingerprintGenerator(str(self.path)).get_params(account.id))
        self.assertEqual(self.original, account.metadata["fingerprint"])

    def test_existing_account_id_descriptor_is_not_overwritten(self):
        write_json_atomic(self.path, {"existing": self.original})
        account = SimpleNamespace(id="existing", metadata={"fingerprint": {**self.original, "device_model": "Older Device"}})
        self.assertEqual(self.original, self.generator.params_for_account(account))

    def test_legacy_fields_and_version_are_preserved(self):
        legacy = {key: self.original[key] for key in ("device_model", "system_version", "app_version", "lang_code")}
        write_json_atomic(self.path, {"existing": legacy})
        result = self.generator.get_params("existing")
        for key, value in legacy.items():
            self.assertEqual(value, result[key])
        # Merely reading an old entry must not silently migrate the file.
        self.assertEqual({"existing": legacy}, read_json(self.path, {}))

    def test_two_stale_instances_preserve_each_others_records(self):
        other = FingerprintGenerator(str(self.path))
        first = self.generator.get_params("first")
        second = other.get_params("second")
        self.assertEqual({"first": first, "second": second}, read_json(self.path, {}))
        self.assertEqual(second, self.generator.get_params("second"))

    def test_simultaneous_creation_for_same_id_keeps_one_descriptor(self):
        other = FingerprintGenerator(str(self.path))
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda gen: gen.get_params("same"), (self.generator, other)))
        self.assertEqual(values[0], values[1])
        self.assertEqual(values[0], read_json(self.path, {})["same"])

    def test_explicit_regeneration_only_changes_selected_record(self):
        self.generator.get_params("selected")
        untouched = self.generator.get_params("untouched")
        with patch.object(self.generator, "generate_random", return_value=SessionFingerprint.from_dict(self.original)):
            self.assertEqual(self.original, self.generator.regenerate("selected"))
        self.assertEqual(untouched, read_json(self.path, {})["untouched"])

    def test_invalid_metadata_falls_back_to_account_backend(self):
        account = SimpleNamespace(id="new", backend="telethon", metadata={"fingerprint": {"device_model": ""}})
        with patch("modules.fingerprint_generator.version", return_value="1.42"):
            self.assertIn("Telethon", self.generator.params_for_account(account)["app_version"])

    def test_malformed_entries_do_not_crash_or_erase_other_entries(self):
        write_json_atomic(self.path, {"valid": self.original, "invalid": None})
        self.assertEqual(self.original, self.generator.get_params("valid"))
        self.generator.get_params("new")
        self.assertEqual(self.original, read_json(self.path, {})["valid"])


if __name__ == "__main__":
    unittest.main()
