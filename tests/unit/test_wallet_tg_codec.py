import sys as _sys
from pathlib import Path as _Path
for _p in (_Path(__file__).resolve().parents[2] / 'app', _Path(__file__).resolve().parent):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))
"""Offline codec/fixture signatures with random unfunded keys. No RPC/broadcast."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from core.windows_dpapi import DPAPIError
from modules.financial_vault import FinancialVault,FinancialVaultError
from modules.wallet_tg_codec import WalletTgCodec,WalletCodecError,HELPER,CODE_HASH,REV00_HASH,SUBWALLET

AVAILABLE=bool(shutil.which('node') and (HELPER.parent/'node_modules'/'@ton'/'core'/'package.json').is_file())
DEST='0:'+'02'*32
ANCHOR_REFERENCE='d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a'


class Protector:
    name='synthetic-offline-key'
    def __init__(self): self.key=os.urandom(32);self.reads=0
    def protect(self,value,entropy):
        nonce=os.urandom(12);return nonce+AESGCM(self.key).encrypt(nonce,bytes(value),entropy)
    def unprotect(self,value,entropy):
        self.reads+=1
        try: return AESGCM(self.key).decrypt(value[:12],value[12:],entropy)
        except Exception: raise DPAPIError('synthetic wrong owner') from None


@unittest.skipUnless(AVAILABLE,'Pinned Node/TON codec runtime not installed; production stays unsupported')
class CodecTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.now=1000
        self.codec=WalletTgCodec(clock=lambda:self.now)
        self.seed=os.urandom(32)
        self.public=Ed25519PrivateKey.from_private_bytes(self.seed).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        self.anchor=Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
        self.owner=self.codec.derive(self.anchor,'mainnet')
        self.protector=Protector();self.vault=FinancialVault(self.temp.name,protector=self.protector);self.vault.create(confirmed=True)
        self.identity=dict(account='unfunded-fixture',user_id=42,context='a'*64,network='mainnet',address=self.owner['source'],public_key=self.public)
        revision=self.vault.put(**self.identity,seed=self.seed,confirmed=True)
        self.identity['revision']=revision
        self.snapshot=dict(network='mainnet',address=self.owner['source'],active=True,code_hash=CODE_HASH,implementation_hash=REV00_HASH,
            subwallet_id=SUBWALLET['mainnet'],public_key=self.public,seqno=7,checked_at=self.now,block_id='a'*64,evidence='offline_fixture')
        self.transfer=dict(destination=DEST,amount_nano='100000000',intent_id='b'*32,valid_until=1180,channel='external',mode=3)

    def tearDown(self): self.temp.cleanup()
    def sign(self,**changes):
        return self.codec.sign_fixture(self.vault,self.identity,anchor_public_key=self.anchor,snapshot=self.snapshot,transfer={**self.transfer,**changes})

    def test_public_address_regression_fixture_and_network_separation(self):
        # Public RFC8032 key only. No private fixture is embedded or used/funded.
        main=self.codec.derive(ANCHOR_REFERENCE,'mainnet')
        self.assertEqual(main['source'],'0:25f9ce4527cc0e1244d8756f20c78d39b431d9d5bd02a1a6c6cd433d4038a574')
        self.assertEqual(main['trampoline_hash'],CODE_HASH)
        self.assertNotEqual(main['source'],self.codec.derive(ANCHOR_REFERENCE,'testnet')['source'])

    def test_rotated_signing_key_keeps_anchor_address_and_signature_verifies(self):
        result=self.sign()
        self.assertEqual(result['source'],self.owner['source'])
        self.assertNotEqual(result['source'],self.codec.derive(self.public,'mainnet')['source'])
        self.assertFalse(result['production_signing'])
        self.assertFalse(result['broadcast_supported'])
        self.assertIn('external_boc',result)
        self.assertNotIn(self.seed.hex(),json.dumps(result))
        self.assertNotIn(base64.b64encode(self.seed).decode(),json.dumps(result))

    def test_external_and_internal_payloads_differ_and_signature_not_reusable(self):
        external=self.sign()
        internal=self.sign(channel='internal')
        self.assertEqual(external['opcode'],0x63896e75)
        self.assertEqual(internal['opcode'],0x63896e74)
        self.assertNotEqual(external['payload_hash'],internal['payload_hash'])
        self.assertNotIn('external_boc',internal)

    def test_stale_key_network_code_impl_anchor_and_incoherent_snapshot_block_key_access(self):
        before=self.protector.reads
        original=dict(self.snapshot)
        for field,value in (('public_key','00'*32),('network','testnet'),('active',False),('code_hash','00'*32),
                ('implementation_hash','00'*32),('subwallet_id',0),('checked_at',969),('checked_at',1001),
                ('seqno',True),('seqno',2**32),('evidence','provider_reported'),('address',DEST)):
            self.snapshot={**original,field:value}
            with self.assertRaises(WalletCodecError): self.sign()
        self.assertEqual(before,self.protector.reads)

    def test_header_amount_modes_expiry_and_arbitrary_payload_are_rejected(self):
        before=self.protector.reads
        for changes in ({'valid_until':1000},{'valid_until':1301},{'valid_until':True},{'mode':35},{'mode':130},
                        {'mode':True},{'amount_nano':'1e8'},{'amount_nano':'0'},{'amount_nano':'1000000001'},
                        {'amount_nano':0.1},{'payload_boc':'forbidden'},{'channel':'other'}):
            with self.assertRaises(WalletCodecError): self.sign(**changes)
        self.assertEqual(before,self.protector.reads)

    def test_vault_context_revision_and_process_lock_block_fixture_signing(self):
        self.vault.lock()
        with self.assertRaises(FinancialVaultError): self.sign()
        self.vault.unlock(confirmed=True)
        self.identity['revision']='f'*32
        with self.assertRaises(FinancialVaultError): self.sign()

    def test_node_never_receives_private_seed_and_scrubs_preload_environment(self):
        original=subprocess.run;seen=[]
        def recording(*args,**kwargs):
            seen.append(kwargs)
            return original(*args,**kwargs)
        with patch('modules.wallet_tg_codec.subprocess.run',side_effect=recording),patch.dict(os.environ,{'NODE_OPTIONS':'--require forbidden-preload','TELEGRAM_API_HASH':'synthetic-never-pass'}):
            self.sign()
        for invocation in seen:
            self.assertNotIn(self.seed.hex().encode(),invocation['input'])
            self.assertNotIn(base64.b64encode(self.seed),invocation['input'])
            self.assertNotIn('NODE_OPTIONS',invocation['env'])
            self.assertNotIn('TELEGRAM_API_HASH',invocation['env'])

    def test_independent_core_decode_checks_signed_header_comment_and_envelope(self):
        result=self.sign()
        # This oracle decodes our BOC; it does not import the application codec.
        script=r"""
