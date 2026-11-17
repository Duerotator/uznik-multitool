"""Non-custodial TON Connect intents. No seeds, signer, broadcasts or auto-retries."""
import asyncio
import base64
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import time
from urllib.parse import urlsplit
import zlib

import httpx

from core.ton_address import normalize_address, friendly_address, format_nano
from modules.accounts import AccountService
from modules.asset_registry import account_context
from modules.ton_readonly import TonCenterReader, ReadError, _unique, _unsigned, toncenter_headers
from modules.money_reconciliation import reconcile, ReconciliationError

CHAIN = {"mainnet": "-239", "testnet": "-3"}
SDK_PATH = Path(__file__).resolve().parents[1]/"ui"/"finance"/"dist"/"money.js"


class FinanceError(ValueError):
    pass


def public_origin():
    value = os.getenv("TGB_FINANCE_PUBLIC_URL", "").rstrip("/")
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.path or url.query or url.fragment or url.port not in (None, 443)
            or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", url.hostname)):
        raise FinanceError("Configure TGB_FINANCE_PUBLIC_URL as the public HTTPS origin (no path/query)")
    return 'https://'+url.hostname.lower()


def settings():
    try:
        origin = public_origin()
    except (FinanceError, ValueError):
        origin = None
    return {"public_origin": origin, "sdk_ready": SDK_PATH.is_file(),
            "mainnet_enabled": os.getenv("TGB_FINANCE_ENABLE_MAINNET") == "1",
            "max_transfer_nano": "1000000000", "daily_limit_nano": "5000000000",
            "custody": "external_wallet", "telegram_link": "operator_declared",
            "activation": "unsupported", "auto_sweep": "unsupported"}


def manifest():
    origin = public_origin()
    return {"url": origin, "name": "Accmanager Money", "iconUrl": origin+"/finance/icon.png"}


def icon_png():
    # Code-native app mark, no external asset fetch or Telegram impersonation.
    pixels = bytearray()
    for y in range(180):
        pixels.append(0)
        for x in range(180):
            mark = 36 <= y <= 143 and (34 <= x <= 49 or 130 <= x <= 145 or abs(x-y) < 9 and x < 91 or abs(180-x-y) < 9 and x > 89)
            pixels.extend((233, 240, 248) if mark else (47, 115, 188))
    def chunk(kind, data):
        return struct.pack('!I', len(data))+kind+data+struct.pack('!I', zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('!2I5B',180,180,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(bytes(pixels)))+chunk(b'IEND',b'')


def amount_nano(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,9})?", value):
        raise FinanceError("Enter a positive decimal amount with at most 9 fractional digits; no exponent or float")
    whole, _, fraction = value.partition('.')
    amount = int(whole)*10**9+int(fraction.ljust(9,'0') or '0')
    if not 0 < amount <= 10**9:
        raise FinanceError("Initial module limit: more than zero and at most 1 native coin per transfer")
    return str(amount)


def transaction_page(raw, *, limit=20, network='mainnet'):
    if network not in {'mainnet', 'testnet'}:
        raise FinanceError("Unsupported history network")
    if len(raw) > 1024*1024:
        raise FinanceError("History response exceeds size limit")
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(data, dict) or data.get('ok') is not True or not isinstance(data.get('result'), list) or len(data['result']) > limit:
            raise ValueError()
        result = []
        for tx in data['result']:
            ident = tx['transaction_id']
            lt = _unsigned(ident['lt'], bits=64)
            hash_value = ident['hash']
            if not isinstance(hash_value,str) or len(base64.b64decode(hash_value, validate=True)) != 32:
                raise ValueError()
            if type(tx['utime']) is not int or not 0 <= tx['utime'] < 2**63:
                raise ValueError()
            incoming = tx.get('in_msg')
            outgoing = tx.get('out_msgs')
            if not isinstance(incoming,dict) or not isinstance(outgoing,list) or len(outgoing)>256:
                raise ValueError()
            def msg(item):
                def address(key):
                    value = item.get(key)
                    return normalize_address(value, network).raw if value else None
                return {'source':address('source'),'destination':address('destination'),'amount_nano':_unsigned(item['value'])}
            result.append({'lt':lt,'hash':hash_value,'utime':tx['utime'],'fee_nano':_unsigned(tx['fee']),
                           'incoming':msg(incoming),'outgoing':[msg(item) for item in outgoing]})
        return result
    except (KeyError, ValueError, TypeError, UnicodeError, ReadError, RecursionError):
        raise FinanceError("Incomplete/unsupported provider history response") from None


