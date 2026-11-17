"""Explicit single-session Money metadata. DPAPI/signing/sending not implemented here."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid

from core.telegram_client import create_client
from modules.accounts import AccountService
from modules.asset_registry import account_context


class NativeMoneyError(ValueError):
    pass


class NativeMoneyService:
    def __init__(self, config, *, accounts=None, client_factory=create_client, clock=time.time):
        self.config, self.clock, self.factory = config, clock, client_factory
        self.accounts = accounts if accounts is not None else AccountService(config)
        self.path = Path(config.data_dir) / 'finance_native.sqlite3'

    def database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=5000')
        db.execute('PRAGMA foreign_keys=ON')
        mode = db.execute('PRAGMA journal_mode').fetchone()
        if not mode or str(mode[0]).lower() != 'wal':
            db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE IF NOT EXISTS observations(account TEXT PRIMARY KEY, context TEXT, checked REAL, payload TEXT, error TEXT, due REAL, lease TEXT)')
        if 'lease' not in {row[1] for row in db.execute('PRAGMA table_info(observations)')}:
            db.execute('ALTER TABLE observations ADD COLUMN lease TEXT')
        db.commit()
        return db

    async def action(self, operation, payload, *, scope_ids, ui_context, scope_check=lambda: True):
        schema = {'native-status': {'account_id', 'confirmed'},
                  'native-resolve': {'account_id', 'target_account_id', 'prepare_address', 'confirmed'},
                  'native-last': {'account_id'}}
        if operation not in schema or not isinstance(payload, dict) or set(payload) - schema[operation]:
            raise NativeMoneyError('Unsupported native request; keys/BOC/arbitrary RPC not accepted')
        source_id = payload.get('account_id')
        if not isinstance(source_id, str) or source_id not in scope_ids or not scope_check():
            raise NativeMoneyError('Choose one account within current filters')
        source = self.accounts.get_account(source_id)
        if not source.enabled or type(source.user_id) is not int or source.user_id <= 0:
            raise NativeMoneyError('Source must be enabled and identity-checked')
        context = account_context(source)
        if operation == 'native-last':
            with closing(self.database()) as db:
                row = db.execute('SELECT * FROM observations WHERE account=?', (source_id,)).fetchone()
            if not row or row['context'] != context:
                return {'native': None}
            return {'native': json.loads(row['payload']) if row['payload'] else None,
                    'checked_at': row['checked'], 'error': row['error'],
                    'stale': bool(row['error']) or row['checked'] is None or not 0 <= self.clock() - row['checked'] <= 300}
        if payload.get('confirmed') is not True:
            raise NativeMoneyError('Explicit Telegram session access confirmation required')
        target, target_context, target_username = None, None, None
        if operation == 'native-resolve':
            target_id = payload.get('target_account_id')
            if not isinstance(target_id, str) or target_id not in scope_ids or target_id == source_id:
                raise NativeMoneyError('Choose a distinct recipient from current filtered accounts')
            if type(payload.get('prepare_address')) is not bool:
                raise NativeMoneyError('Explicitly choose whether address preparation is allowed')
            target = self.accounts.get_account(target_id)
            if (not target.enabled or type(target.user_id) is not int or target.user_id <= 0 or target.user_id == source.user_id
                    or not isinstance(target.username, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', target.username)):
                raise NativeMoneyError('Recipient needs a known identity and current username; refresh its metadata first')
            target_context = account_context(target)
            target_username = target.username
        lease = uuid.uuid4().hex
        with closing(self.database()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT due FROM observations WHERE account=?', (source_id,)).fetchone()
            if row and row['due'] > self.clock():
                raise NativeMoneyError('Native session cooldown; retry explicitly later')
            db.execute('INSERT INTO observations(account,context,due,lease) VALUES(?,?,?,?) ON CONFLICT(account) DO UPDATE SET due=excluded.due,lease=excluded.lease',
                       (source_id, context, self.clock() + 60, lease))

        def check():
            if account_context(self.accounts.get_account(source_id)) != context or not scope_check():
                raise NativeMoneyError('Account/group/filter changed; native result discarded')
            if target:
                current = self.accounts.get_account(target.id)
                if account_context(current) != target_context or current.username != target_username:
                    raise NativeMoneyError('Recipient changed; native result discarded')

        try:
            async with asyncio.timeout(50):
                async with self.factory(self.config, source) as client:
                    me = await client.get_me()
                    if me.get('user_id') != source.user_id:
                        raise NativeMoneyError('Session identity mismatch; no Money request made')
                    check()
                    status = await client.native_money_state()
                    check()
                    result = {'source_account_id': source_id, 'state': status, 'gasless': None,
                              'recipient': None, 'network': 'mainnet', 'signing': 'unsupported',
                              'send_all': 'requires_emulation_and_signer', 'ui_context': ui_context}
                    if operation == 'native-status' and status['availability'] == 'ready':
                        result['gasless'] = await client.native_money_gasless()
                    elif operation == 'native-resolve':
                        if status['availability'] != 'ready' or int(status.get('balance_nano') or '0') <= 0:
                            raise NativeMoneyError('Sender Money must be ready and funded before recipient lookup')
                        check()
                        def admission():
                            check()
                            with closing(self.database()) as db:
                                current=db.execute('SELECT lease,due FROM observations WHERE account=?',(source_id,)).fetchone()
                            if not current or current['lease']!=lease or current['due']<=self.clock():
                                raise NativeMoneyError('Native observation lease changed/expired; preparation cancelled')
                            return True
                        result['recipient'] = {**await client.native_money_recipient(target_username, target.user_id,
                            prepare_address=payload['prepare_address'],admission_check=admission), 'account_id': target.id, 'username': target_username}
                    check()
            check()
            with closing(self.database()) as db, db:
                saved = db.execute('UPDATE observations SET context=?,checked=?,payload=?,error=NULL,due=?,lease=NULL WHERE account=? AND lease=? AND due>?',
                           (context, self.clock(), json.dumps(result, sort_keys=True), self.clock() + 10, source_id, lease, self.clock()))
                if saved.rowcount != 1:
                    raise NativeMoneyError('Native observation lease expired/replaced; late result discarded')
            return {'native': result, 'checked_at': self.clock(), 'stale': False}
        except asyncio.CancelledError:
            self.failed(source_id, lease, 'CancelledError')
            raise
        except Exception as exc:
            seconds = getattr(exc, 'value', None) or getattr(exc, 'seconds', None)
            cooldown = max(60, seconds) if type(seconds) is int and 0 < seconds < 2**31 else 60
            self.failed(source_id, lease, type(exc).__name__, cooldown)
            if isinstance(exc, NativeMoneyError):
                raise
            raise NativeMoneyError(f'Native Money request failed ({type(exc).__name__}); no transfer sent. API/layer availability is not live-verified.') from None

    def failed(self, account_id, lease, reason, cooldown=60):
        with closing(self.database()) as db, db:
            db.execute('UPDATE observations SET error=?,due=?,lease=NULL WHERE account=? AND lease=?',
                       (reason, self.clock() + cooldown, account_id, lease))
