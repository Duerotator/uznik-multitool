"""Installation contract checks; no real installers, accounts or requests."""
from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import check_install as checker


class RequirementsTests(unittest.TestCase):
    def requirements(self) -> dict[str, str]:
        lines = (ROOT / "config/requirements.txt").read_text(encoding="utf-8").splitlines()
        return {re.match(r"[A-Za-z0-9_.-]+", line).group().lower().replace("_", "-"): line
                for line in lines if line and not line.startswith(("#", "--"))}

    def test_application_external_imports_have_explicit_dependencies(self):
        providers = {
            "PIL": "pillow", "PySide6": "pyside6", "pyrogram": "kurigram",
            "telethon": "telethon", "dotenv": "python-dotenv", "socks": "pysocks",
            "zxingcpp": "zxing-cpp", "cv2": "opencv-python", "tgcrypto": "tgcrypto-pyrofork",
        }
        packages = self.requirements()
        for base in (ROOT / "app", ROOT / "scripts"):
            for file in base.rglob("*.py"):
                for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                        names = [node.module]
                    for name in names:
                        top = name.split(".")[0]
                        if top in sys.stdlib_module_names or top in {"core", "modules", "ui", "utils", "scripts"}:
                            continue
                        self.assertIn(providers.get(top, top.lower()), packages,
                                      f"Unlisted dependency {top} in {file.relative_to(ROOT)}")

    def test_only_supported_namespace_providers_are_installed(self):
        requirements = self.requirements()
        self.assertEqual("Kurigram==2.2.26", requirements["kurigram"])
        self.assertEqual("TgCrypto-pyrofork==1.2.8", requirements["tgcrypto-pyrofork"])
        for forbidden in ("pyrogram", "pyrofork", "tgcrypto", "solvecaptcha-python", "fastapi", "uvicorn", "python-telegram-bot"):
            self.assertNotIn(forbidden, requirements)
        self.assertIn("--only-binary=TgCrypto-pyrofork",
                      (ROOT / "config/requirements.txt").read_text(encoding="utf-8"))

    def test_ocr_cpu_dependencies_and_platform_specific_opencv(self):
        requirements = self.requirements()
        for required in ("ddddocr", "onnxruntime", "numpy", "pillow", "zxing-cpp"):
            self.assertIn(required, requirements)
        self.assertNotIn("onnxruntime-gpu", requirements)
        self.assertIn('sys_platform == "win32"', requirements["opencv-python"])
        self.assertIn('sys_platform == "linux"', requirements["opencv-python-headless"])

    def test_public_readme_links_resolve_and_omits_handoff_notes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("## Возможности", readme)
        for phrase in ("Comfortable", "исходного проекта", "Репозиторий пока приватный", "нет серверных файлов"):
            self.assertNotIn(phrase, readme)
        for link in re.findall(r"\]\(([^)]+)\)", readme):
            if not link.startswith(("https://", "http://", "#")):
                self.assertTrue((ROOT / link).is_file(), link)

    def test_all_readme_local_links_and_documented_tool_paths_exist(self):
        readmes = [ROOT / "README.md", *ROOT.glob("*/README.md"), *ROOT.glob("*/*/README.md")]
        self.assertGreaterEqual(len(readmes), 11)
        for path in readmes:
            content = path.read_text(encoding="utf-8")
            for link in re.findall(r"\]\(([^)]+)\)", content):
                if not link.startswith(("https://", "http://", "#")):
                    self.assertTrue((path.parent / link.split("#", 1)[0]).is_file(), f"{path}: {link}")
        tools = (ROOT / "scripts/README.md").read_text(encoding="utf-8")
        for command in re.findall(r"^\| ([\w./-]+\.(?:py|ps1)) \|", tools, re.M):
            self.assertTrue((ROOT / "scripts" / command).is_file(), command)


