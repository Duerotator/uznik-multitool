"""TEP-0003 §3.3 Rotation Mnemonic (12/24 words) & WalletTg provisioning helpers.

Implements the exact cryptography used by Telegram Desktop (`i582/wallet-engine`):
- 12-word or 24-word BIP-39 Rotation Mnemonic generation and validation
- PBKDF2-HMAC-SHA512 ('mnemonic', 2048 rounds) + SLIP-0010 Ed25519 m/44'/607'/0'
- Ownership proof signing (`ton-proof-item-v2/` + `\\xff\\xffton-connect`)
- Raw TL wire codecs for `wallet.getProofChallenge#2025e697` and `wallet.replaceWallet#d8c72eec`
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
from pathlib import Path
import re
import secrets
import struct
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.ton_address import format_nano, normalize_address
from modules.money_native_tl import (
    STATE,
    STATE_EMPTY,
    VECTOR,
    Reader,
    WalletWireError,
    install as install_native_tl,
)
from modules.money_native_transport import constructor, nano, public_key as decode_public_key
from modules.wallet_tg_codec import WalletTgCodec

WORDLIST_SHA256 = '2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda'
WORDLIST_PATH = Path(__file__).with_name('wordlist_en.txt')
SLIP10_TON_PATH = (44, 607, 0)

GET_PROOF_CHALLENGE = 0x2025E697
PROOF_CHALLENGE = 0x99E41707
OWNERSHIP_PROOF = 0x60BCCB0D
INPUT_WALLET_NEW = 0x63A440DC
INPUT_WALLET_IMPORTED = 0x66A66A3B
REPLACE_WALLET = 0xD8C72EEC
EXPORT_SECRET_PHRASE = 0xB80ED449
SECRET_PHRASE = 0x69A473A7
DECRYPTED_KEY_PART = 0x8B90DD08
INPUT_CHECK_PASSWORD_EMPTY = 0x9880F658
SEED_SHARE_LENGTH = 215
_CURVE25519_P = (1 << 255) - 19

_WORDLIST_CACHE: tuple[str, ...] | None = None
_WORD_INDEX_CACHE: dict[str, int] | None = None


class WalletCustodyError(ValueError):
    pass


def require(cond: bool, msg: str) -> None:
    if not cond:
        raise WalletWireError(msg)


def u32(val: int) -> bytes:
    return struct.pack('<I', val & 0xFFFFFFFF)


def i32(val: int) -> bytes:
    return struct.pack('<i', int(val))


def u64(val: int) -> bytes:
    return struct.pack('<Q', val & 0xFFFFFFFFFFFFFFFF)


def tl_bytes(data: bytes) -> bytes:
    raw = bytes(data)
    length = len(raw)
    if length < 254:
        prefix = bytes([length])
    else:
        prefix = b'\xfe' + length.to_bytes(3, 'little')
    pad = bytes(-(len(prefix) + length) % 4)
    return prefix + raw + pad


def tl_string(text: str) -> bytes:
    return tl_bytes(text.encode('utf-8'))


def _wipe(buf):
    if isinstance(buf, bytearray):
        buf[:] = bytes(len(buf))


def wordlist() -> tuple[str, ...]:
    global _WORDLIST_CACHE, _WORD_INDEX_CACHE
    if _WORDLIST_CACHE is None:
        raw = WORDLIST_PATH.read_bytes().replace(b'\r\n', b'\n').strip() + b'\n'
        digest = hashlib.sha256(raw).hexdigest()
        if digest != WORDLIST_SHA256:
            raise WalletCustodyError('BIP-39 English wordlist SHA-256 mismatch')
        words = tuple(line.strip() for line in raw.decode('utf-8').splitlines() if line.strip())
        if len(words) != 2048:
            raise WalletCustodyError('BIP-39 English wordlist must contain 2048 words')
        _WORDLIST_CACHE = words
        _WORD_INDEX_CACHE = {w: idx for idx, w in enumerate(words)}
    return _WORDLIST_CACHE


def _word_index() -> dict[str, int]:
    wordlist()
    assert _WORD_INDEX_CACHE is not None
    return _WORD_INDEX_CACHE


def public_key_hex(seed32: bytes | bytearray | memoryview) -> str:
    raw = bytes(seed32)
    if len(raw) != 32:
        raise WalletCustodyError('Ed25519 seed32 must be 32 bytes')
    return (
        Ed25519PrivateKey.from_private_bytes(raw)
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
        .hex()
    )


def generate_half_mnemonic(entropy: bytes | None = None) -> tuple[str, ...]:
    """Generate a 12-word BIP-39 half-phrase from 16 bytes of entropy."""
    words = wordlist()
    ent = secrets.token_bytes(16) if entropy is None else bytes(entropy)
    if len(ent) != 16:
        raise WalletCustodyError('12-word mnemonic requires 16 bytes (128 bits) of entropy')
    checksum_nibble = hashlib.sha256(ent).digest()[0] >> 4
    bits_int = (int.from_bytes(ent, 'big') << 4) | checksum_nibble
    out = []
    for shift in range(121, -1, -11):
        idx = (bits_int >> shift) & 0x7FF
        out.append(words[idx])
    return tuple(out)


def generate_rotation_mnemonic(*, rotated: bool = False) -> tuple[str, ...]:
    """Generate a 12-word (or 24-word rotated) TEP-0003 §3.3 Rotation Mnemonic."""
    first = generate_half_mnemonic()
    if not rotated:
        return first
    second = generate_half_mnemonic()
    while second == first:
        second = generate_half_mnemonic()
    return first + second


def normalize_mnemonic_words(phrase_or_words: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(phrase_or_words, str):
        cleaned = re.sub(r'(?:^|\s)\d+[\.\)\:]\s*', ' ', phrase_or_words.strip().lower())
        tokens = [tok.strip() for tok in re.split(r'[\s,;]+', cleaned) if tok.strip()]
    elif isinstance(phrase_or_words, (list, tuple)):
        tokens = [str(w).strip().lower() for w in phrase_or_words if str(w).strip()]
    else:
        raise WalletCustodyError('Recovery phrase must be a string or word sequence')
    if len(tokens) not in (12, 24):
        raise WalletCustodyError(f'Expected 12 or 24 recovery words, got {len(tokens)}')
    return tuple(tokens)


def validate_half_mnemonic(words_12: tuple[str, ...] | list[str]) -> bytes:
    if len(words_12) != 12:
        raise WalletCustodyError('Half-mnemonic must contain 12 words')
    index_map = _word_index()
    bits_int = 0
    for word in words_12:
        w = str(word).strip().lower()
        if w not in index_map:
            raise WalletCustodyError(f'Unknown BIP-39 recovery word: {w!r}')
        bits_int = (bits_int << 11) | index_map[w]
    entropy = (bits_int >> 4).to_bytes(16, 'big')
    checksum = bits_int & 0x0F
    expected = hashlib.sha256(entropy).digest()[0] >> 4
    if checksum != expected:
        raise WalletCustodyError('Invalid 12-word recovery phrase checksum')
    return entropy


def derive_half_seed(words_12: tuple[str, ...] | list[str]) -> bytearray:
    """Derive 32-byte Ed25519 seed via TEP-0003 §3.3 (BIP-39 + SLIP-0010 m/44'/607'/0')."""
    normalized = tuple(str(w).strip().lower() for w in words_12)
    validate_half_mnemonic(normalized)
    phrase_bytes = ' '.join(normalized).encode('utf-8')
    bip39_seed = bytearray(hashlib.pbkdf2_hmac('sha512', phrase_bytes, b'mnemonic', 2048, dklen=64))
    try:
        node = bytearray(hmac.digest(b'ed25519 seed', bip39_seed, 'sha512'))
        priv = bytearray(node[:32])
        chain = bytearray(node[32:])
        _wipe(node)
        for index in SLIP10_TON_PATH:
            data = bytearray(b'\x00') + priv + (int(index) | 0x80000000).to_bytes(4, 'big')
            step = bytearray(hmac.digest(chain, data, 'sha512'))
            _wipe(data)
            priv[:] = step[:32]
            chain[:] = step[32:]
            _wipe(step)
        _wipe(chain)
        return priv
    finally:
        _wipe(bip39_seed)


def parse_rotation_mnemonic(phrase_or_words: str | list[str] | tuple[str, ...]) -> dict:
    """Parse 12-word or 24-word TEP-0003 Rotation Mnemonic into anchor and signing seeds."""
    words = normalize_mnemonic_words(phrase_or_words)
    anchor_seed = derive_half_seed(words[:12])
    if len(words) == 12:
        signing_seed = bytearray(anchor_seed)
        rotated = False
    else:
        signing_seed = derive_half_seed(words[12:24])
        rotated = bytes(anchor_seed) != bytes(signing_seed)
    anchor_pub = public_key_hex(anchor_seed)
    signing_pub = public_key_hex(signing_seed)
    return {
        'kind': f'mnemonic{len(words)}',
        'words': words,
        'phrase': ' '.join(words),
        'anchor_seed': anchor_seed,
        'anchor_public_key': anchor_pub,
        'signing_seed': signing_seed,
        'signing_public_key': signing_pub,
        'rotated': rotated,
    }


def parse_seed_or_mnemonic(value: str | bytes | bytearray | list[str] | tuple[str, ...]) -> dict:
    """Accept either a 32-byte/64-hex Ed25519 seed32 OR a 12/24-word recovery phrase."""
    if isinstance(value, (list, tuple)):
        return parse_rotation_mnemonic(value)
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 32:
            raise WalletCustodyError('Binary seed must be 32 bytes')
        seed = bytearray(value)
        pub = public_key_hex(seed)
        return {
            'kind': 'seed32',
            'words': (),
            'phrase': '',
            'anchor_seed': bytearray(seed),
            'anchor_public_key': pub,
            'signing_seed': seed,
            'signing_public_key': pub,
            'rotated': False,
        }
    if not isinstance(value, str):
        raise WalletCustodyError('Expected hex seed32 or 12/24-word recovery phrase')
    stripped = value.strip()
    if re.fullmatch(r'[0-9a-fA-F]{64}', stripped):
        seed = bytearray(bytes.fromhex(stripped.lower()))
        pub = public_key_hex(seed)
        return {
            'kind': 'seed32',
            'words': (),
            'phrase': '',
            'anchor_seed': bytearray(seed),
            'anchor_public_key': pub,
            'signing_seed': seed,
            'signing_public_key': pub,
            'rotated': False,
        }
    return parse_rotation_mnemonic(stripped)


def derive_wallet_identity(
    phrase_or_seed: str | bytes | bytearray,
    *,
    network: str = 'mainnet',
    codec: WalletTgCodec | None = None,
) -> dict:
    """Derive complete WalletTg address + anchor/signing public keys from phrase or seed32."""
    parsed = parse_seed_or_mnemonic(phrase_or_seed)
    tg_codec = codec or WalletTgCodec()
    derived = tg_codec.derive(network=network, anchor_public_key=parsed['anchor_public_key'])
    parsed['network'] = network
    parsed['address'] = derived['source']
    parsed['subwallet_id'] = derived['subwallet_id']
    parsed['trampoline_hash'] = derived['trampoline_hash']
    return parsed


def sign_ownership_proof(
    *,
    signing_seed: bytes | bytearray | memoryview,
    address: str,
    domain: str,
    timestamp: int,
    payload: str,
    network: str = 'mainnet',
) -> bytes:
    """Sign TON Connect / WalletTg ownership proof (`ton-proof-item-v2/`)."""
    seed_bytes = bytes(signing_seed)
    if len(seed_bytes) != 32:
        raise WalletCustodyError('signing_seed must be 32 bytes')
    if type(timestamp) is not int or timestamp <= 0:
        raise WalletCustodyError('Proof timestamp must be a positive int')
    if not isinstance(domain, str) or not domain.strip():
        raise WalletCustodyError('Proof domain must be a non-empty string')
    if not isinstance(payload, str) or not payload:
        raise WalletCustodyError('Proof payload must be a non-empty string')
    raw_addr = normalize_address(address, network).raw
    wc_str, hex_hash = raw_addr.split(':', 1)
    workchain = int(wc_str)
    addr_hash = bytes.fromhex(hex_hash)
    domain_bytes = domain.encode('utf-8')
    payload_bytes = payload.encode('utf-8')
    msg = (
        b'ton-proof-item-v2/'
        + struct.pack('>i', workchain)
        + addr_hash
        + struct.pack('<I', len(domain_bytes))
        + domain_bytes
        + struct.pack('<Q', timestamp)
        + payload_bytes
    )
    msg_hash = hashlib.sha256(msg).digest()
    full_hash = hashlib.sha256(b'\xff\xffton-connect' + msg_hash).digest()
    return Ed25519PrivateKey.from_private_bytes(seed_bytes).sign(full_hash)


def _decode_proof_challenge_stream(stream, backend):
    r = Reader(stream, backend)
    payload = r.text()
    expires = r.int(signed=True)
    domain = r.text()
    return SimpleNamespace(
        ID=PROOF_CHALLENGE,
        CONSTRUCTOR_ID=PROOF_CHALLENGE,
        payload=payload,
        expires=expires,
        domain=domain,
    )


def install_custody(backend: str) -> None:
    """Register WalletTg proof challenge, secretPhrase and state response constructors."""
    install_native_tl(backend)
    if backend == 'pyrogram':
        from pyrogram.raw.core.tl_object import objects as registry
    elif backend == 'telethon':
        from telethon.tl.alltlobjects import tlobjects as registry
    else:
        raise WalletWireError('Unsupported Telegram backend')
    if PROOF_CHALLENGE not in registry:
        class ProofChallengeCtor:
            ID = PROOF_CHALLENGE
            CONSTRUCTOR_ID = PROOF_CHALLENGE

            @staticmethod
            def read(stream, *args):
                return _decode_proof_challenge_stream(stream, backend)

            @staticmethod
            def from_reader(stream):
                return _decode_proof_challenge_stream(stream, backend)

        registry.setdefault(PROOF_CHALLENGE, ProofChallengeCtor)
    if SECRET_PHRASE not in registry:
        class SecretPhraseCtor:
            ID = SECRET_PHRASE
            CONSTRUCTOR_ID = SECRET_PHRASE

            @staticmethod
            def read(stream, *args):
                return _decode_secret_phrase_stream(stream, backend)

            @staticmethod
            def from_reader(stream):
                return _decode_secret_phrase_stream(stream, backend)

        registry.setdefault(SECRET_PHRASE, SecretPhraseCtor)


def encode_get_proof_challenge() -> bytes:
    """Encode `wallet.getProofChallenge#2025e697 = wallet.ProofChallenge`."""
    return u32(GET_PROOF_CHALLENGE)


def decode_proof_challenge(data: bytes) -> dict:
    """Decode `wallet.proofChallenge#99e41707 payload:string expires:int domain:string`."""
    stream = io.BytesIO(data)
    r = Reader(stream, 'pyrogram')
    ctor = r.int()
    require(ctor == PROOF_CHALLENGE, f'Unexpected wallet.ProofChallenge constructor: {hex(ctor)}')
    payload = r.text()
    expires = r.int(signed=True)
    domain = r.text()
    require(stream.read() == b'', 'Trailing bytes in wallet.proofChallenge')
    require(bool(payload and domain and expires > 0), 'Invalid wallet.proofChallenge fields')
    return {'constructor': 'wallet.proofChallenge', 'payload': payload, 'expires': expires, 'domain': domain}


def encode_replace_wallet_imported(
    *,
    signing_public_key: str,
    anchor_public_key: str | None = None,
    timestamp: int,
    signature: bytes,
    srp_password_tl: bytes | None = None,
) -> bytes:
    """Encode `wallet.replaceWallet#d8c72eec` with `inputWalletImported#66a66a3b`."""
    require(
        isinstance(signing_public_key, str) and bool(re.fullmatch(r'[0-9a-f]{64}', signing_public_key)),
        'Invalid signing_public_key hex',
    )
    anchor_hex = anchor_public_key or signing_public_key
    require(
        isinstance(anchor_hex, str) and bool(re.fullmatch(r'[0-9a-f]{64}', anchor_hex)),
        'Invalid anchor_public_key hex',
    )
    require(isinstance(signature, (bytes, bytearray)) and len(signature) == 64, 'Invalid 64-byte Ed25519 proof signature')
    require(type(timestamp) is int and timestamp > 0, 'Invalid proof timestamp')

    proof_tl = u32(OWNERSHIP_PROOF) + u64(timestamp) + tl_bytes(bytes(signature))
    rotated = anchor_hex != signing_public_key
    wallet_flags = 1 if rotated else 0
    input_wallet = (
        u32(INPUT_WALLET_IMPORTED)
        + u32(wallet_flags)
        + tl_bytes(bytes.fromhex(signing_public_key))
        + (tl_bytes(bytes.fromhex(anchor_hex)) if rotated else b'')
        + proof_tl
    )
    outer_flags = 1 if srp_password_tl is not None else 0
    return (
        u32(REPLACE_WALLET)
        + u32(outer_flags)
        + (bytes(srp_password_tl) if srp_password_tl is not None else b'')
        + input_wallet
    )


def encode_replace_wallet_new(
    *,
    public_key: str,
    srp_password_tl: bytes | None = None,
) -> bytes:
    """Encode `wallet.replaceWallet#d8c72eec` with `inputWalletNew#63a440dc`."""
    require(
        isinstance(public_key, str) and bool(re.fullmatch(r'[0-9a-f]{64}', public_key)),
        'Invalid public_key hex',
    )
    input_wallet = u32(INPUT_WALLET_NEW) + tl_bytes(bytes.fromhex(public_key))
    outer_flags = 1 if srp_password_tl is not None else 0
    return (
        u32(REPLACE_WALLET)
        + u32(outer_flags)
        + (bytes(srp_password_tl) if srp_password_tl is not None else b'')
        + input_wallet
    )


def build_replace_wallet_request_from_challenge(
    *,
    challenge: dict,
    signing_seed: bytes | bytearray | memoryview,
    signing_public_key: str,
    anchor_public_key: str,
    address: str,
    network: str = 'mainnet',
    timestamp: int | None = None,
    srp_password_tl: bytes | None = None,
) -> bytes:
    now = int(time.time()) if timestamp is None else int(timestamp)
    sig = sign_ownership_proof(
        signing_seed=signing_seed,
        address=address,
        domain=challenge['domain'],
        timestamp=now,
        payload=challenge['payload'],
        network=network,
    )
    return encode_replace_wallet_imported(
        signing_public_key=signing_public_key,
        anchor_public_key=anchor_public_key,
        timestamp=now,
        signature=sig,
        srp_password_tl=srp_password_tl,
    )


def _wrap_tl_request(backend: str, ctor_id: int, qualname: str, wire: bytes):
    install_custody(backend)
    if backend == 'pyrogram':
        from pyrogram.raw.core import TLObject

        class Request(TLObject):
            ID = ctor_id
            QUALNAME = qualname

            def write(self, *args):
                return wire

        return Request()
    if backend == 'telethon':
        from telethon.tl.tlobject import TLRequest

        class Request(TLRequest):
            CONSTRUCTOR_ID = ctor_id

            def _bytes(self):
                return wire

            def to_dict(self):
                return {'_': qualname}

        return Request()
    raise WalletWireError('Unsupported Telegram backend')


def proof_challenge_request(backend: str):
    return _wrap_tl_request(
        backend,
        GET_PROOF_CHALLENGE,
        'functions.WalletGetProofChallenge',
        encode_get_proof_challenge(),
    )


def replace_wallet_imported_request(
    backend: str,
    *,
    challenge: dict,
    signing_seed: bytes | bytearray | memoryview,
    signing_public_key: str,
    anchor_public_key: str,
    address: str,
    network: str = 'mainnet',
    timestamp: int | None = None,
    srp_password_tl: bytes | None = None,
):
    wire = build_replace_wallet_request_from_challenge(
        challenge=challenge,
        signing_seed=signing_seed,
        signing_public_key=signing_public_key,
        anchor_public_key=anchor_public_key,
        address=address,
        network=network,
        timestamp=timestamp,
        srp_password_tl=srp_password_tl,
    )
    return _wrap_tl_request(backend, REPLACE_WALLET, 'functions.WalletReplaceWallet', wire)


async def get_proof_challenge(invoke, backend: str) -> dict:
    req = proof_challenge_request(backend)
    async with asyncio.timeout(20):
        reply = await invoke(req)
    if constructor(reply) != PROOF_CHALLENGE:
        raise WalletWireError('Unexpected wallet.getProofChallenge response')
    payload = getattr(reply, 'payload', None)
    expires = getattr(reply, 'expires', None)
    domain = getattr(reply, 'domain', None)
    if not isinstance(payload, str) or not payload or not isinstance(domain, str) or not domain or type(expires) is not int:
        raise WalletWireError('Invalid wallet.proofChallenge fields')
    return {'payload': payload, 'expires': expires, 'domain': domain}


async def replace_wallet_on_telegram(
    invoke,
    backend: str,
    *,
    signing_seed: bytes | bytearray | memoryview,
    signing_public_key: str,
    anchor_public_key: str,
    address: str,
    network: str = 'mainnet',
    timestamp: int | None = None,
    srp_password_tl: bytes | None = None,
) -> dict:
    challenge = await get_proof_challenge(invoke, backend)
    req = replace_wallet_imported_request(
        backend,
        challenge=challenge,
        signing_seed=signing_seed,
        signing_public_key=signing_public_key,
        anchor_public_key=anchor_public_key,
        address=address,
        network=network,
        timestamp=timestamp,
        srp_password_tl=srp_password_tl,
    )
    async with asyncio.timeout(20):
        reply = await invoke(req)
    kind = constructor(reply)
    if kind == STATE_EMPTY:
        creating = bool(getattr(reply, 'creating', False))
        return {
            'availability': 'provisioning' if creating else 'missing',
            'address': normalize_address(address, network).raw,
            'public_key': signing_public_key,
            'balance_nano': '0',
            'verification': 'telegram_reported',
        }
    if kind != STATE:
        raise WalletWireError('Unsupported wallet.replaceWallet response')
    returned_addr = normalize_address(getattr(reply, 'address', None), network).raw
    returned_pub = decode_public_key(getattr(reply, 'public_key', None))
    bal = nano(getattr(reply, 'balance', 0))
    return {
        'availability': 'ready',
        'address': returned_addr,
        'public_key': returned_pub,
        'balance_nano': bal,
        'balance_native': format_nano(bal),
        'backup_enabled': bool(getattr(reply, 'backup_enabled', False)),
        'can_export_phrase': bool(getattr(reply, 'can_export_phrase', False)),
        'verification': 'telegram_reported',
    }


def _decode_secret_phrase_stream(stream, backend):
    r = Reader(stream, backend)
    if r.int() != VECTOR:
        raise WalletWireError('Invalid wallet.secretPhrase vector')
    count = r.int(signed=True)
    if not 1 <= count <= 16:
        raise WalletWireError('Invalid wallet.secretPhrase parts count')
    parts = [r.data(max_size=4096) for _ in range(count)]
    return SimpleNamespace(
        ID=SECRET_PHRASE,
        CONSTRUCTOR_ID=SECRET_PHRASE,
        parts=parts,
    )


def tde2e_compute_shared_secret(
    my_ed25519_seed32: bytes | bytearray | memoryview,
    peer_ed25519_pubkey32: bytes | bytearray | memoryview,
) -> bytes:
    """Exact port of `tde2e_core::PrivateKey::compute_shared_secret`."""
    seed = bytes(my_ed25519_seed32)
    pub = bytearray(peer_ed25519_pubkey32)
    if len(seed) != 32 or len(pub) != 32:
        raise WalletCustodyError('tde2e ECDH requires 32-byte Ed25519 seed and 32-byte public key')
    pub[31] &= 0x7F
    y = int.from_bytes(pub, 'little')
    denom = (_CURVE25519_P - (y - 1)) % _CURVE25519_P
    inv = pow(denom, -1, _CURVE25519_P)
    u = ((y + 1) * inv) % _CURVE25519_P
    x25519_pub = u.to_bytes(32, 'little')

    h = bytearray(hashlib.sha512(seed).digest()[:32])
    h[0] &= 248
    h[31] &= 127
    h[31] |= 64
    x25519_priv = bytes(h)
    _wipe(h)

    raw_ecdh = X25519PrivateKey.from_private_bytes(x25519_priv).exchange(
        X25519PublicKey.from_public_bytes(x25519_pub)
    )
    return hmac.digest(b'tde2e_shared_secret', raw_ecdh, 'sha512')[:32]


def tde2e_encrypt_for_one(
    my_ed25519_seed32: bytes | bytearray | memoryview,
    peer_ed25519_pubkey32: bytes | bytearray | memoryview,
    plaintext: bytes,
    *,
    extra: bytes = b'',
) -> bytes:
    """Exact port of `TdE2E::TemporaryKeyPair::encryptForOne`."""
    secret = tde2e_compute_shared_secret(my_ed25519_seed32, peer_ed25519_pubkey32)
    data = bytes(plaintext)
    pad_len = ((16 + 15 + len(data)) & ~15) - len(data)
    prefix = bytearray(secrets.token_bytes(pad_len))
    prefix[0] = pad_len
    padded = bytes(prefix) + data

    large_secret = hmac.digest(secret, b'tde2e_encrypt_data', 'sha512')
    encrypt_secret = large_secret[:32]
    hmac_secret = large_secret[32:64]

    tail = padded + extra + struct.pack('<i', len(extra))
    msg_id = hmac.digest(hmac_secret, tail, 'sha256')[:16]
    cbc_hash = hmac.digest(encrypt_secret, msg_id, 'sha512')
    aes_key, aes_iv = cbc_hash[:32], cbc_hash[32:48]
    enc = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv)).encryptor()
    ciphertext = enc.update(padded) + enc.finalize()
    return msg_id + ciphertext


