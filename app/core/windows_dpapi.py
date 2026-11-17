"""Small CurrentUser-only DPAPI boundary. No fallback, prompts or machine scope."""
import ctypes
import sys


class DPAPIError(RuntimeError):
    pass


class CurrentUserDPAPI:
    name = 'windows-current-user-dpapi'

    def protect(self, plain, entropy):
        return self._call(plain, entropy, decrypt=False)

    def unprotect(self, cipher, entropy):
        return self._call(cipher, entropy, decrypt=True)

    @staticmethod
    def _call(value, entropy, *, decrypt):
        if sys.platform != 'win32':
            raise DPAPIError('Financial DPAPI protection requires Windows; no fallback')
        if (not isinstance(value, (bytes, bytearray)) or not 1 <= len(value) <= 4096
                or not isinstance(entropy, bytes) or not 1 <= len(entropy) <= 1024):
            raise DPAPIError('Invalid DPAPI input size/type')
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        value_copy = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        entropy_copy = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
        incoming, context, outgoing = Blob(len(value), value_copy), Blob(len(entropy), entropy_copy), Blob()
        try:
            # CRYPTPROTECT_UI_FORBIDDEN=1. LOCAL_MACHINE=4 is NEVER set.
            if not function(ctypes.byref(incoming), None, ctypes.byref(context), None, None, 1, ctypes.byref(outgoing)):
                raise DPAPIError('DPAPI unavailable, wrong Windows identity/context or damaged ciphertext')
            if not outgoing.data or not 1 <= outgoing.size <= 4096:
                raise DPAPIError('Unexpected DPAPI output')
            return ctypes.string_at(outgoing.data, outgoing.size)
        finally:
            ctypes.memset(value_copy, 0, len(value_copy))
            ctypes.memset(entropy_copy, 0, len(entropy_copy))
            if outgoing.data:
                ctypes.memset(outgoing.data, 0, outgoing.size)
                kernel.LocalFree(outgoing.data)
