"""Offline-only single-native-message WalletTg codec/fixture signer.

No RPC, broadcast, HTTP route, production arming, arbitrary hash signing, mnemonic
derivation or rotation. Mode 130 is unsigned emulation only, never signed here.
Private seeds never cross Node. Caller snapshots are NOT chain proof. Production
policy/emulation/signing integration remains disconnected.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.ton_address import normalize_address

CODE_HASH = '9149ae51c1e4689710cebf7830297b16acfbadb363a920a537893e7ffeeca768'
REV00_HASH = 'e30911420bef1191c09dce58b9df2b4ca4c2d9c383cc3b6a91170349ffa70e2c'
SUBWALLET = {'mainnet':0x7fff7f11,'testnet':0x7fff7ffd}
HELPER = Path(__file__).resolve().parents[1]/'ui'/'finance'/'wallet_tg_codec.cjs'


class WalletCodecError(ValueError):
    pass


def _hex(value, size):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{'+str(size*2)+'}',value):
        raise WalletCodecError('Invalid public WalletTg identity/hash')
    return value


def _uint(value):
    if type(value) is not int or not 0 <= value < 2**32:
        raise WalletCodecError('WalletTg seqno/header must be uint32')
    return value


class WalletTgCodec:
    def __init__(self, *, node=None, clock=time.time):
        self.node = node or shutil.which('node')
        self.clock = clock
        self._derived = {}

    def _run(self, payload):
        if not self.node or not Path(self.node).is_file() or not HELPER.is_file():
            raise WalletCodecError('Pinned local Node/TON codec unavailable; no installation/CDN fallback')
        # The subprocess sees public keys, transfer fields and signatures only.
        # NODE_OPTIONS/NODE_PATH are not inherited (no preload hook).
        env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','LANG'}}
        try:
            result=subprocess.run([str(self.node),'--max-old-space-size=128',str(HELPER)],
                input=json.dumps(payload,separators=(',',':')).encode(),capture_output=True,
                timeout=10,env=env,check=False,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            if result.returncode != 0 or len(result.stdout)>65536:
                err_text=(result.stderr or b'').decode('ascii',errors='ignore').strip().splitlines()
                detail=err_text[-1][:120] if err_text else ''
                if detail.startswith('WalletTg codec rejected input:'):
                    raise WalletCodecError(f'{detail}; no unsafe fallback')
                raise WalletCodecError('WalletTg codec failed; no unsafe fallback')
            value=json.loads(result.stdout)
            if not isinstance(value,dict):
                raise WalletCodecError('Invalid local codec response')
            return value
        except WalletCodecError:
            raise
        except (OSError,subprocess.TimeoutExpired,UnicodeError,json.JSONDecodeError):
            raise WalletCodecError('Local WalletTg codec unavailable/failed') from None

    def derive(self, anchor_public_key, network):
        _hex(anchor_public_key,32)
        if not isinstance(network,str) or network not in SUBWALLET:
            raise WalletCodecError('Choose an explicit WalletTg network')
        if not self.node or not Path(self.node).is_file() or not HELPER.is_file():
            raise WalletCodecError('Pinned local Node/TON codec unavailable; no installation/CDN fallback')
        key=(anchor_public_key,network)
        if key not in self._derived:
            subwallet = SUBWALLET[network]
            data_repr = b'\x00\x52\x00\x00\x00\x00\x00' + subwallet.to_bytes(4, 'big') + bytes.fromhex(anchor_public_key)
            data_hash = hashlib.sha256(data_repr).digest()
            init_repr = b'\x02\x01\x34\x00\x00\x00\x00' + bytes.fromhex(CODE_HASH) + data_hash
            source = '0:' + hashlib.sha256(init_repr).hexdigest()
            result = {'source': source, 'subwallet_id': subwallet, 'trampoline_hash': CODE_HASH}
            if len(self._derived)>=4096: self._derived.clear()
            self._derived[key]=result
        return dict(self._derived[key])

    def prepare(self, *, network, anchor_public_key, seqno, valid_until, destination, amount_nano, intent_id, channel='external', mode=3, deploy=False, allow_high_caps=False):
        _uint(seqno); _uint(valid_until); _hex(intent_id,16)
        if type(allow_high_caps) is not bool:
            raise WalletCodecError('Explicit cap tier choice required')
        if not 30 <= valid_until-self.clock() <= 300:
            raise WalletCodecError('WalletTg request needs a fresh 30–300 second expiry')
        if channel not in {'external','internal'} or type(mode) is not int or mode != 3:
            raise WalletCodecError('Only single native mode-3 messages supported; no destroy/send-all/rotation')
        if type(deploy) is not bool or deploy and (channel!='external' or seqno!=0):
            raise WalletCodecError('Deployment requires explicit external seqno-zero initial WalletTg')
        destination=normalize_address(destination,network).raw
        max_nano=100*10**9 if allow_high_caps else 10**9
        pattern='[1-9][0-9]{0,11}' if allow_high_caps else '[1-9][0-9]{0,9}'
        if not isinstance(amount_nano,str) or not re.fullmatch(pattern,amount_nano) or int(amount_nano)>max_nano:
            raise WalletCodecError('Bounded native amount must be an exact positive nano string')
        self.derive(anchor_public_key,network)
        request={'op':'prepare','network':network,'anchor_public_key':anchor_public_key,'seqno':seqno,
                 'valid_until':valid_until,'destination':destination,'amount_nano':amount_nano,'intent_id':intent_id,'channel':channel,'mode':mode}
        if deploy: request['deploy']=True
        if allow_high_caps: request['allow_high_caps']=True
        result=self._run(request)
        return {'request':request,'codec':result,'production_signing':False,'broadcast_supported':False}

    def prepare_drain(self, *, network, anchor_public_key, seqno, valid_until, destination, intent_id, deploy=False):
        """Dedicated external mode 130 codec; caller must enforce policy/emulation.

        No arbitrary mode, deployment, amount or key; ordinary prepare/fixture
        signing remain mode 3 only. No authorization is granted by this codec.
        """
        _uint(seqno);_uint(valid_until);_hex(intent_id,16)
        if not 30<=valid_until-self.clock()<=300:
            raise WalletCodecError('Drain requires fresh bounded expiry')
        if type(deploy) is not bool or deploy and seqno!=0:
            raise WalletCodecError('Drain deployment requires seqno-zero initial WalletTg')
        self.derive(anchor_public_key,network)
        request={'op':'drain-prepare','network':network,'anchor_public_key':anchor_public_key,
            'seqno':seqno,'valid_until':valid_until,'destination':normalize_address(destination,network).raw,
            'amount_nano':'0','intent_id':intent_id,'channel':'external','mode':130}
        if deploy: request['deploy']=True
        return {'request':request,'codec':self._run(request),'production_signing':False,'broadcast_supported':False}

    def inspect_state(self, *, network, anchor_public_key, code_boc, data_boc, implementation_boc):
        self.derive(anchor_public_key,network)
        return self._run({'op':'inspect-state','network':network,'anchor_public_key':anchor_public_key,
                         'code_boc':code_boc,'data_boc':data_boc,'implementation_boc':implementation_boc})

    def inspect_initial(self, *, network, anchor_public_key, implementation_boc):
        self.derive(anchor_public_key,network)
        return self._run({'op':'inspect-initial','network':network,'anchor_public_key':anchor_public_key,'implementation_boc':implementation_boc})

    def inspect_external(self, *, external_boc, signing_public_key, network):
        _hex(signing_public_key, 32)
        if not isinstance(network, str) or network not in SUBWALLET:
            raise WalletCodecError('Choose an explicit WalletTg network')
        if not isinstance(external_boc, str) or not external_boc:
            raise WalletCodecError('Signed external BOC required')
        return self._run({'op':'inspect-external','network':network,'external_boc':external_boc,'signing_public_key':signing_public_key})

    def prepare_emulation(self, *, network, anchor_public_key, seqno, valid_until, destination, amount_nano, intent_id, send_all=False, deploy=False, allow_deploy_drain=False, allow_high_caps=False):
        if type(send_all) is not bool:
            raise WalletCodecError('Explicit send-all choice required')
        if not send_all:
            prepared=self.prepare(network=network,anchor_public_key=anchor_public_key,seqno=seqno,
                valid_until=valid_until,destination=destination,amount_nano=amount_nano,intent_id=intent_id,deploy=deploy,allow_high_caps=allow_high_caps)
            request=prepared['request']
        else:
            if deploy is not False and not (allow_deploy_drain and deploy is True and seqno==0):
                raise WalletCodecError('Initial deployment cannot drain the source')
            self.derive(anchor_public_key,network);_uint(seqno);_uint(valid_until);_hex(intent_id,16)
            if amount_nano!='0' or not 30 <= valid_until-self.clock() <= 300:
                raise WalletCodecError('Emulation drain uses zero nominal value and fresh expiry')
            request={'op':'emulate','network':network,'anchor_public_key':anchor_public_key,'seqno':seqno,
                     'valid_until':valid_until,'destination':normalize_address(destination,network).raw,
                     'amount_nano':'0','intent_id':intent_id,'channel':'external','mode':130}
            if deploy: request['deploy']=True
        result=self._run({**request,'op':'emulate'})
        if result.get('signature_valid') is not False or result.get('broadcastable') is not False:
            raise WalletCodecError('Pre-sign emulation must never contain a valid signature')
        return result

    def validate_fixture_snapshot(self, identity, anchor_public_key, snapshot):
        if not isinstance(identity,dict) or set(identity)!={'account','user_id','context','network','address','public_key','revision'}:
            raise WalletCodecError('Exact stored financial key identity required')
        required={'network','address','active','code_hash','implementation_hash','subwallet_id','public_key','seqno','checked_at','block_id','evidence'}
        if not isinstance(snapshot,dict) or set(snapshot)!=required or snapshot['evidence']!='offline_fixture':
            raise WalletCodecError('Only explicit offline fixture snapshots accepted; no production verifier exists yet')
        network=identity['network']
        derived=self.derive(anchor_public_key,network)
        if (snapshot['network']!=network or snapshot['address']!=identity['address'] or derived['source']!=identity['address']
                or snapshot['active'] is not True or snapshot['code_hash']!=CODE_HASH or snapshot['implementation_hash']!=REV00_HASH
                or type(snapshot['subwallet_id']) is not int or snapshot['subwallet_id']!=SUBWALLET[network]
                or snapshot['public_key']!=identity['public_key']):
            raise WalletCodecError('WalletTg snapshot/anchor/current key mismatch or unknown implementation')
        _uint(snapshot['seqno']); _hex(snapshot['block_id'],32)
        if type(snapshot['checked_at']) not in (int,float) or not 0 <= self.clock()-snapshot['checked_at'] <= 30:
            raise WalletCodecError('WalletTg observation is stale/future')
        return derived

    def sign_fixture(self, vault, identity, *, anchor_public_key, snapshot, transfer):
        """Disconnected test primitive. Not wired to any finance action or journal transition."""
        allowed={'destination','amount_nano','intent_id','valid_until','channel','mode'}
        required={'destination','amount_nano','intent_id','valid_until'}
        if not isinstance(transfer,dict) or set(transfer)-allowed or not required <= set(transfer):
            raise WalletCodecError('No arbitrary payload/hash/key/BOC signing')
        if not isinstance(identity,dict) or not isinstance(snapshot,dict):
            raise WalletCodecError('Exact offline identity/snapshot required')
        identity,snapshot,transfer=dict(identity),dict(snapshot),dict(transfer)
        derived=self.validate_fixture_snapshot(identity,anchor_public_key,snapshot)
        prepared=self.prepare(network=identity['network'],anchor_public_key=anchor_public_key,seqno=snapshot['seqno'],**transfer)
        if prepared['codec']['source']!=derived['source']:
            raise WalletCodecError('Source changed while preparing fixture')
        payload_hash=bytes.fromhex(_hex(prepared['codec']['payload_hash'],32))
        # Recheck expiry/freshness after local serialization, before key access.
        self.validate_fixture_snapshot(identity,anchor_public_key,snapshot)
        if prepared['request']['valid_until']<=self.clock():
            raise WalletCodecError('Fixture request expired before signing')
        with vault.use_seed(**identity) as seed:
            signature=Ed25519PrivateKey.from_private_bytes(seed).sign(payload_hash)
        attached=self._run({**prepared['request'],'op':'attach','signature':signature.hex(),'signing_public_key':identity['public_key']})
        if attached['payload_hash']!=prepared['codec']['payload_hash']:
            raise WalletCodecError('Signed fixture does not match prepared payload')
        return {**attached,'production_signing':False,'broadcast_supported':False,'verification':'offline_fixture_not_chain_proof'}
