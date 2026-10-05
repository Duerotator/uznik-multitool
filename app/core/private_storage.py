"""Authenticated local JSON encryption; Windows keys are protected by user DPAPI."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from core.storage import write_json_atomic


FORMAT = "uznik-private-json-v1"


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(target.data)


def _key(path: Path, *, create: bool) -> bytes:
    key_path = path.with_name(path.name + ".key")
    if create and not key_path.exists():
        secret = Fernet.generate_key()
        protected = b"DPAPI1\0" + _dpapi(secret) if os.name == "nt" else b"FERNET1\0" + secret
        try:
            descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(protected)
    try:
        protected = key_path.read_bytes()
    except FileNotFoundError:
        raise ValueError("Profile plan key is missing; restore it together with the plan.") from None
    if protected.startswith(b"DPAPI1\0"):
        if os.name != "nt":
            raise ValueError("This profile plan requires its original Windows user.")
        return _dpapi(protected[7:], decrypt=True)
    if protected.startswith(b"FERNET1\0") and os.name != "nt":
        return protected[8:]
    raise ValueError("Unsupported profile plan key format.")


def write_private_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    token = Fernet(_key(path, create=True)).encrypt(json.dumps(payload, ensure_ascii=False).encode())
    write_json_atomic(path, {"format": FORMAT, "ciphertext": token.decode("ascii")})


def read_private_json(path: Path) -> tuple[Any, bool]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        return raw, False
    try:
        payload = Fernet(_key(path, create=False)).decrypt(raw["ciphertext"].encode("ascii"))
        return json.loads(payload), True
    except (InvalidToken, KeyError, UnicodeError):
        raise ValueError("Profile plan is damaged or cannot be decrypted with this key.") from None