def tde2e_decrypt_for_one(
    my_ed25519_seed32: bytes | bytearray | memoryview,
    peer_ed25519_pubkey32: bytes | bytearray | memoryview,
    encrypted: bytes,
    *,
    extra: bytes = b'',
) -> bytes:
    """Exact port of `TdE2E::TemporaryKeyPair::decryptForOne`."""
    raw = bytes(encrypted)
    if len(raw) < 32 or (len(raw) - 16) % 16 != 0:
        raise WalletCustodyError('Invalid tde2e encrypted payload size')
    secret = tde2e_compute_shared_secret(my_ed25519_seed32, peer_ed25519_pubkey32)
    large_secret = hmac.digest(secret, b'tde2e_encrypt_data', 'sha512')
    encrypt_secret = large_secret[:32]
    hmac_secret = large_secret[32:64]

    msg_id = raw[:16]
    ciphertext = raw[16:]
    cbc_hash = hmac.digest(encrypt_secret, msg_id, 'sha512')
    aes_key, aes_iv = cbc_hash[:32], cbc_hash[32:48]
    dec = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv)).decryptor()
    decrypted_data = dec.update(ciphertext) + dec.finalize()

    tail = decrypted_data + extra + struct.pack('<i', len(extra))
    expected_msg_id = hmac.digest(hmac_secret, tail, 'sha256')[:16]
    if not hmac.compare_digest(expected_msg_id, msg_id):
        raise WalletCustodyError('tde2e message MAC mismatch')
    prefix_size = decrypted_data[0]
    if prefix_size < 16 or prefix_size > len(decrypted_data):
        raise WalletCustodyError('Invalid tde2e prefix size')
    return decrypted_data[prefix_size:]