class InstallationCheckTests(unittest.TestCase):
    def test_namespace_check_is_safe_before_packages_are_installed(self):
        with patch.object(checker, "installed_version", return_value=None):
            self.assertEqual([], checker.namespace_errors())

    def test_namespace_check_rejects_overlapping_distributions(self):
        with patch.object(checker, "installed_version", side_effect=lambda name: "1" if name == "Pyrogram" else None):
            errors = checker.namespace_errors()
        self.assertEqual(1, len(errors))
        self.assertIn("Conflicting package Pyrogram", errors[0])

    def test_unsupported_python_is_rejected(self):
        with patch.object(checker.sys, "version_info", (3, 11, 0)), \
             patch.object(checker, "installed_version", return_value=None):
            self.assertIn("CPython 3.12-3.14 x64", checker.namespace_errors()[0])

    def test_free_threaded_python_is_rejected(self):
        with patch.object(checker.sysconfig, "get_config_var", return_value=1), \
             patch.object(checker, "installed_version", return_value=None):
            self.assertIn("not free-threaded", checker.namespace_errors()[0])

    def test_namespace_only_never_imports_packages_or_launches_tools(self):
        with patch.object(checker, "namespace_errors", return_value=[]), \
             patch.object(checker.importlib, "import_module") as imports, \
             patch.object(checker, "check_browser") as browser, \
             patch.object(checker, "check_native_tool") as native:
            self.assertEqual(0, checker.main(["--namespace-only"]))
        imports.assert_not_called()
        browser.assert_not_called()
        native.assert_not_called()

    def test_complete_install_passes_without_reading_configuration(self):
        with patch.object(checker, "namespace_errors", return_value=[]), \
             patch.object(checker, "installed_version", return_value="1"), \
             patch.object(checker.importlib, "import_module"), \
             patch.object(checker, "check_crypto"), patch.object(checker, "check_browser"), \
             patch.object(checker, "check_native_tool", return_value=True), \
             patch("builtins.open", side_effect=AssertionError("No user files may be opened")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, checker.main(["--require-native"]))

    def test_missing_native_tools_fail_only_in_full_mode(self):
        with patch.object(checker, "namespace_errors", return_value=[]), \
             patch.object(checker, "installed_version", return_value="1"), \
             patch.object(checker.importlib, "import_module"), \
             patch.object(checker, "check_crypto"), patch.object(checker, "check_browser"), \
             patch.object(checker, "check_native_tool", return_value=False), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(1, checker.main(["--require-native"]))
            self.assertEqual(0, checker.main([]))

    def test_broken_crypto_browser_import_and_tool_do_not_report_success(self):
        with patch.object(checker, "namespace_errors", return_value=[]), \
             patch.object(checker, "installed_version", return_value="1"), \
             patch.object(checker.importlib, "import_module", side_effect=ImportError("missing")), \
             patch.object(checker, "check_crypto", side_effect=RuntimeError("bad AES")), \
             patch.object(checker, "check_browser", side_effect=RuntimeError("missing Chromium")), \
             patch.object(checker, "check_native_tool", side_effect=OSError("bad exe")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(1, checker.main(["--require-native"]))
        self.assertIn("bad AES", output.getvalue())
        self.assertIn("missing Chromium", output.getvalue())

    def test_native_probe_is_bounded_and_only_requests_version(self):
        for tool, argument in (("ffmpeg", "-version"), ("tesseract", "--version"), ("xray", "version")):
            with patch.object(checker, "find_native_tool", return_value="fixture.exe"), \
                 patch.object(checker.subprocess, "run") as run:
                self.assertTrue(checker.check_native_tool(tool))
            self.assertEqual(["fixture.exe", argument], run.call_args.args[0])
            self.assertEqual(10, run.call_args.kwargs["timeout"])
            self.assertTrue(run.call_args.kwargs["check"])

    @unittest.skipUnless(os.name == "nt", "Windows-specific standard executable paths")
    def test_tesseract_is_found_without_path_in_standard_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "Tesseract-OCR/tesseract.exe"
            executable.parent.mkdir()
            executable.touch()
            with patch.object(checker.shutil, "which", return_value=None), \
                 patch.dict(os.environ, {"ProgramFiles": temp}, clear=True):
                self.assertEqual(str(executable), checker.find_native_tool("tesseract"))


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell required")
class WindowsInstallerTests(unittest.TestCase):
    def run_script(self, code: str):
        # Scripts are parsed/evaluated in memory with mocked native commands.
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", code],
                              cwd=ROOT, text=True, capture_output=True, timeout=30)

    def test_powershell_scripts_parse(self):
        result = self.run_script("$files = @('scripts/setup.ps1', 'scripts/windows/install_native_tools.ps1'); "
                                 "foreach ($file in $files) { $tokens=$null; $errors=$null; "
                                 "[void][System.Management.Automation.Language.Parser]::ParseFile("
                                 "(Join-Path (Get-Location) $file), [ref]$tokens, [ref]$errors); "
                                 "if ($errors.Count) { throw ($errors | Out-String) } }")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_native_installer_reuses_existing_tools_without_installing(self):
        result = self.run_script("$source=Get-Content -Raw scripts/windows/install_native_tools.ps1; "
                                 "$source=$source.Replace('Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue',"
                                 "'( [pscustomobject]@{ Source = ''fixture.exe'' } )'); "
                                 "Invoke-Expression $source")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(3, result.stdout.count("already installed"))

    def test_native_installer_uses_exact_ids_and_stops_on_failure(self):
        result = self.run_script("$source=Get-Content -Raw scripts/windows/install_native_tools.ps1; "
                                 "$source=$source.Replace('if (Find-NativeTool $tool.Name)', 'if ($false)'); "
                                 "$source=$source.Replace('$winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue',"
                                 "'$winget = [pscustomobject]@{ Source = ''Fake-Winget'' }'); "
                                 "function Fake-Winget { $global:LASTEXITCODE = 9 }; "
                                 "try { Invoke-Expression $source; throw 'Expected failure' } catch { "
                                 "if ($_.Exception.Message -notlike '*WinGet could not install Gyan.FFmpeg*') { throw }; "
                                 "Write-Output 'EXPECTED_FAILURE' }")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("EXPECTED_FAILURE", result.stdout)
        source = (ROOT / "scripts/windows/install_native_tools.ps1").read_text(encoding="utf-8")
        for marker in ("Gyan.FFmpeg", "UB-Mannheim.TesseractOCR", "XTLS.Xray-core", "--exact", "--source winget"):
            self.assertIn(marker, source)

    def test_native_installer_installs_all_missing_tools_and_rechecks(self):
        result = self.run_script("$source=Get-Content -Raw scripts/windows/install_native_tools.ps1; "
                                 "$source=$source.Replace('function Find-NativeTool {', 'function Original-Finder {'); "
                                 "$source=$source.Replace('$winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue',"
                                 "'$winget = [pscustomobject]@{ Source = ''Fake-Winget'' }'); "
                                 "$global:installed=@(); $global:calls=@(); "
                                 "function Find-NativeTool { param($Name) "
                                 "if ($global:installed -contains $Name) { return 'fixture.exe' }; return $null }; "
                                 "function Fake-Winget { $global:calls+=($args -join ' '); "
                                 "$global:installed+=switch ($args[2]) { 'Gyan.FFmpeg' {'ffmpeg'} "
                                 "'UB-Mannheim.TesseractOCR' {'tesseract'} 'XTLS.Xray-core' {'xray'} }; "
                                 "$global:LASTEXITCODE=0 }; Invoke-Expression $source; "
                                 "if ($global:calls.Count -ne 3 -or $global:installed.Count -ne 3) { throw 'Incomplete installation' }; "
                                 "Write-Output 'ALL_INSTALLED'")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("ALL_INSTALLED", result.stdout)

    def test_setup_orchestration_does_not_overwrite_existing_env(self):
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            python = project / ".venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            env = project / ".env"
            env.write_text("USER_SETTING=keep_me\n", encoding="utf-8")
            # Quote only a test-generated absolute path. Never use shell-built
            # commands to move/delete project files.
            literal = str(project).replace("'", "''")
            scripts_literal = str(ROOT / "scripts").replace("'", "''")
            result = self.run_script("$source=Get-Content -Raw scripts/setup.ps1; "
                                     "$source=$source.Replace('param([switch]$NoOpenConfig, [switch]$SkipNativeTools)', ''); "
                                     f"$source=$source.Replace('$projectRoot = Split-Path -Parent $PSScriptRoot', \"`$projectRoot = '{literal}'\"); "
                                     "$source=$source.Replace('function Invoke-Checked {', 'function Original-Invoke {'); "
                                     "$source=$source.Replace('& (Join-Path $PSScriptRoot ''create_shortcut.ps1'')', 'Write-Output ''FAKE_SHORTCUT'''); "
                                     f"$source=$source.Replace('$PSScriptRoot', \"'{scripts_literal}'\"); "
                                     "$SkipNativeTools=$true; $NoOpenConfig=$true; $global:calls=@(); "
                                     "function Invoke-Checked { param($Executable, [string[]]$Arguments) "
                                     "$global:calls+=($Arguments -join ' ') }; Invoke-Expression $source; "
                                     "if ($global:calls.Count -ne 7) { throw ('Unexpected steps: '+$global:calls.Count) }; "
                                     "Write-Output ($global:calls -join [Environment]::NewLine)")
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual("USER_SETTING=keep_me\n", env.read_text(encoding="utf-8"))
            self.assertIn("--namespace-only", result.stdout)
            self.assertIn("-m pip check", result.stdout)
            self.assertIn("-m playwright install chromium", result.stdout)
            self.assertIn("FAKE_SHORTCUT", result.stdout)

    def test_setup_runs_full_validation_and_preserves_env(self):
        source = (ROOT / "scripts/setup.ps1").read_text(encoding="utf-8")
        for marker in ("'check_install.py'", "'--namespace-only'", "'--require-native'",
                       "'check'", "'install', 'chromium'", "'windows\\install_native_tools.ps1'",
                       "if (-not (Test-Path -LiteralPath $envFile))", "[switch]$SkipNativeTools"):
            self.assertIn(marker, source)


if __name__ == "__main__":
    unittest.main()
