"""Offline installation check: imports, crypto, blank browser and native tools.

Does not load .env, open account databases or connect to Telegram.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

# Keep this in sync with the application imports and config/requirements.txt.
REQUIRED_MODULES = (
    "pyrogram", "tgcrypto", "telethon", "PySide6.QtWidgets", "dotenv",
    "httpx", "socks", "socksio", "gdown", "psutil", "playwright.sync_api",
    "PIL", "numpy", "zxingcpp", "ddddocr", "onnxruntime", "cv2",
    "cbor2", "cryptography.hazmat.primitives.asymmetric.ec",
)


def installed_version(name: str) -> str | None:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def namespace_errors() -> list[str]:
    errors = []
    if not ((3, 12) <= sys.version_info[:2] <= (3, 14)
            and sys.maxsize > 2**32 and sys.implementation.name == "cpython"
            and not sysconfig.get_config_var("Py_GIL_DISABLED")):
        errors.append("Use standard (not free-threaded) CPython 3.12-3.14 x64 for this environment.")
    # Stale distribution metadata is unsafe: later uninstalls can delete the
    # active fork's files. Never silently uninstall user's packages.
    for name in ("Pyrogram", "Pyrofork", "TgCrypto"):
        if installed_version(name):
            errors.append(f"Conflicting package {name}: remove it from this project's .venv, then rerun setup.bat. "
                          "The supported providers are Kurigram and TgCrypto-pyrofork.")
    return errors


def find_native_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if os.name != "nt":
        return None
    local = os.getenv("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "Microsoft/WinGet/Links" / f"{name}.exe"
        if candidate.is_file():
            return str(candidate)
    if name == "tesseract":
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.getenv(variable)
            if not base:
                continue
            for relative in ("Tesseract-OCR/tesseract.exe", "Programs/Tesseract-OCR/tesseract.exe"):
                candidate = Path(base) / relative
                if candidate.is_file():
                    return str(candidate)
    return None


def check_crypto() -> None:
    import tgcrypto
    from pyrogram.crypto import aes
    data, key, iv = bytes(range(64)), bytes(range(32)), bytes(range(32))
    if tgcrypto.ige256_decrypt(tgcrypto.ige256_encrypt(data, key, iv), key, iv) != data:
        raise RuntimeError("TgCrypto AES round-trip failed")
    if getattr(aes, "tgcrypto", None) is not tgcrypto:
        raise RuntimeError("Kurigram is not using TgCrypto acceleration")


def check_browser() -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, timeout=20_000)
        try:
            page = browser.new_page()
            page.set_content("<title>Uznik install check</title>")
            if page.title() != "Uznik install check":
                raise RuntimeError("Blank browser check failed")
        finally:
            browser.close()


def check_native_tool(name: str) -> bool:
    executable = find_native_tool(name)
    if not executable:
        return False
    argument = {"ffmpeg": "-version", "tesseract": "--version", "xray": "version"}[name]
    subprocess.run([executable, argument], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace-only", action="store_true", help="Pre-install interpreter / namespace conflict check")
    parser.add_argument("--require-native", action="store_true", help="Fail if FFmpeg, Tesseract or Xray are missing")
    args = parser.parse_args(argv)
    errors = list(namespace_errors())
    if args.namespace_only:
        for error in errors:
            print(f"ERROR: {error}")
        return int(bool(errors))

    for name in ("Kurigram", "TgCrypto-pyrofork"):
        version = installed_version(name)
        print(f"{name}: {version or 'MISSING'}")
        if not version:
            errors.append(f"Install {name} through config/requirements.txt.")
    for module in REQUIRED_MODULES:
        try:
            importlib.import_module(module)
        except Exception as exc:
            errors.append(f"Cannot import {module}: {type(exc).__name__}: {exc}")
    for label, check in (("MTProto acceleration", check_crypto), ("Chromium", check_browser)):
        try:
            check()
            print(f"{label}: OK")
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
    for name in ("ffmpeg", "tesseract", "xray"):
        try:
            present = check_native_tool(name)
        except (OSError, subprocess.SubprocessError):
            present = False
        print(f"{name}: {'OK' if present else 'MISSING / NOT WORKING'}")
        if not present and args.require_native:
            errors.append(f"Install {name} or fix PATH; see docs/INSTALLATION.md.")
    for error in errors:
        print(f"ERROR: {error}")
    print("No account data loaded or network requests made.")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
