"""Stable Windows taskbar identity shared by the application and its shortcuts."""
from __future__ import annotations

import ctypes
import logging
import os
import uuid
from pathlib import Path

APP_ID = "Uznik.MultiTool.Desktop"


def initialize_desktop_identity() -> None:
    if os.name != "nt":
        return
    setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    setter.argtypes, setter.restype = [ctypes.c_wchar_p], ctypes.c_long
    result = setter(APP_ID)
    if result:
        logging.getLogger("desktop").warning("Could not set Windows taskbar identity: %s", result)


def set_shortcut_app_id(path: str) -> None:
    """Write PKEY_AppUserModel_ID through Windows' IPropertyStore."""
    if os.name != "nt":
        return
    target = Path(path).resolve(strict=True)

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_ubyte * 16)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]

    class VALUE(ctypes.Union):
        _fields_ = [("text", ctypes.c_wchar_p), ("reserved", ctypes.c_ubyte * 16)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_uint16), ("r1", ctypes.c_uint16),
                    ("r2", ctypes.c_uint16), ("r3", ctypes.c_uint16), ("value", VALUE)]

    def guid(text):
        return GUID.from_buffer_copy(uuid.UUID(text).bytes_le)

    ole = ctypes.OleDLL("ole32")
    shell = ctypes.OleDLL("shell32")
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = []
    ole.CoUninitialize.restype = None
    ole.PropVariantClear.argtypes = [ctypes.c_void_p]
    ole.PropVariantClear.restype = ctypes.c_long
    shell.SHGetPropertyStoreFromParsingName.argtypes = [
        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetPropertyStoreFromParsingName.restype = ctypes.c_long

    # S_OK (0) and S_FALSE (1) increment the COM apartment refcount and require
    # CoUninitialize(); RPC_E_CHANGED_MODE (-2147417850) means COM is already initialized
    # on this thread in another mode and must NOT be balanced with CoUninitialize().
    co_hr = ole.CoInitializeEx(None, 2)
    need_uninit = co_hr in (0, 1)
    if co_hr < 0 and co_hr != -2147417850:
        raise ctypes.WinError(co_hr)

    store = ctypes.c_void_p()
    try:
        hr = shell.SHGetPropertyStoreFromParsingName(
            str(target), None, 2,
            ctypes.byref(guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")),
            ctypes.byref(store),
        )
        if hr < 0 or not store.value:
            raise OSError(f"Unable to open shortcut property store: {hr}")
        vtable = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(vtable[6])
        commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(vtable[7])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
        try:
            key = PROPERTYKEY(guid("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3"), 5)
            value = PROPVARIANT()
            value.vt, value.value.text = 31, APP_ID  # VT_LPWSTR, no allocation transferred.
            for operation in (lambda: set_value(store, ctypes.byref(key), ctypes.byref(value)), lambda: commit(store)):
                result = operation()
                if result < 0:
                    raise OSError(f"Unable to write shortcut AppUserModelID: {result}")
            # Verify the saved property before reporting shortcut creation as successful.
            get_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(vtable[5])
            saved = PROPVARIANT()
            if get_value(store, ctypes.byref(key), ctypes.byref(saved)) < 0:
                raise OSError("Unable to verify shortcut AppUserModelID")
            try:
                if saved.vt != 31 or saved.value.text != APP_ID:
                    raise OSError("Shortcut AppUserModelID did not match the application")
            finally:
                ole.PropVariantClear(ctypes.byref(saved))
        finally:
            release(store)
    finally:
        if need_uninit:
            ole.CoUninitialize()
