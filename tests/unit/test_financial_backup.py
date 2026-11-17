import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Portable backup tests: disposable unfunded keys, temp directories, no RPC."""
import base64
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import getpass
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import warnings
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from modules.financial_vault import FinancialVault,FinancialVaultError
from modules.financial_backup import backup,verify_backup,restore_new,MAX_FILE
from core.windows_dpapi import DPAPIError


PASSWORD = 'disposable-backup-passphrase-not-a-user-secret'


class SyntheticProtector:
    name = 'synthetic-no-production'
    def __init__(self): self.key=os.urandom(32)
    def protect(self,value,entropy):
        nonce=os.urandom(12)
        return nonce+AESGCM(self.key).encrypt(nonce,bytes(value),entropy)
    def unprotect(self,value,entropy):
        try: return AESGCM(self.key).decrypt(value[:12],value[12:],entropy)
        except Exception: raise DPAPIError('synthetic wrong owner') from None


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.protector=SyntheticProtector()
        self.vault=FinancialVault(self.root/'source',protector=self.protector)
        self.vault.create(confirmed=True)
        self.seed=os.urandom(32)
        public=Ed25519PrivateKey.from_private_bytes(self.seed).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        self.identity=dict(account='synthetic',user_id=42,context='a'*64,network='testnet',address='0:'+'01'*32,public_key=public)
        self.revision=self.vault.put(**self.identity,seed=self.seed,confirmed=True)
        self.file=self.root/'portable.enc'

    def tearDown(self): self.temp.cleanup()
    def export(self): return backup(self.vault,self.file,PASSWORD,confirmed=True)
    def target(self): return FinancialVault(self.root/'target',protector=SyntheticProtector())
    def restore(self,target,report,**changes):
        return restore_new(target,self.file,PASSWORD,confirmed=True,
            **{'expected_file_hash':report['file_hash'],'expected_manifest_hash':report['manifest_hash'],**changes})

    def test_portable_roundtrip_rewraps_master_for_new_owner_and_preserves_context(self):
        report=self.export(); target=self.target()
        result=self.restore(target,report)
        self.assertTrue(result['chain_reverification_required'])
        self.assertTrue(target.status()['locked_in_process'])
        target.unlock(confirmed=True)
        with target.use_seed(**self.identity,revision=self.revision) as view:
            self.assertEqual(bytes(view),self.seed)
        wrong_owner=FinancialVault(self.root/'target',protector=self.protector)
        with self.assertRaises(FinancialVaultError):
            with wrong_owner.use_seed(**self.identity,revision=self.revision): pass
        self.assertEqual(target.status()['records'],report['records'])

    def test_export_is_encrypted_no_master_seed_or_passphrase_and_no_overwrite(self):
        report=self.export(); raw=self.file.read_bytes()
        self.assertNotIn(self.seed,raw)
        self.assertNotIn(self.seed.hex().encode(),raw)
        self.assertNotIn(base64.b64encode(self.seed),raw)
        self.assertNotIn(PASSWORD.encode(),raw)
        self.assertNotIn('master',json.dumps(report))
        with self.assertRaises(FileExistsError): self.export()
        self.assertEqual(raw,self.file.read_bytes())

    def test_wrong_password_or_corrupted_envelope_leaves_target_absent(self):
        report=self.export(); target=self.target()
        with self.assertRaises(FinancialVaultError):
            restore_new(target,self.file,'another-disposable-password',confirmed=True,
                expected_file_hash=report['file_hash'],expected_manifest_hash=report['manifest_hash'])
        self.assertFalse(target.path.exists())
        data=json.loads(self.file.read_bytes()); data['ciphertext']=data['ciphertext'][:-8]+'AAAAAAAA'
        damaged=self.root/'damaged.enc'; damaged.write_bytes(json.dumps(data).encode())
        with self.assertRaises(FinancialVaultError): verify_backup(damaged,PASSWORD)

    def test_review_hash_consent_and_existing_target_guards(self):
        report=self.export(); target=self.target()
        with self.assertRaises(FinancialVaultError):
            restore_new(target,self.file,PASSWORD,expected_file_hash=report['file_hash'],expected_manifest_hash=report['manifest_hash'])
        with self.assertRaises(FinancialVaultError): self.restore(target,report,expected_file_hash='f'*64)
        with self.assertRaises(FinancialVaultError): self.restore(target,report,expected_manifest_hash='f'*64)
        self.assertFalse(target.path.exists())
        self.restore(target,report); before=target.path.read_bytes()
        with self.assertRaises(FinancialVaultError): self.restore(target,report)
        self.assertEqual(before,target.path.read_bytes())

    def test_malicious_kdf_namespace_duplicate_fields_and_size_fail_before_kdf(self):
        self.export(); original=json.loads(self.file.read_bytes())
        for changes in ({'format':'tgb-secret-vault'},{'version':True},{'kdf':{**original['kdf'],'n':2**40}}):
            path=self.root/'invalid.enc'; path.write_bytes(json.dumps({**original,**changes}).encode())
            with patch('modules.financial_backup.Scrypt',side_effect=AssertionError('must reject before KDF')):
                with self.assertRaises(FinancialVaultError): verify_backup(path,PASSWORD)
        duplicate=self.root/'duplicate.enc'; duplicate.write_bytes(b'{"version":1,"version":1}')
        with self.assertRaises(FinancialVaultError): verify_backup(duplicate,PASSWORD)
        big=self.root/'big.enc'
        with big.open('wb') as stream: stream.truncate(MAX_FILE+1)
        with self.assertRaises(FinancialVaultError): verify_backup(big,PASSWORD)

    def test_source_lock_wrong_seed_ciphertext_and_short_password_do_not_write_backup(self):
        for password in ('short',None):
            with self.assertRaises(FinancialVaultError): backup(self.vault,self.file,password,confirmed=True)
        self.vault.lock()
        with self.assertRaises(FinancialVaultError): self.export()
        self.vault.unlock(confirmed=True)
        with closing(sqlite3.connect(self.vault.path)) as db,db:
            row=db.execute('SELECT ciphertext FROM keys').fetchone()[0]
            db.execute('UPDATE keys SET ciphertext=?',(row[:-1]+bytes([row[-1]^1]),))
        with self.assertRaises(FinancialVaultError): self.export()
        self.assertFalse(self.file.exists())

    def test_cancelled_cli_non_tty_and_status_never_reveal_seed(self):
        from scripts.manage_financial_vault import main
        output=io.StringIO()
        with patch('scripts.manage_financial_vault.FinancialVault',return_value=self.vault),patch('sys.stdin.isatty',return_value=False),redirect_stdout(output),redirect_stderr(output):
            self.assertEqual(main(['--data-dir',str(self.root),'backup',str(self.file)]),1)
            self.assertEqual(main(['--data-dir',str(self.root),'status']),0)
        self.assertNotIn(self.seed.hex(),output.getvalue())
        self.assertFalse(self.file.exists())
        with patch('scripts.manage_financial_vault.FinancialVault',return_value=self.vault),patch('sys.stdin.isatty',return_value=True),patch('builtins.input',side_effect=KeyboardInterrupt),redirect_stdout(output),redirect_stderr(output):
            self.assertEqual(main(['--data-dir',str(self.root),'create']),130)

    def test_failed_target_dpapi_and_interrupted_export_never_write_plaintext(self):
        report=self.export(); target=self.target()
        with patch.object(target.protector,'protect',side_effect=DPAPIError('synthetic')):
            with self.assertRaises(FinancialVaultError): self.restore(target,report)
        self.assertFalse(target.path.exists())
        path=self.root/'interrupted.enc'
        with patch('modules.financial_backup.os.fsync',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError): backup(self.vault,path,PASSWORD,confirmed=True)
        self.assertNotIn(self.seed,path.read_bytes())

    def test_cli_restore_requires_exact_manifest_confirmation_and_hidden_input(self):
        from scripts.manage_financial_vault import main
        report=self.export(); target=self.target(); output=io.StringIO()
        args=['--data-dir',str(self.root/'target'),'restore-new',str(self.file)]
        with patch('scripts.manage_financial_vault.FinancialVault',return_value=target),patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',return_value=PASSWORD),patch('builtins.input',return_value='wrong'),redirect_stdout(output),redirect_stderr(output):
            self.assertEqual(main(args),1)
        self.assertFalse(target.path.exists())
        with patch('scripts.manage_financial_vault.FinancialVault',return_value=target),patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',return_value=PASSWORD),patch('builtins.input',return_value='RESTORE '+report['manifest_hash']),redirect_stdout(output),redirect_stderr(output):
            self.assertEqual(main(args),0)
        self.assertTrue(target.status()['locked_in_process'])
        self.assertNotIn(self.seed.hex(),output.getvalue())
        self.assertNotIn(PASSWORD,output.getvalue())

    def test_cli_refuses_getpass_echo_fallback(self):
        from scripts.manage_financial_vault import main
        def unsafe_input(prompt):
            warnings.warn('synthetic no tty',getpass.GetPassWarning)
            raise AssertionError('must not fall back to echoed input')
        output=io.StringIO()
        with patch('scripts.manage_financial_vault.FinancialVault',return_value=self.vault),patch('sys.stdin.isatty',return_value=True),patch('getpass.getpass',side_effect=unsafe_input),redirect_stdout(output),redirect_stderr(output):
            self.assertEqual(main(['--data-dir',str(self.root),'backup',str(self.file)]),1)
        self.assertFalse(self.file.exists())
        self.assertIn('echo fallback forbidden',output.getvalue())

    def test_restored_master_is_verified_before_success_and_failure_stays_locked(self):
        report=self.export(); target=self.target()
        with patch.object(target.protector,'unprotect',side_effect=DPAPIError('synthetic failed target verification')):
            with self.assertRaises(FinancialVaultError): self.restore(target,report)
        self.assertTrue(target.status()['locked_in_process'])
        self.assertFalse(target.path.exists())
        retried=self.restore(target,report)
        self.assertEqual(retried['records'],report['records'])
        target.unlock(confirmed=True)
        with target.use_seed(**self.identity,revision=self.revision) as view:
            self.assertEqual(bytes(view),self.seed)

    def test_empty_new_vault_can_be_backed_up_and_restored_without_seed(self):
        empty=FinancialVault(self.root/'empty',protector=SyntheticProtector()); empty.create(confirmed=True)
        path=self.root/'empty.enc'; report=backup(empty,path,PASSWORD,confirmed=True)
        target=FinancialVault(self.root/'empty-restored',protector=SyntheticProtector())
        restore_new(target,path,PASSWORD,confirmed=True,expected_file_hash=report['file_hash'],expected_manifest_hash=report['manifest_hash'])
        self.assertEqual(target.status()['records'],[])


if __name__=='__main__': unittest.main()
