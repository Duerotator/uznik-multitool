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
    def test_technical_files_are_nested_and_root_keeps_user_launchers(self):
        for name in ("main.py", "launch.pyw", "requirements.txt", ".env.example", "start_debug.bat", "create_shortcut.bat"):
            self.assertFalse((ROOT / name).exists(), name)
        for name in ("app/main.py", "app/launch.pyw", "config/requirements.txt", "config/.env.example",
                     "scripts/windows/start_debug.bat", "scripts/windows/create_shortcut.bat",
                     "start_gui.bat", "setup.bat", "create_sessions.bat", "toolbox.bat"):
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
            config = SimpleNamespace(import_dir=imports)
            try:
                with patch("core.config.AppConfig.load", return_value=config), \
                     patch("builtins.input", side_effect=["4", "5", "0"]), \
                     patch("builtins.print"), patch("os.startfile", create=True) as open_file, \
                     patch("subprocess.run") as run:
                    main()
                self.assertEqual([str(imports / "auth_input"), str(imports / "batch_phones.txt")],
                                 [call.args[0] for call in open_file.call_args_list])
                run.assert_not_called()
            finally:
                os.chdir(previous_dir)


if __name__ == "__main__":
    unittest.main()