const {Cell,loadMessageRelaxed,loadMessage}=require('@ton/core');
const fs=require('fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));
const signed=Cell.fromBase64(x.signed_body_boc).beginParse();const sig=signed.loadBuffer(64);
const payload=signed.asCell();
const opcode=signed.loadUint(32),subwallet=signed.loadUint(32),until=signed.loadUint(32),seqno=signed.loadUint(32),mode=signed.loadUint(8);
const message=loadMessageRelaxed(signed.loadRef().beginParse());
const comment=message.body.beginParse();const commentOpcode=comment.loadUint(32);
const env=loadMessage(Cell.fromBase64(x.external_boc).beginParse());
console.log(JSON.stringify({sig:sig.toString('hex'),payload_hash:payload.hash().toString('hex'),opcode,subwallet,until,seqno,mode,
  dest:message.info.dest.toRawString(),amount:message.info.value.coins.toString(),bounce:message.info.bounce,
  commentOpcode,comment:comment.loadStringTail(),envType:env.info.type,source:env.info.dest.toRawString(),
  sameBody:env.body.hash().equals(Cell.fromBase64(x.signed_body_boc).hash()),noInit:!env.init}));
"""
        run=subprocess.run([self.codec.node,'-e',script],cwd=HELPER.parent,input=json.dumps(result).encode(),capture_output=True,timeout=10,check=True)
        decoded=json.loads(run.stdout)
        self.assertEqual(decoded['payload_hash'],result['payload_hash'])
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.public)).verify(bytes.fromhex(decoded['sig']),bytes.fromhex(result['payload_hash']))
        self.assertEqual(decoded['opcode'],0x63896e75)
        self.assertEqual(decoded['subwallet'],SUBWALLET['mainnet'])
        self.assertEqual(decoded['until'],1180);self.assertEqual(decoded['seqno'],7);self.assertEqual(decoded['mode'],3)
        self.assertEqual(decoded['amount'],'100000000');self.assertEqual(decoded['dest'],DEST);self.assertFalse(decoded['bounce'])
        self.assertEqual(decoded['commentOpcode'],0);self.assertEqual(decoded['comment'],'accmanager:'+'b'*32)
        self.assertEqual(decoded['envType'],'external-in');self.assertEqual(decoded['source'],self.owner['source'])
        self.assertTrue(decoded['sameBody']);self.assertTrue(decoded['noInit'])

    def test_attach_rejects_fake_signature_and_private_unknown_fields(self):
        request=self.codec.prepare(network='mainnet',anchor_public_key=self.anchor,seqno=7,**self.transfer)['request']
        with self.assertRaises(WalletCodecError):
            self.codec._run({**request,'op':'attach','signature':'00'*64,'signing_public_key':self.public})
        with self.assertRaises(WalletCodecError): self.codec._run({**request,'seed':'forbidden'})


class RuntimeFailureTests(unittest.TestCase):
    def test_missing_node_has_no_installation_or_crypto_fallback(self):
        codec=WalletTgCodec(node='/definitely-not-a-runtime',clock=lambda:1000)
        with self.assertRaises(WalletCodecError): codec.derive(ANCHOR_REFERENCE,'mainnet')


if __name__=='__main__': unittest.main()
