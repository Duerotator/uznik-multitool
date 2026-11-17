"""Authenticated local JSON encryption; Windows keys are protected by user DPAPI."""
from __future__ import annotations

import ctypes
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from core.storage import write_json_atomic


FORMAT = "uznik-private-json-v1"
_DPAPI_ENTROPY = b"uznik-private-json-v1"
_KEY_LOCK = threading.Lock()


def _zero_buffer(buf: ctypes.Array | bytearray) -> None:
    if isinstance(buf, bytearray):
        for i in range(len(buf)):
            buf[i] = 0
    else:
        ctypes.memset(buf, 0, ctypes.sizeof(buf))


def _dpapi(data: bytes, *, decrypt: bool = False, entropy: bytes | None = None) -> bytes:
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buffer)
    entropy_buf = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy) if entropy else None
    entropy_blob = Blob(len(entropy), entropy_buf) if entropy_buf is not None else None
    target = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    try:
        if not function(
            ctypes.byref(source),
            None,
            ctypes.byref(entropy_blob) if entropy_blob is not None else None,
            None,
            None,
            1,
            ctypes.byref(target),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(target.data, target.size)
    finally:
        _zero_buffer(buffer)
        if entropy_buf is not None:
            _zero_buffer(entropy_buf)
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel.LocalFree(target.data)


def _key(path: Path, *, create: bool) -> bytes:
    key_path = path.with_name(path.name + ".key")
    if create:
        with _KEY_LOCK:
            if not key_path.exists():
                secret = bytearray(Fernet.generate_key())
                try:
                    protected = (
                        b"DPAPI1\0" + _dpapi(bytes(secret), entropy=_DPAPI_ENTROPY)
                        if os.name == "nt"
                        else b"FERNET1\0" + bytes(secret)
                    )
                finally:
                    _zero_buffer(secret)
                tmp_path = key_path.with_name(
                    f".{key_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
                )
                try:
                    descriptor = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(protected)
                        stream.flush()
                    if not key_path.exists():
                        os.replace(tmp_path, key_path)
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
    try:
        protected = key_path.read_bytes()
    except FileNotFoundError:
        raise ValueError("Profile plan key is missing; restore it together with the plan.") from None
    if protected.startswith(b"DPAPI1\0"):
        if os.name != "nt":
            raise ValueError("This profile plan requires its original Windows user.")
        blob = protected[7:]
        try:
            return _dpapi(blob, decrypt=True, entropy=_DPAPI_ENTROPY)
        except OSError:
            try:
                return _dpapi(blob, decrypt=True, entropy=None)
            except OSError:
                raise ValueError("Profile plan is damaged or cannot be decrypted with this key.") from None
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