def encrypt_phrase_share(holder_public_key32: bytes, share: bytes) -> bytes:
    """Exact port of `Wallet::PhraseShares::EncryptShare`."""
    temp_seed = secrets.token_bytes(32)
    temp_pub = bytes.fromhex(public_key_hex(temp_seed))
    plain_tl = u32(DECRYPTED_KEY_PART) + tl_bytes(bytes(share))
    cipher = tde2e_encrypt_for_one(temp_seed, holder_public_key32, plain_tl)
    return temp_pub + cipher


def decrypt_phrase_share(temp_seed32: bytes | bytearray | memoryview, encrypted_share: bytes) -> bytes:
    """Exact port of `Wallet::PhraseShares::DecryptShare`."""
    blob = bytes(encrypted_share)
    if len(blob) <= 32:
        raise WalletCustodyError('Encrypted phrase share too short')
    sender_pub = blob[:32]
    cipher = blob[32:]
    plain_tl = tde2e_decrypt_for_one(temp_seed32, sender_pub, cipher)
    if len(plain_tl) % 4 != 0:
        raise WalletCustodyError('Decrypted key part invalid TL alignment')
    stream = io.BytesIO(plain_tl)
    r = Reader(stream, 'pyrogram')
    ctor = r.int()
    if ctor != DECRYPTED_KEY_PART:
        raise WalletCustodyError(f'Unexpected decryptedKeyPart constructor: {hex(ctor)}')
    share = r.data(max_size=1024)
    if stream.read() != b'':
        raise WalletCustodyError('Trailing data in decryptedKeyPart')
    return share


