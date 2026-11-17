import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Synthetic TL fixtures/fake clients only. Never open an account/session."""
import asyncio
from contextlib import asynccontextmanager, closing
from io import BytesIO
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from core.models import AccountRecord
from modules import money_native_tl as tl
from modules.money_native_transport import state, gasless, recipient
from modules.money_native import NativeMoneyService, NativeMoneyError

SOURCE = '0:' + '01' * 32
TARGET = '0:' + '02' * 32


def integer(value): return struct.pack('<I', value)
def long(value): return struct.pack('<q', value)
def data(value):
    value = value.encode() if isinstance(value, str) else value
    prefix = bytes([len(value)]) if len(value) < 254 else b'\xfe' + len(value).to_bytes(3, 'little')
    return prefix + value + bytes(-(len(prefix) + len(value)) % 4)
def vector(items): return integer(tl.VECTOR) + integer(len(items)) + b''.join(items)
def state_bytes(): return integer(tl.STATE) + integer(3) + data(SOURCE) + data(b'x'*32) + long(123000000)


def decode(raw, backend):
    tl.install(backend)
    if backend == 'pyrogram':
        from pyrogram.raw.core import TLObject
        return TLObject.read(BytesIO(raw))
    from telethon.extensions import BinaryReader
    return BinaryReader(raw).tgread_object()


class WireTests(unittest.TestCase):
    def test_exact_request_wire_for_both_libraries_and_allowed_methods_only(self):
        for backend in ('pyrogram', 'telethon'):
            for method in ('state', 'gasless'):
                request = tl.request(backend, method)
                self.assertEqual(request.write() if backend == 'pyrogram' else bytes(request), integer(tl.METHODS[method]))
            request = tl.request(backend, 'addresses', user_id=84, access_hash=-5, force=True)
            expected = integer(tl.METHODS['addresses']) + integer(1) + vector([integer(0xf21158c6) + long(84) + long(-5)]) + vector([])
            self.assertEqual(request.write() if backend == 'pyrogram' else bytes(request), expected)
        for method in ('sendTransfer', 'exportSecretPhrase', 'replaceWallet'):
            with self.assertRaises(tl.WalletWireError): tl.request('pyrogram', method)
        for args in ({'user_id':True, 'access_hash':1}, {'user_id':84, 'access_hash':None}, {'user_id':84, 'access_hash':1, 'force':1}):
            with self.assertRaises(tl.WalletWireError): tl.request('telethon', 'addresses', **args)

    def test_state_empty_and_public_state_binary_roundtrip(self):
        for backend in ('pyrogram', 'telethon'):
            row = decode(state_bytes(), backend)
            self.assertEqual(row.address, SOURCE)
            self.assertEqual(row.public_key, b'x'*32)
            self.assertEqual(row.balance, 123000000)
            self.assertTrue(row.backup_enabled)
            self.assertTrue(decode(integer(tl.STATE_EMPTY) + integer(1), backend).creating)

    def test_addresses_and_nested_existing_user_objects(self):
        from pyrogram.raw.types import UserEmpty
        item = integer(tl.USER_ADDRESS) + integer(1) + long(84) + data(TARGET) + data(b'y'*32)
        raw = integer(tl.USER_ADDRESSES) + vector([item]) + vector([UserEmpty(id=84).write()])
        for backend in ('pyrogram', 'telethon'):
            result = decode(raw, backend)
            self.assertEqual(result.addresses[0].user_id, 84)
            self.assertEqual(result.users[0].id, 84)

    def test_gasless_inside_existing_updates_container(self):
        update = integer(tl.UPDATE_GASLESS) + integer(1) + integer(5) + integer(2000) + long(100000000) + data(SOURCE)
        raw = integer(0x74ae4240) + vector([update]) + vector([]) + vector([]) + integer(1000) + integer(1)
        for backend in ('pyrogram', 'telethon'):
            result = decode(raw, backend)
            self.assertEqual(result.updates[0].left, 5)
            self.assertEqual(result.updates[0].min_amount, 100000000)

    def test_reject_unknown_flags_truncation_oversize_and_bad_vectors(self):
        from pyrogram.raw.core.tl_object import objects as pyrogram_objects
        fallback_ids = (tl.STATE_EMPTY, tl.STATE, tl.USER_ADDRESS, tl.USER_ADDRESSES, tl.UPDATE_STATE, tl.UPDATE_GASLESS)
        vectors = (integer(tl.STATE_EMPTY) + integer(2), state_bytes()[:-1],
                   integer(tl.USER_ADDRESSES) + integer(tl.VECTOR) + integer(101),
                   integer(tl.STATE) + integer(0) + data(b'x'*1025))
        with patch.dict(pyrogram_objects, clear=False):
            for ident in fallback_ids:
                pyrogram_objects.pop(ident, None)
            for raw in vectors:
                with self.assertRaises((tl.WalletWireError, BufferError, ValueError, EOFError)):
                    decode(raw, 'pyrogram')
        for raw in vectors:
            with self.assertRaises((tl.WalletWireError, BufferError, ValueError, EOFError)):
                decode(raw, 'telethon')

    def test_additive_registry_does_not_replace_existing_constructor(self):
        from pyrogram.raw.core.tl_object import objects
        from telethon.tl.alltlobjects import tlobjects
        for backend, registry in (('pyrogram', objects), ('telethon', tlobjects)):
            tl.install(backend)
            original = registry[tl.STATE]
            tl.install(backend)
            self.assertIs(registry[tl.STATE], original)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_account_adapters_resolve_username_and_reject_reassigned_peer(self):
        from core.telegram_client import PyrogramAccountClient, TelethonAccountClient
        from pyrogram import raw
        from telethon.tl import types
        for adapter, classes in ((PyrogramAccountClient, raw.types), (TelethonAccountClient, types)):
            resolved = SimpleNamespace(peer=classes.PeerUser(user_id=84), users=[classes.User(id=84,access_hash=7)])
            address = SimpleNamespace(ID=tl.USER_ADDRESS,user_id=84,address=TARGET,public_key=b'y'*32)
            fake = SimpleNamespace(_native_money_invoke=AsyncMock(side_effect=[resolved,SimpleNamespace(ID=tl.USER_ADDRESSES,addresses=[address])]))
            result = await adapter.native_money_recipient(fake,'TargetUser',84,prepare_address=True)
            self.assertEqual(result['address'], TARGET)
            self.assertEqual(fake._native_money_invoke.await_count, 2)
            resolved.peer = classes.PeerUser(user_id=85)
            fake._native_money_invoke = AsyncMock(return_value=resolved)
            with self.assertRaises(ValueError): await adapter.native_money_recipient(fake,'TargetUser',84,prepare_address=True)
            self.assertEqual(fake._native_money_invoke.await_count, 1)

    async def test_force_rechecks_admission_after_username_resolution_before_request(self):
        from core.telegram_client import PyrogramAccountClient, TelethonAccountClient
        from pyrogram import raw
        from telethon.tl import types
        for adapter, classes in ((PyrogramAccountClient,raw.types),(TelethonAccountClient,types)):
            resolved=SimpleNamespace(peer=classes.PeerUser(user_id=84),users=[classes.User(id=84,access_hash=7)])
            fake=SimpleNamespace(_native_money_invoke=AsyncMock(return_value=resolved))
            with self.assertRaises(ValueError):
                await adapter.native_money_recipient(fake,'TargetUser',84,prepare_address=True,admission_check=lambda:False)
            self.assertEqual(fake._native_money_invoke.await_count,1,'No force RPC after scope/Stop revocation')

    async def test_state_distinguishes_telegram_ready_from_chain_and_signer(self):
        for backend in ('pyrogram', 'telethon'):
            result = await state(AsyncMock(return_value=decode(state_bytes(), backend)), backend)
            self.assertEqual(result['availability'], 'ready')
            self.assertEqual(result['balance_nano'], '123000000')
            self.assertEqual(result['chain_state'], 'unknown')
            self.assertEqual(result['signer'], 'unknown')
            empty = decode(integer(tl.STATE_EMPTY) + integer(0), backend)
            self.assertEqual((await state(AsyncMock(return_value=empty), backend))['availability'], 'missing')

    async def test_gasless_is_terms_not_fee_estimate_and_missing_terms_rejected(self):
        row = SimpleNamespace(ID=tl.UPDATE_GASLESS, available=True, left=3, reset_at=2000, min_amount=100000000, relayer_address=SOURCE)
        result = await gasless(AsyncMock(return_value=SimpleNamespace(updates=[row])), 'pyrogram')
        self.assertIsNone(result['ordinary_fee_nano'])
        self.assertFalse(result['automatic_sponsorship'])
        with self.assertRaises(ValueError): await gasless(AsyncMock(return_value=SimpleNamespace(updates=[])), 'telethon')
        row.min_amount = True
        with self.assertRaises(ValueError): await gasless(AsyncMock(return_value=SimpleNamespace(updates=[row])), 'pyrogram')

    async def test_recipient_identity_and_key_validation_no_activation_claim(self):
        item = SimpleNamespace(ID=tl.USER_ADDRESS, user_id=84, address=TARGET, public_key=b'y'*32)
        invoke = AsyncMock(return_value=SimpleNamespace(ID=tl.USER_ADDRESSES, addresses=[item]))
        result = await recipient(invoke, 'telethon', user_id=84, access_hash=7, force=True)
        self.assertEqual(result['money_activated'], 'unknown')
        self.assertTrue(result['prepare_address_requested'])
        item.user_id = 85
        with self.assertRaises(ValueError): await recipient(invoke, 'pyrogram', user_id=84, access_hash=7)

    async def test_native_kurigram_objects_remain_strictly_validated(self):
        from pyrogram import raw
        from pyrogram.raw.core.tl_object import objects
        tl.install('pyrogram')
        self.assertIs(objects[tl.STATE], raw.types.WalletState)
        oversize_state = raw.types.WalletState(address='x' * 1025, public_key=b'x' * 32, balance=1)
        with self.assertRaises(ValueError):
            await state(AsyncMock(return_value=oversize_state), 'pyrogram')
        bad_key_state = raw.types.WalletState(address=SOURCE, public_key=b'x' * 33, balance=1)
        with self.assertRaises(ValueError):
            await state(AsyncMock(return_value=bad_key_state), 'pyrogram')
        bad_users = raw.types.wallet.UserAddresses(
            addresses=[raw.types.WalletUserAddress(address=TARGET, public_key=b'y' * 32, user_id=84)],
            users=[raw.types.UserEmpty(id=i) for i in range(101)],
        )
        with self.assertRaises(ValueError):
            await recipient(AsyncMock(return_value=bad_users), 'pyrogram', user_id=84, access_hash=7)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000
        self.source = AccountRecord('one','Source','pyrogram','session_file','never.session',user_id=42,username='SourceUser')
        self.target = AccountRecord('two','Target','pyrogram','session_file','also-never.session',user_id=84,username='TargetUser')
        self.records = {'one': self.source, 'two': self.target}
        self.accounts = SimpleNamespace(get_account=lambda ident:self.records[ident])
        self.remote = SimpleNamespace(get_me=AsyncMock(return_value={'user_id':42}),
            native_money_state=AsyncMock(return_value={'availability':'ready','balance_nano':'123000000','address':SOURCE}),
            native_money_gasless=AsyncMock(return_value={'left':5,'ordinary_fee_nano':None}),
            native_money_recipient=AsyncMock(return_value={'address':TARGET,'money_activated':'unknown'}))
        self.opened = 0
        @asynccontextmanager
        async def factory(config, account):
            self.opened += 1
            yield self.remote
        self.factory = factory
        self.config = SimpleNamespace(data_dir=Path(self.temp.name))
        self.service = NativeMoneyService(self.config,accounts=self.accounts,client_factory=factory,clock=lambda:self.now)

    def tearDown(self): self.temp.cleanup()

    async def call(self, op, **fields):
        return await self.service.action(op,{'account_id':'one',**fields},scope_ids={'one','two'},ui_context='fixture')

    async def test_explicit_status_persists_restart_local_snapshot_never_opens_session(self):
        with self.assertRaises(NativeMoneyError): await self.call('native-status')
        self.assertEqual(self.opened, 0)
        result = await self.call('native-status',confirmed=True)
        self.assertEqual(result['native']['signing'], 'unsupported')
        self.service = NativeMoneyService(self.config,accounts=self.accounts,client_factory=self.factory,clock=lambda:self.now)
        self.assertFalse((await self.call('native-last'))['stale'])
        self.assertEqual(self.opened, 1)
        self.now += 301
        self.assertTrue((await self.call('native-last'))['stale'])

    async def test_resolve_exact_scoped_target_and_force_is_explicit(self):
        result = await self.call('native-resolve',target_account_id='two',prepare_address=True,confirmed=True)
        self.remote.native_money_recipient.assert_awaited_once()
        self.assertEqual(self.remote.native_money_recipient.await_args.args,('TargetUser',84))
        self.assertTrue(self.remote.native_money_recipient.await_args.kwargs['prepare_address'])
        self.assertTrue(callable(self.remote.native_money_recipient.await_args.kwargs['admission_check']))
        self.assertEqual(result['native']['recipient']['account_id'], 'two')
        self.assertEqual(result['native']['recipient']['money_activated'], 'unknown')
        with self.assertRaises(NativeMoneyError): await self.call('native-resolve',target_account_id='two',prepare_address=True,confirmed=True)

    async def test_scope_bad_fields_no_username_and_unknown_identity_fail_before_network(self):
        for fields in ({'target_account_id':'outside','prepare_address':False},
                       {'target_account_id':'one','prepare_address':False},
                       {'target_account_id':'two','prepare_address':'yes'},
                       {'target_account_id':'two','prepare_address':False,'seed':'forbidden'}):
            with self.assertRaises(NativeMoneyError): await self.call('native-resolve',confirmed=True,**fields)
        self.target.username = None
        with self.assertRaises(NativeMoneyError): await self.call('native-resolve',target_account_id='two',prepare_address=False,confirmed=True)
        self.assertEqual(self.opened, 0)

    async def test_session_mismatch_does_not_call_money(self):
        self.remote.get_me.return_value = {'user_id':99}
        with self.assertRaises(NativeMoneyError): await self.call('native-status',confirmed=True)
        self.remote.native_money_state.assert_not_awaited()

    async def test_scope_change_discards_late_read(self):
        async def change():
            self.target.username = 'ChangedUser'
            return {'address':TARGET}
        self.remote.native_money_recipient.side_effect = lambda *args, **kwargs: None
        async def changed(*args, **kwargs): return await change()
        self.remote.native_money_recipient.side_effect = changed
        with self.assertRaises(NativeMoneyError): await self.call('native-resolve',target_account_id='two',prepare_address=False,confirmed=True)
        self.assertIsNone((await self.call('native-last'))['native'])

    async def test_failed_read_preserves_last_snapshot_but_marks_stale_and_sanitizes(self):
        await self.call('native-status',confirmed=True)
        self.now += 11
        self.remote.native_money_state.side_effect = RuntimeError('synthetic-secret-must-not-leak')
        with self.assertRaises(NativeMoneyError) as caught: await self.call('native-status',confirmed=True)
        self.assertNotIn('synthetic-secret', str(caught.exception))
        result = await self.call('native-last')
        self.assertTrue(result['stale'])
        self.assertIsNotNone(result['native'])
        self.assertNotIn('synthetic-secret', self.service.path.read_bytes().decode('latin1'))

    async def test_cancelled_native_read_is_not_swallowed_and_snapshot_is_stale(self):
        self.remote.native_money_state.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError): await self.call('native-status',confirmed=True)
        result = await self.call('native-last')
        self.assertIsNone(result['native'])
        self.assertTrue(result['stale'])
        self.assertEqual(result['error'],'CancelledError')

    async def test_floodwait_cooldown_survives_restart_without_sleep_or_retry(self):
        class SyntheticFlood(Exception):
            value = 33809
        self.remote.native_money_state.side_effect = SyntheticFlood('not-logged')
        with self.assertRaises(NativeMoneyError): await self.call('native-status',confirmed=True)
        self.service = NativeMoneyService(self.config,accounts=self.accounts,client_factory=self.factory,clock=lambda:self.now)
        self.now += 100
        with self.assertRaises(NativeMoneyError): await self.call('native-status',confirmed=True)
        self.assertEqual(self.remote.native_money_state.await_count,1)

    async def test_replaced_observation_lease_rejects_old_worker_without_clobbering_new(self):
        async def race():
            with closing(self.service.database()) as db, db:
                db.execute("UPDATE observations SET lease='new-owner' WHERE account='one'")
            return {'availability':'ready','balance_nano':'123000000','address':SOURCE}
        self.remote.native_money_state.side_effect = race
        with self.assertRaises(NativeMoneyError): await self.call('native-status',confirmed=True)
        with closing(self.service.database()) as db:
            row = db.execute("SELECT lease,error,payload FROM observations WHERE account='one'").fetchone()
        self.assertEqual(row['lease'],'new-owner')
        self.assertIsNone(row['error'])
        self.assertIsNone(row['payload'])


if __name__ == '__main__': unittest.main()
