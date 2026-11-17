"""Explicit, query-pinned public WalletTg observations. No signatures/transactions.

Provider-reported data, NOT a lite-client proof. HTTP methods are fixed metadata
reads; runGetMethod POST is never a broadcast or arbitrary getter gateway.
"""
import asyncio
import json
import time

import httpx

from core.ton_address import normalize_address
from modules.money_reconciliation import _hash
from modules.ton_readonly import _unique, _unsigned, parse_account, ReadError, toncenter_headers
from modules.wallet_tg_codec import WalletTgCodec, WalletCodecError


class MoneyReadError(ValueError):
    def __init__(self, message='Money provider request failed', *, status_code=None, retryable=False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _bounded_provider_error(raw):
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            err = parsed.get('error') or parsed.get('message')
            if isinstance(err, str) and err.strip():
                cleaned = ' '.join(err.split())[:140]
                return ''.join(ch for ch in cleaned if 32 <= ord(ch) < 127)
    except Exception:
        pass
    return ''


async def bounded_json(http, method, url, *, params=None, body=None):
    headers={'Accept-Encoding':'identity','User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36','Accept':'application/json, text/plain, */*'}
    if url.startswith('https://toncenter.com/') or url.startswith('https://testnet.toncenter.com/'):
        headers = toncenter_headers(headers)
    async with http.stream(method,url,params=params,json=body,
            headers=headers,follow_redirects=False) as response:
        if response.status_code!=200 or response.headers.get('content-encoding','identity').lower()!='identity':
            detail = ''
            if response.status_code != 200 and response.headers.get('content-encoding', 'identity').lower() == 'identity':
                err_chunks, err_size = [], 0
                async for chunk in response.aiter_bytes():
                    err_size += len(chunk)
                    if err_size > 4096:
                        break
                    err_chunks.append(chunk)
                detail = _bounded_provider_error(b''.join(err_chunks))
            retryable = response.status_code in {429, 500, 502, 503, 504}
            msg = f'Money provider HTTP {response.status_code}' + (f': {detail}' if detail else '')
            raise MoneyReadError(msg, status_code=response.status_code, retryable=retryable)
        data,size=[],0
        async for chunk in response.aiter_bytes():
            size+=len(chunk)
            if size>1024*1024: raise MoneyReadError('Money provider response exceeds limit')
            data.append(chunk)
        try:
            value=json.loads(b''.join(data),object_pairs_hook=_unique)
        except (ValueError,UnicodeError,ReadError,RecursionError):
            raise MoneyReadError('Invalid/duplicate Money provider JSON') from None
        if not isinstance(value,dict): raise MoneyReadError('Invalid Money provider envelope')
        return value


def result_object(value):
    if value.get('ok') is not True or not isinstance(value.get('result'),dict):
        raise MoneyReadError('Money provider did not return a successful result')
    return value['result']


def getter(value,bits):
    result=result_object(value)
    stack=result.get('stack')
    if (type(result.get('exit_code')) is not int or result['exit_code']!=0 or not isinstance(stack,list) or len(stack)!=1
            or not isinstance(stack[0],list) or len(stack[0])!=2 or stack[0][0]!='num' or not isinstance(stack[0][1],str)):
        raise MoneyReadError('Invalid WalletTg getter result')
    import re
    text=stack[0][1]
    if not re.fullmatch(r'(0x[0-9a-fA-F]+|0|[1-9][0-9]*)',text):
        raise MoneyReadError('WalletTg getter must return a nonnegative integer')
    number=int(text,16 if text.startswith('0x') else 10)
    if not 0<=number<2**bits: raise MoneyReadError('WalletTg getter exceeds its width')
    return number


class TonCenterWalletReader:
    def __init__(self, *, client=None, codec=None, clock=time.time):
        self.client,self.clock=client,clock
        self.codec=codec or WalletTgCodec(clock=clock)

    async def read(self, address, network, anchor_public_key, *, confirmed=False, allow_deployment=False):
        if type(allow_deployment) is not bool: raise MoneyReadError('Explicit deployment inspection choice required')
        if confirmed is not True: raise MoneyReadError('Explicit public-address provider read confirmation required')
        source=normalize_address(address,network).raw
        derived=self.codec.derive(anchor_public_key,network)
        if derived['source']!=source: raise MoneyReadError('Original anchor does not derive this source')
        host='toncenter.com' if network=='mainnet' else 'testnet.toncenter.com'
        async def read(http):
            master=await bounded_json(http,'GET',f'https://{host}/api/v3/masterchainInfo')
            last=master.get('last')
            if (not isinstance(last,dict) or last.get('workchain')!=-1 or last.get('shard')!='8000000000000000'
                    or type(last.get('seqno')) is not int or not 0<=last['seqno']<2**31):
                raise MoneyReadError('Incomplete masterchain context')
            block=last['seqno']; block_hash=_hash(last.get('root_hash'))
            generated=int(_unsigned(last.get('gen_utime'),bits=32))
            if not -15<=self.clock()-generated<=120: raise MoneyReadError('Masterchain observation is stale/future')
            async def request(method,path,*,params=None,body=None):
                await asyncio.sleep(1.1)
                return await bounded_json(http,method,f'https://{host}/api/v2/{path}',params=params,body=body)
            account=await request('GET','getAddressInformation',params={'address':source,'seqno':block})
            parsed=parse_account(json.dumps(account).encode(),now=int(self.clock()))
            last_transaction={'lt':parsed['last_transaction']['lt'],'hash':_hash(parsed['last_transaction']['hash'])}
            initial=parsed['chain_state']=='uninit' and allow_deployment
            if parsed['chain_state']!='active' and not initial: raise MoneyReadError('Source is not active; no deployment fallback')
            state=result_object(account)
            if 'address' in state and normalize_address(state['address'],network).raw!=source:
                raise MoneyReadError('Wrong provider account')
            config=result_object(await request('GET','getConfigParam',params={'param':-123,'seqno':block}))
            cell=config.get('config')
            if not isinstance(cell,dict) or not isinstance(cell.get('bytes'),str):
                raise MoneyReadError('Missing shared WalletTg configuration cell')
            if initial:
                if (any(state.get(k) not in (None,'') for k in ('code','data','frozen_hash'))
                        or int(parsed['balance_nano'])<=0):
                    raise MoneyReadError('Initial source must be funded and code-less; no frozen/unknown deployment')
                inspected=self.codec.inspect_initial(network=network,anchor_public_key=anchor_public_key,implementation_boc=cell['bytes'])
                if not -15<=self.clock()-generated<=120: raise MoneyReadError('Initial observation became stale')
                return {**inspected,'network':network,'address':source,'active':False,'chain_state':'uninit',
                    'code_hash':None,'data_hash':None,'balance_nano':parsed['balance_nano'],
                    'last_transaction':last_transaction,'checked_at':self.clock(),
                    'mc_block_seqno':block,'block_id':block_hash,'block_time':generated,'provider':'toncenter',
                    'verification':'provider_reported_not_chain_proof','coherence':'queries_pinned_by_mc_seqno',
                    'public_key_origin':'initial_anchor_not_chain_getter','production_signing':False}
            values={}
            for name,bits in (('seqno',32),('get_subwallet_id',32),('get_public_key',256)):
                values[name]=getter(await request('POST','runGetMethod',body={'address':source,'method':name,'stack':[],'seqno':block}),bits)
            inspected=self.codec.inspect_state(network=network,anchor_public_key=anchor_public_key,
                code_boc=state.get('code'),data_boc=state.get('data'),implementation_boc=cell['bytes'])
            if (inspected['seqno']!=values['seqno'] or inspected['subwallet_id']!=values['get_subwallet_id']
                    or inspected['public_key']!=format(values['get_public_key'],'064x')):
                raise MoneyReadError('Account storage and pinned getters disagree')
            if not -15<=self.clock()-generated<=120: raise MoneyReadError('Observation became stale while reading')
            return {**inspected,'network':network,'address':source,'active':True,
                    'balance_nano':parsed['balance_nano'],'last_transaction':last_transaction,'checked_at':self.clock(),
                    'mc_block_seqno':block,'block_id':block_hash,'block_time':generated,
                    'provider':'toncenter','verification':'provider_reported_not_chain_proof',
                    'coherence':'queries_pinned_by_mc_seqno','production_signing':False}
        try:
            async with asyncio.timeout(90):
                if self.client is not None: return await read(self.client)
                last_err=None
                for attempt in range(4):
                    try:
                        headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36','Accept':'application/json, text/plain, */*'}
                        async with httpx.AsyncClient(headers=headers,timeout=httpx.Timeout(15,connect=8),trust_env=False,follow_redirects=False) as http:
                            return await read(http)
                    except (MoneyReadError,httpx.HTTPError,TimeoutError) as exc:
                        if isinstance(exc,MoneyReadError) and not exc.retryable:
                            raise
                        last_err=exc
                        if attempt<3: await asyncio.sleep(2.0*(attempt+1))
                if last_err is not None:
                    raise last_err
        except MoneyReadError: raise
        except (httpx.HTTPError,TimeoutError,ValueError,TypeError,KeyError,ReadError,WalletCodecError,RecursionError):
            raise MoneyReadError('Pinned Money state read failed/unsupported; no signing authorized') from None
