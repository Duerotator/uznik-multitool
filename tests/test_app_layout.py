"""Offline regression checks for the user-facing root and relocated entry points."""
from __future__ import annotations

import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))


class AppLayoutTests(unittest.TestCase):
    def test_config_reads_bom_and_resolves_paths_from_env_directory(self):
        from core.config import AppConfig
        previous_dir = Path.cwd()
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {
            "TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": "",
        }, clear=True):
            project = Path(temp) / "project"
            project.mkdir()
            env_file = project / ".env"
            env_file.write_text(
                "TELEGRAM_API_ID=123\nTELEGRAM_API_HASH=fixture\n"
                "TELEGRAM_DATA_DIR=state\nTELEGRAM_IMPORT_DIR=input\n"
                "TELEGRAM_BATCH_PHONES_FILE=custom/phones.txt\n",
                encoding="utf-8-sig",
            )
            try:
                os.chdir(temp)
                config = AppConfig.load(env_file)
                config.require_telegram_api()
                self.assertEqual(123, config.api_id)
                self.assertEqual("fixture", config.api_hash)
                self.assertEqual(project / "state", config.data_dir)
                self.assertEqual(project / "input", config.import_dir)
                self.assertEqual(project / "custom/phones.txt", config.batch_phones_file)
                self.assertTrue(config.batch_phones_file.is_file())
            finally:
                os.chdir(previous_dir)

    def test_missing_api_error_names_the_actual_configuration_file(self):
        from core.config import AppConfig
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            env_file = Path(temp) / ".env"
            config = AppConfig.load(env_file)
            with self.assertRaises(RuntimeError) as error:
                config.require_telegram_api()
            self.assertIn(str(env_file), str(error.exception))
            self.assertIn("TELEGRAM_API_ID", str(error.exception))
            self.assertIn("TELEGRAM_API_HASH", str(error.exception))

    def test_session_command_logs_unhandled_failures_with_traceback(self):
        from scripts.sessions.session_logging import run_session_command

        def failing_command():
            raise RuntimeError("fixture failure")

        with tempfile.TemporaryDirectory() as temp, patch("builtins.print"):
            root = Path(temp)
            self.assertEqual(1, run_session_command(root, failing_command, "Login failed"))
            log_text = (root / "data/logs/session_creation.log").read_text(encoding="utf-8")
            self.assertIn("Traceback", log_text)
            self.assertIn("failing_command", log_text)
            self.assertIn("fixture failure", log_text)

    def test_technical_files_are_nested_and_root_keeps_user_launchers(self):
        for name in ("main.py", "launch.pyw", "requirements.txt", ".env.example", "start_debug.bat", "create_shortcut.bat"):
            self.assertFalse((ROOT / name).exists(), name)
        for name in ("app/main.py", "app/launch.pyw", "config/requirements.txt", "config/.env.example",
                     "scripts/windows/start_debug.bat", "scripts/windows/create_shortcut.bat",
                     "start_gui.bat", "setup.bat", "sessions/create_sessions.bat",
                     "sessions/create_session.bat", "sessions/batch_create_sessions.bat", "toolbox.bat"):
            self.assertTrue((ROOT / name).is_file(), name)
        setup = (ROOT / "scripts/setup.ps1").read_text(encoding="utf-8")
        self.assertIn("config\\requirements.txt", setup)
        self.assertIn("config\\.env.example", setup)
        self.assertIn("if (-not (Test-Path -LiteralPath $envFile))", setup)

    def test_main_loads_user_env_from_project_root_not_app(self):
        namespace = runpy.run_path(str(ROOT / "app/main.py"), run_name="layout_test")
        entry = namespace["main"]
        previous_dir, previous_path = Path.cwd(), sys.path[:]
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            (project / "app").mkdir()
            entry.__globals__["__file__"] = str(project / "app/main.py")
            try:
                with patch("core.config.AppConfig.load", return_value=SimpleNamespace()) as load, \
                     patch("core.logging_setup.configure_logging"), \
                     patch("core.diagnostics.install_global_exception_handler"), \
                     patch("ui.qt_app.run_desktop_app") as desktop:
                    self.assertEqual(0, entry())
                    self.assertEqual(project, Path.cwd())
                    load.assert_called_once_with(project / ".env")
                    desktop.assert_called_once()
            finally:
                os.chdir(previous_dir)
                sys.path[:] = previous_path

    def test_launcher_uses_nested_main_and_project_working_directory(self):
        namespace = runpy.run_path(str(ROOT / "app/launch.pyw"), run_name="launcher_test")
        entry = namespace["main"]
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            executable = project / ".venv/Scripts/pythonw.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            entry.__globals__["__file__"] = str(project / "app/launch.pyw")
            with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(0, entry())
            args, kwargs = run.call_args
            self.assertEqual([str(executable), str(project / "app/main.py")], args[0])
            self.assertEqual(project, kwargs["cwd"])
            self.assertTrue((project / "data/logs/launcher.log").is_file())

    def test_session_menu_opens_user_input_paths_without_telegram_calls(self):
        from scripts.sessions.menu import main
        previous_dir = Path.cwd()
        with tempfile.TemporaryDirectory() as temp:
            imports = Path(temp) / "imports"
            config = SimpleNamespace(import_dir=imports, batch_phones_file=Path(temp) / "sessions/batch_phones.txt")
            try:
                with patch("core.config.AppConfig.load", return_value=config), \
                     patch("builtins.input", side_effect=["4", "5", "6", "0"]), \
                     patch("builtins.print"), patch("os.startfile", create=True) as open_file, \
                     patch("subprocess.run") as run:
                    main()
                self.assertEqual([str(imports / "auth_input"), str(Path(temp) / "sessions"),
                                  str(Path(temp) / "sessions" / "batch_phones.txt")],
                                 [call.args[0] for call in open_file.call_args_list])
                run.assert_not_called()
            finally:
                os.chdir(previous_dir)


if __name__ == "__main__":
    unittest.main()
