"""Additive, narrow Wallet TL compatibility for installed older clients.

Schema: telegramdesktop/tdesktop a7f453612f4896ac04d0007a36ab0d377e5bcbcf.
Only metadata RPCs. No sendTransfer, secret export, backup or wallet replacement.
Never override an existing library constructor or change the negotiated layer.
"""
from functools import lru_cache
import struct
from types import SimpleNamespace

STATE_EMPTY = 0x9cb9b2ec
STATE = 0x95c5346b
USER_ADDRESS = 0xfe78eecc
USER_ADDRESSES = 0x928e7b55
UPDATE_STATE = 0x6ac3f2aa
UPDATE_GASLESS = 0xa8ae1cac
VECTOR = 0x1cb5c415
METHODS = {'state': 0xab83b33a, 'addresses': 0x5275dfdd, 'gasless': 0x742df0a1}


class WalletWireError(ValueError):
    pass


class Reader:
    def __init__(self, stream, backend):
        self.stream, self.backend = stream, backend

    def read(self, count):
        value = self.stream.read(count)
        if len(value) != count:
            raise WalletWireError('Truncated wallet metadata')
        return value

    def int(self, *, signed=False):
        return int.from_bytes(self.read(4), 'little', signed=signed)

    def long(self):
        return int.from_bytes(self.read(8), 'little', signed=True)

    def flags(self, allowed):
        value = self.int()
        if value & ~allowed:
            raise WalletWireError('Unsupported wallet flags')
        return value

    def data(self, *, max_size=1024):
        size, prefix = self.read(1)[0], 1
        if size == 254:
            size, prefix = int.from_bytes(self.read(3), 'little'), 4
        elif size == 255:
            raise WalletWireError('Invalid TL bytes prefix')
        if size > max_size:
            raise WalletWireError('Wallet metadata exceeds limit')
        value = self.read(size)
        if any(self.read(-(size + prefix) % 4)):
            raise WalletWireError('Invalid TL padding')
        return value

    def text(self):
        return self.data().decode('utf-8', errors='strict')

    def obj(self):
        if self.backend == 'pyrogram':
            from pyrogram.raw.core import TLObject
            return TLObject.read(self.stream)
        return self.stream.tgread_object()

    def vector(self):
        if self.int() != VECTOR:
            raise WalletWireError('Invalid wallet vector')
        size = self.int(signed=True)
        if not 0 <= size <= 100:
            raise WalletWireError('Wallet vector exceeds limit')
        return [self.obj() for _ in range(size)]


def _decode(ident, stream, backend):
    r = Reader(stream, backend)
    fields = {}
    if ident == STATE_EMPTY:
        fields['creating'] = bool(r.flags(1) & 1)
    elif ident == STATE:
        flags = r.flags(7)
        fields.update(backup_enabled=bool(flags & 1), can_export_phrase=bool(flags & 2),
                      can_enable_backup=bool(flags & 4), address=r.text(), public_key=r.data(max_size=32), balance=r.long())
    elif ident == USER_ADDRESS:
        flags = r.flags(1)
        fields.update(user_id=r.long() if flags & 1 else None, address=r.text(), public_key=r.data(max_size=32))
    elif ident == USER_ADDRESSES:
        fields.update(addresses=r.vector(), users=r.vector())
    elif ident == UPDATE_STATE:
        fields['state'] = r.obj()
    elif ident == UPDATE_GASLESS:
        flags = r.flags(1)
        fields.update(available=bool(flags & 1), left=r.int(signed=True), reset_at=r.int(signed=True),
                      min_amount=r.long(), relayer_address=r.text())
    else:
        raise WalletWireError('Unknown wallet constructor')
    # These are public keys only; no secret-export constructor is registered.
    return SimpleNamespace(ID=ident, CONSTRUCTOR_ID=ident, **fields)


def encode_request(method, *, user_id=None, access_hash=None, force=False):
    if method not in METHODS:
        raise WalletWireError('Wallet RPC not allowed')
    body = struct.pack('<I', METHODS[method])
    if method != 'addresses':
        if user_id is not None or access_hash is not None or force is not False:
            raise WalletWireError('Unexpected wallet parameters')
        return body
    if type(force) is not bool or type(user_id) is not int or not 0 < user_id < 2**63:
        raise WalletWireError('Choose an exact recipient identity')
    if type(access_hash) is not int or not -(2**63) <= access_hash < 2**63:
        raise WalletWireError('Missing recipient access hash')
    return body + struct.pack('<IIIIqqII', int(force), VECTOR, 1, 0xf21158c6,
                              user_id, access_hash, VECTOR, 0)


@lru_cache(maxsize=2)
def _request_class(backend):
    if backend == 'pyrogram':
        from pyrogram.raw.core import TLObject
        class Request(TLObject):
            QUALNAME = 'functions.WalletMetadata'
            __slots__ = ['method', '_wire']

            def __init__(self, method, wire):
                self.method, self._wire = method, wire

            @property
            def ID(self):
                return METHODS[self.method]

            def write(self, *args):
                return self._wire
        return Request
    if backend != 'telethon':
        raise WalletWireError('Unsupported Telegram backend')
    from telethon.tl.tlobject import TLRequest
    class Request(TLRequest):
        def __init__(self, method, wire):
            self.method, self._wire = method, wire
            self.CONSTRUCTOR_ID = METHODS[method]

        def _bytes(self):
            return self._wire

        def to_dict(self):
            return {'_': 'WalletMetadata', 'method': self.method}
    return Request


def install(backend):
    """Register only missing response constructors. Existing classes are untouched."""
    if backend == 'pyrogram':
        from pyrogram.raw.core.tl_object import objects as registry
    elif backend == 'telethon':
        from telethon.tl.alltlobjects import tlobjects as registry
    else:
        raise WalletWireError('Unsupported Telegram backend')
    for ident in (STATE_EMPTY, STATE, USER_ADDRESS, USER_ADDRESSES, UPDATE_STATE, UPDATE_GASLESS):
        if ident not in registry:
            # Bind the loop value in defaults, not a shared closure cell.
            class Constructor:
                ID = ident
                CONSTRUCTOR_ID = ident

                @staticmethod
                def read(stream, *args, _ident=ident):
                    return _decode(_ident, stream, backend)

                @staticmethod
                def from_reader(stream, _ident=ident):
                    return _decode(_ident, stream, backend)
            registry.setdefault(ident, Constructor)


def request(backend, method, **kwargs):
    wire = encode_request(method, **kwargs)
    install(backend)
    return _request_class(backend)(method, wire)
