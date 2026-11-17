"""Native Money metadata operations, no signer or financial mutation RPCs."""
import asyncio

from core.ton_address import normalize_address, format_nano
from modules.money_native_tl import request, STATE, STATE_EMPTY, USER_ADDRESSES, USER_ADDRESS, UPDATE_GASLESS


def constructor(obj):
    return getattr(obj, 'ID', getattr(obj, 'CONSTRUCTOR_ID', None))


def nano(value):
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError('Invalid native Money amount')
    return str(value)


def public_key(value):
    if not isinstance(value, bytes) or len(value) != 32:
        raise ValueError('Invalid native Money public key')
    return value.hex()


async def call(invoke, backend, method, **kwargs):
    async with asyncio.timeout(20):
        return await invoke(request(backend, method, **kwargs))


async def state(invoke, backend):
    reply = await call(invoke, backend, 'state')
    kind = constructor(reply)
    if kind == STATE_EMPTY:
        creating = getattr(reply, 'creating', False)
        if creating is not None and type(creating) is not bool:
            raise ValueError('Invalid Money availability')
        return {'availability': 'provisioning' if creating else 'missing', 'address': None,
                'balance_nano': None, 'signer': 'unknown', 'chain_state': 'unknown',
                'verification': 'telegram_reported'}
    if kind != STATE:
        raise ValueError('Unsupported Money state response')
    address = normalize_address(getattr(reply, 'address', None), 'mainnet').raw
    balance = nano(getattr(reply, 'balance', None))
    backup = getattr(reply, 'backup_enabled', False)
    can_export = getattr(reply, 'can_export_phrase', False)
    can_enable = getattr(reply, 'can_enable_backup', False)
    if any(v is not None and type(v) is not bool for v in (backup, can_export, can_enable)):
        raise ValueError('Invalid native Money state flags')
    return {'availability': 'ready', 'address': address, 'public_key': public_key(getattr(reply, 'public_key', None)),
            'balance_nano': balance, 'balance_native': format_nano(balance),
            'backup_enabled': bool(backup),
            'can_export_phrase': bool(can_export),
            'signer': 'unknown', 'chain_state': 'unknown', 'verification': 'telegram_reported'}


async def gasless(invoke, backend):
    response = await call(invoke, backend, 'gasless')
    updates = getattr(response, 'updates', None)
    if not isinstance(updates, (tuple, list)) or len(updates) > 100:
        raise ValueError('Incomplete native gasless response')
    matches = [u for u in updates if constructor(u) == UPDATE_GASLESS]
    if len(matches) != 1:
        raise ValueError('Missing/ambiguous native gasless terms')
    row = matches[0]
    left, reset = getattr(row, 'left', None), getattr(row, 'reset_at', None)
    if type(left) is not int or not 0 <= left < 2**31 or type(reset) is not int or not 0 <= reset < 2**31:
        raise ValueError('Invalid native gasless limits')
    minimum = nano(getattr(row, 'min_amount', None))
    available = getattr(row, 'available', False)
    if available is not None and type(available) is not bool:
        raise ValueError('Invalid native gasless availability')
    relayer = getattr(row, 'relayer_address', None)
    if not isinstance(relayer, str):
        raise ValueError('Missing gasless relayer field')
    relayer = normalize_address(relayer, 'mainnet').raw if relayer else None
    if available and not relayer:
        raise ValueError('Gasless terms have no relayer')
    return {'available': bool(available), 'left': left, 'reset_at': reset,
            'min_amount_nano': minimum, 'relayer_address': relayer,
            'verification': 'telegram_reported', 'fee_estimate': 'unsupported',
            'ordinary_fee_nano': None, 'automatic_sponsorship': False}


async def recipient(invoke, backend, *, user_id, access_hash, force=False, admission_check=None):
    if admission_check is not None and (not callable(admission_check) or admission_check() is not True):
        raise ValueError('Recipient preparation admission revoked')
    reply = await call(invoke, backend, 'addresses', user_id=user_id, access_hash=access_hash, force=force)
    rows = getattr(reply, 'addresses', None)
    users = getattr(reply, 'users', None)
    if constructor(reply) != USER_ADDRESSES or not isinstance(rows, (list, tuple)) or len(rows) > 1:
        raise ValueError('Unexpected native recipient response')
    if users is not None and (not isinstance(users, (list, tuple)) or len(users) > 100):
        raise ValueError('Unexpected native recipient users vector')
    if not rows:
        return {'recipient_user_id': user_id, 'availability': 'address_not_returned', 'address': None,
                'verification': 'telegram_reported', 'prepare_address_requested': force}
    row = rows[0]
    if constructor(row) != USER_ADDRESS or getattr(row, 'user_id', None) != user_id:
        raise ValueError('Native recipient identity mismatch')
    return {'recipient_user_id': user_id, 'availability': 'address_returned',
            'address': normalize_address(getattr(row, 'address', None), 'mainnet').raw,
            'public_key': public_key(getattr(row, 'public_key', None)),
            'verification': 'telegram_reported', 'prepare_address_requested': force,
            'money_activated': 'unknown', 'signer': 'unknown'}