def split_phrase_shares(phrase_or_words: str | list[str] | tuple[str, ...], count: int = 2) -> list[bytes]:
    """Exact port of `Wallet::PhraseShares::SeedFromWords` + `SplitSeed`."""
    words = normalize_mnemonic_words(phrase_or_words)
    joined = ' '.join(words).encode('utf-8')
    if len(joined) > SEED_SHARE_LENGTH or count < 2:
        raise WalletCustodyError('Invalid seed length or share count')
    padded = joined.ljust(SEED_SHARE_LENGTH, b' ')
    shares: list[bytes] = []
    last = bytearray(padded)
    for _ in range(count - 1):
        rnd = secrets.token_bytes(SEED_SHARE_LENGTH)
        shares.append(rnd)
        for i in range(SEED_SHARE_LENGTH):
            last[i] ^= rnd[i]
    shares.append(bytes(last))
    return shares


def combine_phrase_shares(shares: list[bytes] | tuple[bytes, ...]) -> tuple[str, ...]:
    """Exact port of `Wallet::PhraseShares::CombineShares` returning normalized words."""
    if not shares or not shares[0]:
        raise WalletCustodyError('No phrase shares to combine')
    length = len(shares[0])
    combined = bytearray(length)
    for share in shares:
        if len(share) != length:
            raise WalletCustodyError('Phrase share length mismatch')
        for i, b in enumerate(share):
            combined[i] ^= b
    phrase = bytes(combined).decode('utf-8', errors='strict').strip()
    return normalize_mnemonic_words(phrase)


