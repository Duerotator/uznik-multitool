import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Disposable, unfunded Ed25519 keys. No wallet/session/network/import UI."""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from core.windows_dpapi import CurrentUserDPAPI, DPAPIError
from modules.financial_vault import FinancialVault, FinancialVaultError


class FakeProtector:
    name = 'synthetic-test-protector'
    def __init__(self):
        self.key = os.urandom(32)
        self.reads = 0
    def protect(self, value, entropy):
        nonce = os.urandom(12)
        return nonce + AESGCM(self.key).encrypt(nonce, bytes(value), entropy)
    def unprotect(self, value, entropy):
        self.reads += 1
        try: return AESGCM(self.key).decrypt(value[:12], value[12:], entropy)
        except Exception: raise DPAPIError('synthetic wrong owner/context') from None


class FinancialVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.protector = FakeProtector()
        self.vault = FinancialVault(self.temp.name, protector=self.protector)
        self.seed = os.urandom(32)
        self.public = Ed25519PrivateKey.from_private_bytes(self.seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
        self.identity = dict(account='synthetic',user_id=42,context='a'*64,network='mainnet',
                             address='0:'+'01'*32,public_key=self.public)

    def tearDown(self): self.temp.cleanup()
    def create(self): self.vault.create(confirmed=True)
    def put(self, **fields): return self.vault.put(**self.identity,seed=self.seed,confirmed=True,**fields)

    def test_explicit_creation_no_overwrite_and_no_implicit_secret_creation(self):
        self.assertFalse(self.vault.status()['exists'])
        with self.assertRaises(FinancialVaultError): self.vault.create()
        self.assertFalse(self.vault.path.exists())
        with self.assertRaises(FinancialVaultError): self.put()
        self.create()
        before = self.vault.path.read_bytes()
        with self.assertRaises(FinancialVaultError): self.create()
        self.assertEqual(before,self.vault.path.read_bytes())

    def test_exact_seed_roundtrip_context_manager_wipes_and_no_plaintext_on_disk(self):
        self.create()
        revision = self.put()
        with self.vault.use_seed(**self.identity,revision=revision) as view:
            self.assertEqual(bytes(view),self.seed)
            captured = view
        self.assertEqual(bytes(captured),bytes(32))
        raw = self.vault.path.read_bytes()
        self.assertNotIn(self.seed,raw)
        self.assertNotIn(self.seed.hex().encode(),raw)
        reads = self.protector.reads
        status = self.vault.status()
        self.assertEqual(self.protector.reads,reads)
        self.assertEqual(status['signing'],'unsupported')
        self.assertNotIn(self.seed.hex(),repr(status))

    def test_restart_autounprotect_and_manual_process_lock(self):
        self.create(); revision = self.put()
        self.vault.lock()
        with self.assertRaises(FinancialVaultError):
            with self.vault.use_seed(**self.identity,revision=revision): pass
        with self.assertRaises(FinancialVaultError): self.vault.unlock()
        restarted = FinancialVault(self.temp.name,protector=self.protector)
        with restarted.use_seed(**self.identity,revision=revision) as view: self.assertEqual(bytes(view),self.seed)
        self.vault.unlock(confirmed=True)
        with self.vault.use_seed(**self.identity,revision=revision) as view: self.assertEqual(bytes(view),self.seed)

    def test_revision_cas_and_all_identity_fields_are_bound(self):
        self.create(); revision = self.put()
        with self.assertRaises(FinancialVaultError): self.put()
        changes = ({'user_id':43},{'account':'other'},{'context':'b'*64},{'network':'testnet'},
                   {'address':'0:'+'02'*32},{'public_key':'00'*32},{'revision':'f'*32})
        for change in changes:
            fields = {**self.identity,'revision':revision,**change}
            with self.assertRaises(FinancialVaultError):
                with self.vault.use_seed(**fields): pass
        new = self.put(expected_revision=revision)
        self.assertNotEqual(revision,new)
        with self.assertRaises(FinancialVaultError):
            with self.vault.use_seed(**self.identity,revision=revision): pass

    def test_seed_public_key_match_required_and_no_seed_without_consent(self):
        self.create()
        with self.assertRaises(FinancialVaultError): self.vault.put(**self.identity,seed=self.seed)
        with self.assertRaises(FinancialVaultError): self.vault.put(**{**self.identity,'public_key':'00'*32},seed=self.seed,confirmed=True)
        self.assertEqual(self.vault.status()['records'],[])

    def test_modified_ciphertext_and_wrong_windows_owner_fail_closed(self):
        self.create(); revision = self.put()
        other = FinancialVault(self.temp.name,protector=FakeProtector())
        with self.assertRaises(FinancialVaultError):
            with other.use_seed(**self.identity,revision=revision): pass
        with closing(sqlite3.connect(self.vault.path)) as db, db:
            row = db.execute('SELECT ciphertext FROM keys').fetchone()[0]
            db.execute('UPDATE keys SET ciphertext=?',(row[:-1]+bytes([row[-1]^1]),))
        with self.assertRaises(FinancialVaultError):
            with self.vault.use_seed(**self.identity,revision=revision): pass

    def test_record_copy_between_vaults_fails_aad_and_master_authentication(self):
        self.create(); revision = self.put()
        second = FinancialVault(Path(self.temp.name)/'second',protector=self.protector)
        second.create(confirmed=True)
        with closing(sqlite3.connect(self.vault.path)) as db:
            row = db.execute('SELECT * FROM keys').fetchone()
        with closing(sqlite3.connect(second.path)) as db, db:
            db.execute('INSERT INTO keys VALUES(?,?,?,?,?)',row)
        with self.assertRaises(FinancialVaultError):
            with second.use_seed(**self.identity,revision=revision): pass

    def test_wipe_even_when_consumer_raises(self):
        self.create(); revision = self.put(); captured = None
        with self.assertRaises(RuntimeError):
            with self.vault.use_seed(**self.identity,revision=revision) as view:
                captured = view
                raise RuntimeError('synthetic consumer failure')
        self.assertEqual(bytes(captured),bytes(32))

    def test_unsupported_platform_has_no_plaintext_fallback_or_created_file(self):
        vault = FinancialVault(Path(self.temp.name)/'not-windows')
        with patch('core.windows_dpapi.sys.platform','linux'):
            with self.assertRaises(FinancialVaultError): vault.create(confirmed=True)
        self.assertFalse(vault.path.exists())


@unittest.skipUnless(sys.platform == 'win32','Disposable CurrentUser DPAPI test requires Windows')
class RealDPAPITests(unittest.TestCase):
    def test_real_current_user_roundtrip_entropy_and_ciphertext_damage(self):
        protector = CurrentUserDPAPI()
        value, entropy = os.urandom(32), os.urandom(32)
        wrapped = protector.protect(value,entropy)
        self.assertNotIn(value,wrapped)
        self.assertEqual(protector.unprotect(wrapped,entropy),value)
        with self.assertRaises(DPAPIError): protector.unprotect(wrapped,os.urandom(32))
        with self.assertRaises(DPAPIError): protector.unprotect(wrapped[:-5],entropy)

    def test_real_dpapi_financial_vault_restart_with_unfunded_random_seed(self):
        with tempfile.TemporaryDirectory() as root:
            vault = FinancialVault(root)
            vault.create(confirmed=True)
            seed = os.urandom(32)
            public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
            identity = dict(account='disposable',user_id=42,context='a'*64,network='testnet',address='0:'+'01'*32,public_key=public)
            revision = vault.put(**identity,seed=seed,confirmed=True)
            with FinancialVault(root).use_seed(**identity,revision=revision) as view:
                self.assertEqual(bytes(view),seed)


if __name__ == '__main__': unittest.main()
