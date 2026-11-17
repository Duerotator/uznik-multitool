"""TON address validation only. No wallet, keys, signing or chain requests."""
import base64
import binascii
import re
from dataclasses import dataclass


class AddressError(ValueError):
    pass


@dataclass(frozen=True)
class TonAddress:
    raw: str
    testnet_only: bool
    bounceable: bool | None


def normalize_address(value, network):
    if not isinstance(network, str) or network not in {"mainnet", "testnet"}:
        raise AddressError("Choose mainnet or testnet explicitly")
    if not isinstance(value, str) or len(value) > 100:
        raise AddressError("Enter a public TON address, not a link or secret")
    value = value.strip()
    raw = re.fullmatch(r"(-1|0):([a-fA-F0-9]{64})", value)
    if raw:
        return TonAddress(raw[1]+":"+raw[2].lower(), False, None)
    if not re.fullmatch(r"[A-Za-z0-9_+/\-]{48}", value):
        raise AddressError("Invalid TON address format")
    try:
        data = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AddressError("Invalid TON address encoding") from exc
    if len(data) != 36 or binascii.crc_hqx(data[:34], 0).to_bytes(2, "big") != data[34:]:
        raise AddressError("Invalid TON address checksum")
    tag = data[0]
    if tag not in {0x11, 0x51, 0x91, 0xD1}:
        raise AddressError("Unsupported TON address flags")
    workchain = int.from_bytes(data[1:2], "big", signed=True)
    if workchain not in {0, -1}:
        raise AddressError("Only basechain and masterchain addresses are supported")
    testnet_only = bool(tag & 0x80)
    if testnet_only and network == "mainnet":
        raise AddressError("Testnet-only address cannot be used on mainnet")
    return TonAddress(f"{workchain}:"+data[2:34].hex(), testnet_only, (tag & 0x7F) == 0x11)


def friendly_address(value, network, *, bounceable=False):
    raw = normalize_address(value, network).raw
    workchain, account = raw.split(":")
    tag = 0x11 if bounceable else 0x51
    payload = bytes([tag | (0x80 if network == "testnet" else 0), int(workchain) & 0xFF])+bytes.fromhex(account)
    checksum = binascii.crc_hqx(payload, 0).to_bytes(2, "big")
    return base64.urlsafe_b64encode(payload+checksum).decode()


def format_nano(value):
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]{0,36}", value) or int(value) >= 2**120:
        raise ValueError("Invalid native nanoton amount")
    whole, fraction = divmod(int(value), 1_000_000_000)
    tail = str(fraction).zfill(9).rstrip("0")
    return str(whole)+("."+tail if tail else "")