def encode_export_secret_phrase(
    *,
    public_key32: bytes,
    srp_password_tl: bytes | None = None,
) -> bytes:
    """Encode `wallet.exportSecretPhrase#b80ed449 password:InputCheckPasswordSRP public_key:bytes`."""
    if not isinstance(public_key32, (bytes, bytearray)) or len(public_key32) != 32:
        raise WalletWireError('Invalid 32-byte public_key for wallet.exportSecretPhrase')
    pwd_bytes = bytes(srp_password_tl) if srp_password_tl is not None else u32(INPUT_CHECK_PASSWORD_EMPTY)
    return u32(EXPORT_SECRET_PHRASE) + pwd_bytes + tl_bytes(bytes(public_key32))


def export_secret_phrase_request(
    backend: str,
    *,
    public_key32: bytes,
    srp_password_tl: bytes | None = None,
):
    wire = encode_export_secret_phrase(public_key32=public_key32, srp_password_tl=srp_password_tl)
    return _wrap_tl_request(backend, EXPORT_SECRET_PHRASE, 'functions.WalletExportSecretPhrase', wire)


async def build_srp_password_tl(invoke, backend: str, password: str | None) -> bytes | None:
    """Compute InputCheckPasswordSRP wire bytes if password is provided and 2FA is active."""
    if not password or not isinstance(password, str):
        return None
    if backend == 'pyrogram':
        from pyrogram.raw.functions.account import GetPassword
        from pyrogram.utils import compute_password_check

        async with asyncio.timeout(20):
            pwd_state = await invoke(GetPassword())
        if not getattr(pwd_state, 'has_password', False):
            return None
        srp_obj = compute_password_check(pwd_state, password)
        return bytes(srp_obj.write())
    return None