def _tonapi_history_page(raw, *, limit=20, network='mainnet'):
    if network not in {'mainnet', 'testnet'} or len(raw) > 1024*1024:
        raise FinanceError("Incomplete/unsupported provider history response")
    try:
        data = json.loads(raw, object_pairs_hook=_unique)
        rows = data.get('transactions') if isinstance(data, dict) else None
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError()
        result = []
        for tx in rows:
            if not isinstance(tx, dict):
                raise ValueError()
            lt = _unsigned(tx.get('lt'), bits=64)
            hx = tx.get('hash')
            if not isinstance(hx, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', hx):
                raise ValueError()
            hash_value = base64.b64encode(bytes.fromhex(hx)).decode()
            utime = tx.get('utime')
            if type(utime) is not int or not 0 <= utime < 2**63:
                raise ValueError()
            incoming = tx.get('in_msg') or {}
            outgoing = tx.get('out_msgs') or []
            if not isinstance(incoming, dict) or not isinstance(outgoing, list) or len(outgoing) > 256:
                raise ValueError()
            def msg(item):
                def address(key):
                    val = item.get(key)
                    if isinstance(val, dict):
                        val = val.get('address')
                    return normalize_address(val, network).raw if val else None
                return {'source': address('source'), 'destination': address('destination'), 'amount_nano': _unsigned(item.get('value', 0))}
            result.append({'lt': lt, 'hash': hash_value, 'utime': utime, 'fee_nano': _unsigned(tx.get('total_fees', 0)),
                           'incoming': msg(incoming), 'outgoing': [msg(item) for item in outgoing]})
        return result
    except (KeyError, ValueError, TypeError, UnicodeError, ReadError, RecursionError):
        raise FinanceError("Incomplete/unsupported provider history response") from None


async def read_history(address, network, *, client=None):
    address = normalize_address(address, network).raw
    host = 'toncenter.com' if network == 'mainnet' else 'testnet.toncenter.com'
    tonapi_host = 'tonapi.io' if network == 'mainnet' else 'testnet.tonapi.io'
    async def request_primary(http):
        async with http.stream('GET', f'https://{host}/api/v2/getTransactions', params={'address':address,'limit':20},
                               headers=toncenter_headers({'Accept-Encoding':'identity'}), follow_redirects=False) as response:
            if response.status_code != 200 or response.headers.get('content-encoding','identity').lower() != 'identity':
                raise FinanceError(f"History provider unavailable/rate limited (HTTP {response.status_code}); no automatic retry")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 1024*1024: raise FinanceError("History response exceeds size limit")
                chunks.append(chunk)
            return transaction_page(b''.join(chunks), network=network)
    async def request_tonapi(http):
        async with http.stream('GET', f'https://{tonapi_host}/v2/blockchain/accounts/{address}/transactions', params={'limit':20},
                               headers={'Accept-Encoding':'identity'}, follow_redirects=False) as response:
            if response.status_code != 200 or response.headers.get('content-encoding','identity').lower() != 'identity':
                raise FinanceError(f"History fallback provider unavailable (HTTP {response.status_code})")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 1024*1024: raise FinanceError("History response exceeds size limit")
                chunks.append(chunk)
            return _tonapi_history_page(b''.join(chunks), network=network)
    try:
        async with asyncio.timeout(25):
            if client is not None: return await request_primary(client)
            async with httpx.AsyncClient(timeout=httpx.Timeout(20,connect=5),trust_env=False,follow_redirects=False) as http:
                try:
                    return await request_primary(http)
                except (FinanceError, httpx.HTTPError, TimeoutError) as primary_err:
                    try:
                        return await request_tonapi(http)
                    except (FinanceError, httpx.HTTPError, TimeoutError):
                        if isinstance(primary_err, FinanceError):
                            raise primary_err from None
                        raise
    except (httpx.HTTPError, TimeoutError):
        raise FinanceError("History read failed/timed out") from None


class MoneyService:
    def __init__(self, config, *, accounts=None, balance=None, history=None, reconciliation=None, clock=time.time):
        self.config, self.clock = config, clock
        self.accounts = accounts if accounts is not None else AccountService(config)
        self.balance = balance or TonCenterReader().read
        self.history = history or read_history
        self.reconciliation = reconciliation or reconcile
        self.path = Path(config.data_dir)/'finance_money.sqlite3'
        self.lock = asyncio.Lock()
        self.due = 0

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(self.path,timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA busy_timeout=5000')
            db.execute('PRAGMA foreign_keys=ON')
            mode = db.execute('PRAGMA journal_mode').fetchone()
            if not mode or str(mode[0]).lower() != 'wal':
                db.execute('PRAGMA journal_mode=WAL')
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, token_hash TEXT, account TEXT, context TEXT, ui_context TEXT, network TEXT, source TEXT, destination TEXT, amount TEXT, created REAL, expires INTEGER, state TEXT, receipt_hash TEXT)')
                if 'prompted' not in {row[1] for row in db.execute('PRAGMA table_info(intents)')}:
                    db.execute('ALTER TABLE intents ADD COLUMN prompted REAL')
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS reconciliation(intent TEXT PRIMARY KEY REFERENCES intents(id), checked REAL NOT NULL, evidence TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS native_commitments(intent TEXT PRIMARY KEY, network TEXT NOT NULL, source TEXT NOT NULL, seqno INTEGER, max_debit TEXT NOT NULL, policy_id TEXT NOT NULL, policy_revision TEXT NOT NULL, key_revision TEXT NOT NULL, random_id INTEGER NOT NULL, stage TEXT NOT NULL, boc_hash TEXT, UNIQUE(network,source,seqno))')
                yield db

    def native_executor(self,vault,**dependencies):
        """Local trusted-code integration, never an HTTP seed/policy injection API."""
        from modules.money_execution import NativeMoneyExecutor
        return NativeMoneyExecutor(self,vault,**dependencies)

    def reserved_debit(self,db,source,network):
        return sum(int(row['debit']) for row in db.execute("SELECT COALESCE(n.max_debit,i.amount) AS debit FROM intents i LEFT JOIN native_commitments n ON n.intent=i.id WHERE i.source=? AND i.network=? AND COALESCE(i.prompted,i.created)>=? AND i.state<>'prepared'",
            (source,network,self.clock()-86400)))

    def scope(self, account_id, ids, ui_context):
        if not isinstance(account_id,str) or account_id not in ids:
            raise FinanceError("Select exactly one enabled account within current filters")
        account = self.accounts.get_account(account_id)
        if not account.enabled or type(account.user_id) is not int or account.user_id <= 0:
            raise FinanceError("Select an enabled identity-checked account; wallet link is still only a declaration")
        return account_context(account), ui_context

    async def action(self, action, payload, *, scope_ids, ui_context, scope_check=lambda:True):
        schemas = {'read':{'account_id','source','network','confirmed'},
            'native-status':{'account_id','confirmed'},
            'native-resolve':{'account_id','target_account_id','prepare_address','confirmed'},
            'native-last':{'account_id'},
            'prepare':{'account_id','source','network','destination','amount','confirmed'},
            'prompt':{'account_id','id','review_token','confirmed'},
            'result':{'account_id','id','review_token','receipt_hash','outcome'},
            'resolve':{'account_id','id','source','network','decision','confirmation'},
            'reconcile':{'account_id','id','source','network','confirmed'},
            'journal':{'account_id'}}
        if action not in schemas or not isinstance(payload,dict) or set(payload)-schemas[action]:
            raise FinanceError("Unsupported finance request; no seed, key, raw BOC or arbitrary payload accepted")
        if self.lock.locked(): raise FinanceError("A finance operation is already running")
        async with self.lock:
            context, ui = self.scope(payload.get('account_id'),scope_ids,ui_context)
            account_id = payload['account_id']
            if not scope_check(): raise FinanceError("Group/filter changed")
            if action.startswith('native-'):
                from modules.money_native import NativeMoneyService, NativeMoneyError
                if not hasattr(self, '_native'):
                    self._native = NativeMoneyService(self.config, accounts=self.accounts, clock=self.clock)
                try:
                    return await self._native.action(action,payload,scope_ids=scope_ids,ui_context=ui_context,scope_check=scope_check)
                except NativeMoneyError as exc:
                    raise FinanceError(str(exc)) from None
            if action == 'journal':
                with self.db() as db:
                    rows = db.execute('SELECT id,network,source,destination,amount,created,expires,state,receipt_hash FROM intents WHERE account=? ORDER BY created DESC LIMIT 50',(account_id,)).fetchall()
                    result = []
                    for row in rows:
                        item = dict(row)
                        observation = db.execute('SELECT checked,evidence FROM reconciliation WHERE intent=?',(row['id'],)).fetchone()
                        item['reconciliation'] = {**json.loads(observation['evidence']), 'checked_at': observation['checked']} if observation else None
                        result.append(item)
                return {'journal':result}
            if action == 'reconcile':
                if payload.get('confirmed') is not True or not isinstance(payload.get('id'),str) or not re.fullmatch('[a-f0-9]{32}',payload['id']):
                    raise FinanceError('Explicit read-only reconciliation confirmation and exact ID required')
                with self.db() as db:
                    row = db.execute('SELECT * FROM intents WHERE id=? AND account=?',(payload['id'],account_id)).fetchone()
                    recovered = bool(row and row['state']=='operator_not_sent' and db.execute("SELECT 1 FROM native_commitments WHERE intent=? AND stage='expired_unused_nonce_observed'",(row['id'],)).fetchone())
                    if not row or (row['state'] not in {'wallet_prompted','wallet_submitted_unverified','uncertain'} and not recovered) or row['prompted'] is None:
                        raise FinanceError('Intent is not awaiting reconciliation')
                    if payload.get('network') != row['network'] or normalize_address(payload.get('source'),row['network']).raw != row['source']:
                        raise FinanceError('Use the exact original source wallet and network')
                    intent = dict(row)
                if self.clock() < self.due: raise FinanceError('Finance provider cooldown; retry explicitly later')
                self.due = self.clock()+5
                try:
                    evidence = await self.reconciliation(intent)
                except ReconciliationError as exc:
                    raise FinanceError(str(exc)) from None
                if self.scope(account_id,scope_ids,ui_context) != (context,ui) or not scope_check():
                    raise FinanceError('Account/group changed during reconciliation; result discarded')
                with self.db() as db:
                    current = db.execute('SELECT * FROM intents WHERE id=?',(row['id'],)).fetchone()
                    if dict(current) != intent:
                        raise FinanceError('Intent changed during reconciliation; stale result discarded')
                    if recovered:
                        previous=db.execute('SELECT evidence FROM reconciliation WHERE intent=?',(row['id'],)).fetchone()
                        prior=json.loads(previous['evidence']) if previous else {}
                        review=prior if prior.get('status')=='expired_unused_nonce_observed' else prior.get('prior_nonce_review')
                        if review is not None:
                            evidence={**evidence,'prior_nonce_review':review}
                    state = 'provider_delivered' if evidence['status'] == 'provider_delivered' else row['state']
                    db.execute('UPDATE intents SET state=? WHERE id=?',(state,row['id']))
                    db.execute('INSERT OR REPLACE INTO reconciliation(intent,checked,evidence) VALUES(?,?,?)',
                               (row['id'],self.clock(),json.dumps(evidence,sort_keys=True)))
                return {'state':state,'reconciliation':evidence,'automatic_retry':False}
            if action == 'resolve':
                if not isinstance(payload.get('id'),str) or not re.fullmatch('[a-f0-9]{32}',payload['id']) or payload.get('confirmation') != 'REVIEW '+payload['id']:
                    raise FinanceError("Manual review requires the exact intent ID confirmation")
                with self.db() as db:
                    row = db.execute('SELECT * FROM intents WHERE id=? AND account=?',(payload['id'],account_id)).fetchone()
                    if not row or row['state'] not in {'wallet_prompted','wallet_submitted_unverified','uncertain'}:
                        raise FinanceError("Intent is not awaiting external review")
                    if db.execute('SELECT 1 FROM native_commitments WHERE intent=?',(row['id'],)).fetchone() and self.clock()<=row['expires']+60:
                        raise FinanceError('Native request may still execute; wait for expiry + 60 seconds or use read-only reconciliation')
                    if payload.get('network') != row['network'] or normalize_address(payload.get('source'),row['network']).raw != row['source']:
                        raise FinanceError("Reconnect the exact source wallet and network before reviewing")
                    decision = payload.get('decision')
                    if decision not in {'confirmed_in_wallet','not_sent'}: raise FinanceError("Choose an external review result")
                    if decision == 'not_sent' and self.clock() <= row['expires']+60: raise FinanceError("Wait until the wallet request has expired + 60 seconds, then verify wallet and chain")
                    state = 'operator_confirmed' if decision == 'confirmed_in_wallet' else 'operator_not_sent'
                    db.execute('UPDATE intents SET state=? WHERE id=?',(state,row['id']))
                return {'state':state,'verification':'operator_attested_not_chain_proof','automatic_retry':False}
            if action in {'read','prepare'}:
                if payload.get('confirmed') is not True: raise FinanceError("Explicit provider-read confirmation required")
                network = payload.get('network')
                if not isinstance(network,str) or network not in CHAIN: raise FinanceError("Choose network explicitly")
                source = normalize_address(payload.get('source'),network).raw
                if action == 'prepare':
                    if not settings()['sdk_ready'] or not settings()['public_origin']: raise FinanceError("Build the pinned SDK and configure the public HTTPS manifest before transfers")
                    if network == 'mainnet' and not settings()['mainnet_enabled']: raise FinanceError("Mainnet transfers disabled; operator must explicitly enable them")
                    destination = normalize_address(payload.get('destination'),network).raw
                    amount = amount_nano(payload.get('amount'))
                    if source == destination: raise FinanceError("Self-transfer is not wallet activation; choose another recipient")
                if self.clock() < self.due: raise FinanceError("Finance provider cooldown; retry explicitly later")
                self.due = self.clock()+5
                observation = await self.balance(source, network)
                if self.scope(account_id,scope_ids,ui_context) != (context,ui) or not scope_check():
                    raise FinanceError("Account/group changed during provider read")
                if action == 'read':
                    await asyncio.sleep(1.2)  # Public TON Center requests are sequential, not a burst.
                    if not scope_check(): raise FinanceError("Scope changed before history read")
                    history = await self.history(source,network)
                    if not scope_check(): raise FinanceError("Scope changed during history read")
                    return {'balance':observation,'history':history,'source_ownership':'client_reported_not_verified',
                            'rewards_classification':'unsupported','history_complete':False}
                if int(observation['balance_nano']) < int(amount)+5_000_000:
                    raise FinanceError("Insufficient provider-reported balance for amount + minimum reserve; actual fees remain wallet-controlled")
                ident, token = secrets.token_hex(16), secrets.token_hex(24)
                expires = int(self.clock())+180
                with self.db() as db:
                    unresolved = db.execute("SELECT 1 FROM intents WHERE source=? AND network=? AND state IN ('wallet_prompted','wallet_submitted_unverified','uncertain')",(source,network)).fetchone()
                    if unresolved: raise FinanceError("Source has an active/uncertain intent; reconcile it before another transfer")
                    drafts = db.execute("SELECT count(*) FROM intents WHERE source=? AND network=? AND state='prepared' AND expires>?",(source,network,self.clock())).fetchone()[0]
                    if drafts >= 10: raise FinanceError("Too many active drafts for this source; wait for their 3-minute expiry")
                    db.execute('INSERT INTO intents(id,token_hash,account,context,ui_context,network,source,destination,amount,created,expires,state,receipt_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(ident,hashlib.sha256(token.encode()).hexdigest(),account_id,context,ui,network,source,destination,amount,self.clock(),expires,'prepared',None))
                return {'plan':{'id':ident,'review_token':token,'account_id':account_id,'network':network,
                    'source':source,'destination':friendly_address(destination,network),'destination_raw':destination,
                    'amount_nano':amount,'amount_native':format_nano(amount),'expires':expires,
                    'comment':'accmanager:'+ident,'fee':'unknown_wallet_confirmation_required','telegram_link':'operator_declared'}}
            token = payload.get('review_token')
            if not isinstance(token,str) or not re.fullmatch('[a-f0-9]{48}',token): raise FinanceError("Invalid review capability")
            with self.db() as db:
                row = db.execute('SELECT * FROM intents WHERE id=?',(payload.get('id'),)).fetchone()
                if not row or row['account'] != account_id or row['token_hash'] != hashlib.sha256(token.encode()).hexdigest() or row['context'] != context or row['ui_context'] != ui:
                    raise FinanceError("Intent/account/context changed; no wallet request authorized")
                if action == 'prompt':
                    if payload.get('confirmed') is not True or row['state'] != 'prepared' or row['expires'] <= self.clock():
                        raise FinanceError("Intent expired/already prompted; never automatically retry")
                    if row['network']=='mainnet' and not settings()['mainnet_enabled']: raise FinanceError("Mainnet was disabled")
                    other = db.execute("SELECT 1 FROM intents WHERE source=? AND network=? AND id<>? AND state IN ('wallet_prompted','wallet_submitted_unverified','uncertain')",(row['source'],row['network'],row['id'])).fetchone()
                    if other: raise FinanceError("Another intent already owns this source; no second wallet prompt")
                    native = db.execute("SELECT 1 FROM native_commitments n JOIN intents i ON i.id=n.intent WHERE n.source=? AND n.network=? AND n.stage<>'preflight_failed' AND i.created>=?",(row['source'],row['network'],self.clock()-86400)).fetchone()
                    if native: raise FinanceError('Native source/day reservation cannot mix with unknown TON Connect fees')
                    total = self.reserved_debit(db,row['source'],row['network'])
                    if total+int(row['amount']) > 5*10**9: raise FinanceError("Daily module reservation cap exceeded")
                    db.execute("UPDATE intents SET state='wallet_prompted',prompted=? WHERE id=?",(self.clock(),row['id']))
                    return {'authorized':True,'id':row['id'], 'comment':'accmanager:'+row['id'],
                            'transaction':{'validUntil':row['expires'],'network':CHAIN[row['network']],
                                'from':row['source'], 'messages':[{'address':friendly_address(row['destination'],row['network']),'amount':row['amount']}]}}
                receipt = payload.get('receipt_hash')
                if payload.get('outcome') not in {'wallet_returned','uncertain'} or receipt is not None and (not isinstance(receipt,str) or not re.fullmatch('[a-f0-9]{64}',receipt)):
                    raise FinanceError("Invalid receipt metadata")
                if payload.get('outcome')=='wallet_returned' and not receipt: raise FinanceError("Wallet receipt hash required")
                if row['state'] not in {'wallet_prompted','uncertain','wallet_submitted_unverified'}: raise FinanceError("Intent was never prompted")
                if row['receipt_hash'] and row['receipt_hash'] != receipt: raise FinanceError("Conflicting wallet receipt; manual reconciliation required")
                state = 'wallet_submitted_unverified' if receipt else 'uncertain'
                db.execute('UPDATE intents SET state=?, receipt_hash=? WHERE id=?',(state,receipt,row['id']))
                return {'state':state,'confirmed_on_chain':False,'automatic_retry':False}