async def export_secret_phrase_on_telegram(
    invoke,
    backend: str,
    *,
    srp_password_tl: bytes | None = None,
    network: str = 'mainnet',
    codec: WalletTgCodec | None = None,
) -> dict:
    """Export cloud-backed recovery phrase via `wallet.exportSecretPhrase` and derive WalletTg identity."""
    temp_seed = bytearray(secrets.token_bytes(32))
    try:
        temp_pub = bytes.fromhex(public_key_hex(temp_seed))
        req = export_secret_phrase_request(backend, public_key32=temp_pub, srp_password_tl=srp_password_tl)
        async with asyncio.timeout(20):
            reply = await invoke(req)
        if constructor(reply) != SECRET_PHRASE:
            raise WalletWireError('Unexpected wallet.exportSecretPhrase response')
        encrypted_parts = getattr(reply, 'parts', None)
        if not isinstance(encrypted_parts, (list, tuple)) or not encrypted_parts:
            raise WalletWireError('Empty wallet.secretPhrase parts')
        decrypted_shares = [decrypt_phrase_share(temp_seed, part) for part in encrypted_parts]
        words = combine_phrase_shares(decrypted_shares)
        return derive_wallet_identity(words, network=network, codec=codec)
    finally:
        _wipe(temp_seed)

