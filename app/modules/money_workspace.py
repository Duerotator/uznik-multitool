"""Local desktop Money setup + persistent policies and one-use exact reviews.

Never exposed through HTTP. Seed input goes directly to DPAPI, not task payloads.
Preview reads public/Telegram metadata and emulates unsigned; send is separate.
"""
import asyncio
from dataclasses import asdict
import hashlib
import json
import re
import secrets
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat

from core.telegram_client import create_client
from core.ton_address import friendly_address
from modules.financial_vault import FinancialVault
from modules.money_execution import NativeExecutionError,require
from modules.money_policy import make_policy,NativeSpendPolicy,quote


def amount(text):
    if not isinstance(text,str) or not re.fullmatch(r'(0|[1-9][0-9]{0,8})(\.[0-9]{1,9})?',text):
        raise NativeExecutionError('Use an exact decimal native amount with up to 9 fractional digits')
    whole,_,fraction=text.partition('.')
    return str(int(whole)*10**9+int(fraction.ljust(9,'0') or '0'))


class NativeMoneyWorkspace:
    def __init__(self,money,*,vault=None,**execution_dependencies):
        self.money=money;self.vault=vault or FinancialVault(money.config.data_dir)
        self.executor=money.native_executor(self.vault,**execution_dependencies)
        with money.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS native_key_links(account TEXT PRIMARY KEY, revision TEXT NOT NULL, anchor TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS native_wallet_mnemonics(account TEXT PRIMARY KEY, revision TEXT NOT NULL, wrapped BLOB NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS native_policies(account TEXT PRIMARY KEY, revision TEXT NOT NULL, enabled INTEGER NOT NULL, target TEXT NOT NULL, target_context TEXT NOT NULL, username TEXT NOT NULL, policy TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS native_reviews(token_hash TEXT PRIMARY KEY, account TEXT NOT NULL, policy_revision TEXT NOT NULL, key_revision TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL, review TEXT NOT NULL)')

    def _scope(self,source,scope_ids,ui_context):
        require(isinstance(ui_context,str) and re.fullmatch('[a-f0-9]{64}',ui_context))
        return self.money.scope(source,scope_ids,ui_context)

    def _key(self,source,context):
        records=[r for r in self.vault.status()['records'] if r['account']==source and r['network']=='mainnet' and r['context']==context]
        with self.money.db() as db: link=db.execute('SELECT * FROM native_key_links WHERE account=?',(source,)).fetchone()
        require(len(records)==1 and link and link['revision']==records[0]['revision'],'Import/relink the current local financial key for this identity')
        identity={k:records[0][k] for k in ('account','user_id','context','network','address','public_key','revision')}
        return identity,link['anchor']

    def report(self,source,*,scope_ids,ui_context):
        context,_=self._scope(source,scope_ids,ui_context)
        inventory=self.vault.status()
        record=next((r for r in inventory['records'] if r['account']==source and r['network']=='mainnet'),None)
        if record: record={k:record[k] for k in ('account','user_id','context','network','address','public_key','revision')}
        with self.money.db() as db:
            saved=db.execute('SELECT * FROM native_policies WHERE account=?',(source,)).fetchone()
            journal=[dict(r) for r in db.execute('SELECT i.id,i.state,i.amount,i.destination,i.expires,n.seqno AS native_seqno,n.stage AS native_stage FROM intents i LEFT JOIN native_commitments n ON n.intent=i.id WHERE i.account=? ORDER BY i.created DESC LIMIT 10',(source,))]
        policy=json.loads(saved['policy']) if saved else None
        return {'account_id':source,'context':context,'ui_context':ui_context,'vault_exists':inventory['exists'],
            'vault_locked':inventory['locked_in_process'],'key':record,'policy':policy,
            'policy_enabled':bool(saved and saved['enabled']),'target_id':saved['target'] if saved else None,
            'target_context':saved['target_context'] if saved else None,'username':saved['username'] if saved else None,
            'native_enabled':self.executor.allow(),'journal':journal,'provider_read_performed':False}

    def import_key(self,source,seed,anchor,*,scope_ids,ui_context,expected_revision,confirmed=False):
        # bytearray only so the UI-owned buffer is wiped even on failed validation.
        require(isinstance(seed,bytearray),'Local mutable seed buffer required')
        try:
            require(confirmed is True and len(seed)==32,'Confirm local import of one raw Ed25519 seed32')
            context,_=self._scope(source,scope_ids,ui_context)
            public=Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw,PublicFormat.Raw).hex()
            anchor=anchor or public
            address=self.executor.codec.derive(anchor,'mainnet')['source']
            account=self.money.accounts.get_account(source)
            revision=self.vault.put(account=source,user_id=account.user_id,context=context,network='mainnet',
                address=address,public_key=public,seed=seed,expected_revision=expected_revision,confirmed=True)
            with self.money.db() as db:
                db.execute('INSERT OR REPLACE INTO native_key_links VALUES(?,?,?)',(source,revision,anchor))
                db.execute('UPDATE native_policies SET enabled=0 WHERE account=?',(source,))
            return self.report(source,scope_ids=scope_ids,ui_context=ui_context)
        finally:
            seed[:]=bytes(len(seed))

    def _ensure_vault_ready(self):
        inv = self.vault.status()
        if not inv['exists']:
            self.vault.create(confirmed=True)
        elif inv.get('locked_in_process'):
            self.vault.unlock(confirmed=True)

    def _save_wrapped_mnemonic(self, source, revision, phrase):
        if not phrase or not revision:
            return
        try:
            entropy = hashlib.sha256(f'accmanager/wallet-mnemonic/v1/{source}/{revision}'.encode()).digest()
            wrapped = self.vault.protector.protect(phrase.encode('utf-8'), entropy)
            with self.money.db() as db:
                db.execute('INSERT OR REPLACE INTO native_wallet_mnemonics VALUES(?,?,?)', (source, revision, wrapped))
        except Exception:
            pass

    def _register_public_asset(self, source, address):
        try:
            from modules.asset_registry import AssetStore
            store = AssetStore(self.money.config.data_dir, clock=self.money.clock)
            existing = store.report(source, 'mainnet')
            if not existing or not existing.get('binding'):
                store.register(source, 'mainnet', address, 'provider_export', expected_revision=None, confirmed=True)
        except Exception:
            pass

    def export_wrapped_mnemonic(self, source, *, scope_ids, ui_context, confirmed=False):
        require(confirmed is True, 'Confirm reading DPAPI-wrapped recovery phrase from local vault')
        inv = self.vault.status()
        require(inv.get('exists') and not inv.get('locked_in_process'), 'Financial vault missing or locked in this process')
        report = self.report(source, scope_ids=scope_ids, ui_context=ui_context)
        key = report.get('key')
        require(isinstance(key, dict) and key.get('revision'), 'No configured wallet key in local vault')
        with self.money.db() as db:
            row = db.execute('SELECT revision, wrapped FROM native_wallet_mnemonics WHERE account=?', (source,)).fetchone()
        require(row is not None and row['revision'] == key['revision'], 'Stored wrapped mnemonic is absent or revision-mismatched')
        entropy = hashlib.sha256(f"accmanager/wallet-mnemonic/v1/{source}/{row['revision']}".encode()).digest()
        return self.vault.protector.unprotect(row['wrapped'], entropy).decode('utf-8')

    def import_mnemonic(self, source, phrase_or_seed, *, anchor=None, scope_ids, ui_context, expected_revision=None, confirmed=False):
        """Import a 12/24-word TEP-0003 Rotation Mnemonic or 64-hex seed32 into FinancialVault."""
        require(confirmed is True, 'Confirm local import of recovery phrase or seed32')
        from modules.wallet_tg_custody import parse_seed_or_mnemonic, _wipe
        parsed = parse_seed_or_mnemonic(phrase_or_seed)
        signing_seed = parsed['signing_seed']
        anchor_seed = parsed['anchor_seed']
        try:
            self._ensure_vault_ready()
            current_report = self.report(source, scope_ids=scope_ids, ui_context=ui_context)
            rev = expected_revision if expected_revision is not None else (current_report['key']['revision'] if current_report.get('key') else None)
            effective_anchor = anchor or parsed['anchor_public_key']
            updated = self.import_key(
                source,
                signing_seed,
                effective_anchor,
                scope_ids=scope_ids,
                ui_context=ui_context,
                expected_revision=rev,
                confirmed=True,
            )
            if updated.get('key'):
                self._save_wrapped_mnemonic(source, updated['key']['revision'], parsed.get('phrase', ''))
                self._register_public_asset(source, updated['key']['address'])
            return updated
        finally:
            _wipe(signing_seed)
            _wipe(anchor_seed)

    def provision_local_wallet(
        self,
        source,
        *,
        phrase_or_seed=None,
        replace_existing=False,
        scope_ids,
        ui_context,
        confirmed=False,
    ):
        """Instant offline WalletTg generation/import in DPAPI FinancialVault (zero network/proxy dependency)."""
        require(confirmed is True, 'Confirm local WalletTg creation in FinancialVault')
        from modules.wallet_tg_custody import (
            derive_wallet_identity,
            generate_rotation_mnemonic,
            _wipe,
        )
        self._ensure_vault_ready()
        scope_ids = frozenset(scope_ids)
        context, _ = self._scope(source, scope_ids, ui_context)
        if not replace_existing and not phrase_or_seed:
            try:
                existing, _ = self._key(source, context)
                rep = self.report(source, scope_ids=scope_ids, ui_context=ui_context)
                addr = existing['address']
                self._register_public_asset(source, addr)
                return {
                    **rep,
                    'wallet_state': {'availability': 'local_vault_ready', 'address': addr, 'public_key': existing['public_key']},
                    'custody_mode': 'already_configured',
                    'mnemonic_phrase': '',
                    'provisioned_address': addr,
                    'friendly_address': friendly_address(addr, 'mainnet', bounceable=True),
                    'non_bounceable_address': friendly_address(addr, 'mainnet', bounceable=False),
                }
            except ValueError:
                old_rec = next((r for r in self.vault.status().get('records', []) if r.get('account') == source and r.get('network') == 'mainnet'), None)
                if old_rec is not None:
                    seed_buf = None
                    try:
                        with self.money.db() as db:
                            link = db.execute('SELECT anchor FROM native_key_links WHERE account=?', (source,)).fetchone()
                        anchor = link['anchor'] if link else old_rec['public_key']
                        old_meta = {k: old_rec[k] for k in ('account', 'user_id', 'context', 'network', 'address', 'public_key', 'revision')}
                        with self.vault.use_seed(**old_meta) as plain_view:
                            seed_buf = bytearray(plain_view)
                        updated = self.import_key(
                            source,
                            seed_buf,
                            anchor,
                            scope_ids=scope_ids,
                            ui_context=ui_context,
                            expected_revision=old_rec['revision'],
                            confirmed=True,
                        )
                        addr = old_rec['address']
                        self._register_public_asset(source, addr)
                        return {
                            **updated,
                            'wallet_state': {'availability': 'local_vault_ready', 'address': addr, 'public_key': old_rec['public_key']},
                            'custody_mode': 'already_configured',
                            'mnemonic_phrase': '',
                            'provisioned_address': addr,
                            'friendly_address': friendly_address(addr, 'mainnet', bounceable=True),
                            'non_bounceable_address': friendly_address(addr, 'mainnet', bounceable=False),
                        }
                    except Exception as exc:
                        raise NativeExecutionError('Existing vault key could not be unlocked/rebound without replace_existing=True') from exc
                    finally:
                        _wipe(seed_buf)
        identity = None
        try:
            raw_input = phrase_or_seed if phrase_or_seed else generate_rotation_mnemonic()
            identity = derive_wallet_identity(raw_input, network='mainnet', codec=self.executor.codec)
            generated_phrase = identity.get('phrase', '')
            current_report = self.report(source, scope_ids=scope_ids, ui_context=ui_context)
            rev = current_report['key']['revision'] if current_report.get('key') else None
            updated = self.import_key(
                source,
                identity['signing_seed'],
                identity['anchor_public_key'],
                scope_ids=scope_ids,
                ui_context=ui_context,
                expected_revision=rev,
                confirmed=True,
            )
            addr = identity['address']
            if updated.get('key'):
                self._save_wrapped_mnemonic(source, updated['key']['revision'], generated_phrase)
            self._register_public_asset(source, addr)
            return {
                **updated,
                'wallet_state': {
                    'availability': 'local_vault_ready',
                    'address': addr,
                    'public_key': identity['signing_public_key'],
                },
                'custody_mode': 'provisioned_local_wallet_tg',
                'mnemonic_phrase': generated_phrase,
                'provisioned_address': addr,
                'friendly_address': friendly_address(addr, 'mainnet', bounceable=True),
                'non_bounceable_address': friendly_address(addr, 'mainnet', bounceable=False),
            }
        finally:
            if identity:
                _wipe(identity.get('signing_seed'))
                _wipe(identity.get('anchor_seed'))

    async def auto_sync_or_provision_wallet(
        self,
        source,
        *,
        phrase_or_seed=None,
        password=None,
        replace_existing=False,
        online_sync=True,
        online_timeout=15.0,
        scope_ids,
        ui_context,
        confirmed=False,
        scope_check=lambda: True,
    ):
        """Extract cloud secret phrase, bind imported phrase/seed, or provision a new WalletTg (with automatic offline fallback)."""
        require(confirmed is True, 'Confirm Telegram wallet synchronization / provisioning')
        from modules.wallet_tg_custody import (
            derive_wallet_identity,
            generate_rotation_mnemonic,
            _wipe,
        )
        self._ensure_vault_ready()
        scope_ids = frozenset(scope_ids)
        context, _ = self._scope(source, scope_ids, ui_context)
        require(scope_check())
        if not online_sync:
            return self.provision_local_wallet(
                source,
                phrase_or_seed=phrase_or_seed,
                replace_existing=replace_existing,
                scope_ids=scope_ids,
                ui_context=ui_context,
                confirmed=True,
            )

        account = self.money.accounts.get_account(source)
        generated_phrase = ''
        mode = 'synced'
        identity = None
        state = None

        try:
            async with asyncio.timeout(online_timeout):
                require(scope_check())
                async with self.executor.factory(self.money.config, account) as client:
                    require((await client.get_me()).get('user_id') == account.user_id)
                    try:
                        state = await client.native_money_state()
                    except Exception as exc:
                        if 'WALLET_UNAVAILABLE' not in str(exc).upper():
                            raise
                        state = {'availability': 'wallet_unavailable_region'}
                    require(scope_check())

                    if phrase_or_seed:
                        identity = derive_wallet_identity(phrase_or_seed, network='mainnet', codec=self.executor.codec)
                        generated_phrase = identity.get('phrase', '')
                        if state.get('availability') == 'wallet_unavailable_region':
                            state = {
                                'availability': 'wallet_unavailable_region',
                                'address': identity['address'],
                                'public_key': identity['signing_public_key'],
                            }
                            mode = 'provisioned_local_wallet_tg'
                        elif (
                            not replace_existing
                            and state.get('availability') == 'ready'
                            and (
                                state.get('address') != identity['address']
                                or state.get('public_key') != identity['signing_public_key']
                            )
                        ):
                            raise NativeExecutionError('Existing ready Telegram Wallet has a different key/address; pass replace_existing=True to overwrite')
                        elif (
                            replace_existing
                            or state.get('availability') != 'ready'
                            or state.get('address') != identity['address']
                            or state.get('public_key') != identity['signing_public_key']
                        ):
                            try:
                                state = await client.native_money_replace_wallet(
                                    signing_seed=identity['signing_seed'],
                                    signing_public_key=identity['signing_public_key'],
                                    anchor_public_key=identity['anchor_public_key'],
                                    address=identity['address'],
                                    password=password,
                                    network='mainnet',
                                )
                                mode = 'replaced_on_telegram'
                            except Exception as exc:
                                if 'WALLET_UNAVAILABLE' not in str(exc).upper():
                                    raise
                                state = {
                                    'availability': 'wallet_unavailable_region',
                                    'address': identity['address'],
                                    'public_key': identity['signing_public_key'],
                                }
                                mode = 'provisioned_local_wallet_tg'
                        else:
                            mode = 'matched_on_telegram'
                    else:
                        if state.get('availability') == 'ready':
                            export_err = None
                            if state.get('can_export_phrase') and hasattr(client, 'native_money_export_phrase'):
                                try:
                                    exported = await client.native_money_export_phrase(
                                        password=password,
                                        network='mainnet',
                                        codec=self.executor.codec,
                                    )
                                    if (
                                        exported.get('address') == state.get('address')
                                        and exported.get('signing_public_key') == state.get('public_key')
                                    ):
                                        identity = exported
                                        generated_phrase = exported.get('phrase', '')
                                        mode = 'exported_from_telegram'
                                    else:
                                        export_err = 'identity mismatch'
                                except Exception as exc:
                                    identity = None
                                    export_err = type(exc).__name__
                            if identity is None and not replace_existing:
                                local_report = self.report(source, scope_ids=scope_ids, ui_context=ui_context)
                                local_key = local_report.get('key') or {}
                                if (
                                    local_key.get('address') == state.get('address')
                                    and local_key.get('public_key') == state.get('public_key')
                                ):
                                    addr = local_key['address']
                                    self._register_public_asset(source, addr)
                                    return {
                                        **local_report,
                                        'wallet_state': state,
                                        'custody_mode': 'already_configured',
                                        'mnemonic_phrase': '',
                                        'provisioned_address': addr,
                                        'friendly_address': friendly_address(addr, 'mainnet', bounceable=True),
                                        'non_bounceable_address': friendly_address(addr, 'mainnet', bounceable=False),
                                    }
                                reason = f' ({export_err})' if export_err else ''
                                raise NativeExecutionError(
                                    f'Existing ready Telegram Wallet cannot be synced without secret phrase export{reason}; refusing replaceWallet overwrite'
                                )

                        if identity is None:
                            words = generate_rotation_mnemonic()
                            identity = derive_wallet_identity(words, network='mainnet', codec=self.executor.codec)
                            generated_phrase = identity.get('phrase', '')
                            if state.get('availability') == 'wallet_unavailable_region':
                                state = {
                                    'availability': 'wallet_unavailable_region',
                                    'address': identity['address'],
                                    'public_key': identity['signing_public_key'],
                                }
                                mode = 'provisioned_local_wallet_tg'
                            else:
                                try:
                                    state = await client.native_money_replace_wallet(
                                        signing_seed=identity['signing_seed'],
                                        signing_public_key=identity['signing_public_key'],
                                        anchor_public_key=identity['anchor_public_key'],
                                        address=identity['address'],
                                        password=password,
                                        network='mainnet',
                                    )
                                    mode = 'provisioned_new_on_telegram'
                                except Exception as exc:
                                    if 'WALLET_UNAVAILABLE' not in str(exc).upper():
                                        raise
                                    state = {
                                        'availability': 'wallet_unavailable_region',
                                        'address': identity['address'],
                                        'public_key': identity['signing_public_key'],
                                    }
                                    mode = 'provisioned_local_wallet_tg'

                    require(scope_check() and self._scope(source, scope_ids, ui_context)[0] == context)
        except (NativeExecutionError, asyncio.CancelledError):
            if identity:
                _wipe(identity.get('signing_seed'))
                _wipe(identity.get('anchor_seed'))
            raise
        except Exception:
            if identity:
                _wipe(identity.get('signing_seed'))
                _wipe(identity.get('anchor_seed'))
                identity = None
            require(scope_check() and self._scope(source, scope_ids, ui_context)[0] == context)
            return self.provision_local_wallet(
                source,
                phrase_or_seed=phrase_or_seed,
                replace_existing=replace_existing,
                scope_ids=scope_ids,
                ui_context=ui_context,
                confirmed=True,
            )

        try:
            current_report = self.report(source, scope_ids=scope_ids, ui_context=ui_context)
            rev = current_report['key']['revision'] if current_report.get('key') else None
            updated = self.import_key(
                source,
                identity['signing_seed'],
                identity['anchor_public_key'],
                scope_ids=scope_ids,
                ui_context=ui_context,
                expected_revision=rev,
                confirmed=True,
            )
            addr = identity['address']
            if updated.get('key'):
                self._save_wrapped_mnemonic(source, updated['key']['revision'], generated_phrase)
            self._register_public_asset(source, addr)
            return {
                **updated,
                'wallet_state': state,
                'custody_mode': mode,
                'mnemonic_phrase': generated_phrase,
                'provisioned_address': addr,
                'friendly_address': friendly_address(addr, 'mainnet', bounceable=True),
                'non_bounceable_address': friendly_address(addr, 'mainnet', bounceable=False),
            }
        finally:
            if identity:
                _wipe(identity.get('signing_seed'))
                _wipe(identity.get('anchor_seed'))

    def export_wallets_summary(self, account_ids=None):
        """Return and write human-readable + JSON wallet inventory for all configured accounts."""
        inventory = self.vault.status()
        records = {
            r['account']: r
            for r in inventory.get('records', [])
            if r.get('network') == 'mainnet'
        }
        with self.money.db() as db:
            links = {
                r['account']: r['anchor']
                for r in db.execute('SELECT account, anchor FROM native_key_links').fetchall()
            }
        target_ids = list(account_ids) if account_ids is not None else sorted(records.keys())
        items = []
        lines = [
            '# Uznik MultiTool / WalletTg Local Custody Inventory (Mainnet)',
            '# account_id | phone | @username | non_bounceable (UQ, for first deposit) | bounceable (EQ) | raw_address',
        ]
        for acc_id in target_ids:
            rec = records.get(acc_id)
            if not rec:
                continue
            try:
                acc = self.money.accounts.get_account(acc_id)
                phone = getattr(acc, 'phone', None) or ''
                username = getattr(acc, 'username', None) or ''
                group = getattr(acc, 'group', None) or ''
            except Exception:
                phone, username, group = '', '', ''
            raw_addr = rec['address']
            eq_addr = friendly_address(raw_addr, 'mainnet', bounceable=True)
            uq_addr = friendly_address(raw_addr, 'mainnet', bounceable=False)
            entry = {
                'account_id': acc_id,
                'user_id': rec.get('user_id'),
                'phone': phone,
                'username': username,
                'group': group,
                'raw_address': raw_addr,
                'bounceable_address': eq_addr,
                'non_bounceable_address': uq_addr,
                'public_key': rec.get('public_key'),
                'anchor_public_key': links.get(acc_id, rec.get('public_key')),
            }
            items.append(entry)
            user_str = f'@{username}' if username else '-'
            lines.append(f'{acc_id} | {phone or "-"} | {user_str} | {uq_addr} | {eq_addr} | {raw_addr}')
        txt_path = self.money.config.data_dir / 'wallet_addresses.txt'
        json_path = self.money.config.data_dir / 'wallet_addresses.json'
        try:
            txt_path.parent.mkdir(parents=True, exist_ok=True)
            txt_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            json_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass
        return {
            'count': len(items),
            'items': items,
            'txt_path': str(txt_path),
            'json_path': str(json_path),
        }

    async def batch_provision_wallets(
        self,
        account_ids,
        *,
        replace_existing=False,
        online_sync=False,
        scope_ids,
        ui_context,
        confirmed=False,
        scope_check=lambda: True,
        progress=None,
    ):
        """Batch-create or sync WalletTg wallets for all selected/group accounts and export address list."""
        require(confirmed is True, 'Confirm batch WalletTg provisioning')
        self._ensure_vault_ready()
        scope_ids = frozenset(scope_ids)
        ordered = list(account_ids)
        created = 0
        already = 0
        skipped = []
        failed = []
        provisioned = []
        total = len(ordered)

        def _notify_progress(acc_id, index, *, ok=True, error=''):
            if progress is None:
                return
            if hasattr(progress, 'mark_ok') and hasattr(progress, 'mark_error'):
                if ok:
                    progress.mark_ok(acc_id)
                else:
                    progress.mark_error(acc_id, error)
            elif callable(progress):
                progress(index, total)

        for index, acc_id in enumerate(ordered, 1):
            require(scope_check())
            try:
                acc = self.money.accounts.get_account(acc_id)
            except Exception as exc:
                failed.append({'account_id': acc_id, 'error': str(exc)})
                _notify_progress(acc_id, index, ok=False, error=str(exc))
                continue
            if not getattr(acc, 'enabled', True) or type(getattr(acc, 'user_id', None)) is not int or acc.user_id <= 0:
                skipped.append({'account_id': acc_id, 'reason': 'no verified user_id (run Check sessions first)'})
                _notify_progress(acc_id, index, ok=False, error='no verified user_id')
                continue
            try:
                if online_sync:
                    res = await self.auto_sync_or_provision_wallet(
                        acc_id,
                        replace_existing=replace_existing,
                        online_sync=True,
                        scope_ids=scope_ids,
                        ui_context=ui_context,
                        confirmed=True,
                        scope_check=scope_check,
                    )
                else:
                    res = self.provision_local_wallet(
                        acc_id,
                        replace_existing=replace_existing,
                        scope_ids=scope_ids,
                        ui_context=ui_context,
                        confirmed=True,
                    )
                mode = res.get('custody_mode', 'provisioned_local_wallet_tg')
                if mode == 'already_configured':
                    already += 1
                else:
                    created += 1
                provisioned.append({
                    'account_id': acc_id,
                    'username': getattr(acc, 'username', None) or '',
                    'raw_address': res['provisioned_address'],
                    'friendly_address': res.get('friendly_address', ''),
                    'non_bounceable_address': res.get('non_bounceable_address', ''),
                    'custody_mode': mode,
                })
                _notify_progress(acc_id, index, ok=True)
            except Exception as exc:
                failed.append({'account_id': acc_id, 'error': str(exc)})
                _notify_progress(acc_id, index, ok=False, error=str(exc))
        summary = self.export_wallets_summary()
        return {
            'total': total,
            'created': created,
            'already_configured': already,
            'skipped': skipped,
            'failed': failed,
            'provisioned': provisioned,
            'txt_path': summary['txt_path'],
            'json_path': summary['json_path'],
        }

    def save_policy(self,source,values,*,scope_ids,ui_context,expected_revision,confirmed=False):
        require(confirmed is True and isinstance(values,dict) and set(values)=={'target_id','target_address','debit','fee','daily','reserve','allow_existing'},'Exact explicitly confirmed local policy required')
        context,_=self._scope(source,scope_ids,ui_context);identity,_=self._key(source,context)
        target=self.money.accounts.get_account(values['target_id'])
        target_context,_=self._scope(target.id,scope_ids,ui_context)
        require(target.id!=source and isinstance(target.username,str) and bool(target.username))
        require(values['allow_existing'] is True,'Separately authorize spending existing native funds')
        policy=make_policy(account=source,account_context=context,ui_context=ui_context,network='mainnet',
            source=identity['address'],targets=[values['target_address']],scope='all_native_authorized',
            reserve_nano=amount(values['reserve']),fee_cap_nano=amount(values['fee']),
            transfer_debit_cap_nano=amount(values['debit']),daily_reservation_cap_nano=amount(values['daily']),
            confirmed=True,all_native_confirmed=True,allow_high_caps=(self.executor.factory is create_client))
        with self.money.db() as db:
            old=db.execute('SELECT revision FROM native_policies WHERE account=?',(source,)).fetchone()
            require((old['revision'] if old else None)==expected_revision,'Policy changed; reload before saving')
            db.execute('INSERT OR REPLACE INTO native_policies VALUES(?,?,?,?,?,?,?)',
                (source,policy.revision,1,target.id,target_context,target.username,json.dumps(asdict(policy),sort_keys=True)))
        return self.report(source,scope_ids=scope_ids,ui_context=ui_context)

    def _load(self,source,scope_ids,ui_context):
        report=self.report(source,scope_ids=scope_ids,ui_context=ui_context)
        require(report['policy_enabled'] and report['policy'],'Save/enable a current local policy first')
        data=dict(report['policy']);data['targets']=tuple(data['targets']);policy=NativeSpendPolicy(**data)
        identity,anchor=self._key(source,report['context'])
        require(not report['vault_locked'],'Financial vault is locked in this process')
        target=self.money.accounts.get_account(report['target_id'])
        ctx,_=self._scope(target.id,scope_ids,ui_context)
        require(policy.account_context==report['context'] and policy.ui_context==ui_context
            and ctx==report['target_context'] and target.username==report['username'],'Identity/group/filter/recipient changed; save policy again')
        return report,policy,identity,anchor,target

    async def resolve(self,source,target_id,*,scope_ids,ui_context,confirmed=False,scope_check=lambda:True):
        require(confirmed is True,'Confirm a session read for this specific recipient')
        context,_=self._scope(source,scope_ids,ui_context)
        target_context,_=self._scope(target_id,scope_ids,ui_context)
        account=self.money.accounts.get_account(source);target=self.money.accounts.get_account(target_id)
        require(source!=target_id and isinstance(target.username,str) and bool(target.username))
        username=target.username
        result={}
        try:
            async with asyncio.timeout(18):
                require(scope_check())
                async with self.executor.factory(self.money.config,account) as client:
                    require((await client.get_me()).get('user_id')==account.user_id)
                    result=await client.native_money_recipient(username,target.user_id,prepare_address=False)
                require(scope_check() and self._scope(source,scope_ids,ui_context)[0]==context
                    and self._scope(target_id,scope_ids,ui_context)[0]==target_context
                    and self.money.accounts.get_account(target_id).username==username)
        except (NativeExecutionError, asyncio.CancelledError):
            raise
        except Exception:
            require(scope_check() and self._scope(source,scope_ids,ui_context)[0]==context
                and self._scope(target_id,scope_ids,ui_context)[0]==target_context
                and self.money.accounts.get_account(target_id).username==username)
            result={}
        if not isinstance(result.get('address'),str):
            try:
                target_identity,_=self._key(target_id,target_context)
                return {'target_id':target_id,'username':username,'address':target_identity['address'],'verification':'local_vault_key','money_activated':'unknown'}
            except Exception:
                pass
        require(isinstance(result.get('address'),str),'Telegram did not return a recipient address; no force/provisioning was requested')
        return {'target_id':target_id,'username':username,'address':result['address'],'verification':'telegram_reported','money_activated':'unknown'}

    async def prepare_recipient(self,source,target_id,*,scope_ids,ui_context,confirmation,confirmed=False,scope_check=lambda:True):
        """Explicit Telegram provisioning, not a read fallback or payment."""
        require(confirmed is True and isinstance(source,str) and isinstance(target_id,str)
            and confirmation=='PREPARE '+source+' TO '+target_id,'Exact source/recipient PREPARE confirmation required')
        scope_ids=frozenset(scope_ids)
        context,_=self._scope(source,scope_ids,ui_context)
        target_context,_=self._scope(target_id,scope_ids,ui_context)
        target=self.money.accounts.get_account(target_id);username=target.username
        require(source!=target_id and isinstance(username,str) and bool(username))
        def check():
            require(scope_check() and self._scope(source,scope_ids,ui_context)[0]==context
                and self._scope(target_id,scope_ids,ui_context)[0]==target_context
                and self.money.accounts.get_account(target_id).username==username,
                'Source/recipient/group/filter changed; address preparation cancelled')
            return True
        check()
        # Reuse metadata lease/cooldown/identity checks, not a second wallet/payment registry.
        from modules.money_native import NativeMoneyService
        service=NativeMoneyService(self.money.config,accounts=self.money.accounts,
            client_factory=self.executor.factory,clock=self.money.clock)
        result=await service.action('native-resolve',{'account_id':source,'target_account_id':target_id,
            'prepare_address':True,'confirmed':True},scope_ids=scope_ids,ui_context=ui_context,scope_check=check)
        check();recipient=result['native']['recipient']
        require(isinstance(recipient.get('address'),str),
            'Preparation returned no address. Outcome is not activation; check explicitly later without force. No automatic retry')
        return {'target_id':target_id,'username':username,'address':recipient['address'],
            'prepare_address_requested':True,'verification':'telegram_reported','money_activated':'unknown',
            'chain_state':'unknown','signer_ready':False,'automatic_retry':False}

    async def preview(self,source,*,scope_ids,ui_context,confirmed=False,scope_check=lambda:True):
        require(confirmed is True,'Confirm Telegram/provider reads and unsigned simulation')
        report,policy,identity,anchor,target=self._load(source,scope_ids,ui_context)
        def check():
            require(scope_check())
            fresh=self.report(source,scope_ids=scope_ids,ui_context=ui_context)
            require(fresh['policy_enabled'] and fresh['policy']['revision']==policy.revision
                and fresh['key']['revision']==identity['revision'],'Policy/key changed during preview')
            target_ctx,_=self._scope(target.id,scope_ids,ui_context)
            require(target_ctx==report['target_context'] and self.money.accounts.get_account(target.id).username==report['username'])
        check();ident=uuid.uuid4().hex;expires=int(self.money.clock())+180
        async with asyncio.timeout(90):
            async with self.executor.factory(self.money.config,self.money.accounts.get_account(source)) as client:
                require((await client.get_me()).get('user_id')==identity['user_id'])
                state=await client.native_money_state();check()
                require(state.get('availability')=='ready' and state.get('address')==policy.source and state.get('public_key')==identity['public_key'])
                recipient=await client.native_money_recipient(target.username,target.user_id,prepare_address=False);check()
                require(recipient.get('address') in policy.targets,'Recipient address does not match the saved allowlist')
                minimum=await client.native_money_minimum();check()
                observed=await self.executor.reader.read(policy.source,'mainnet',anchor,confirmed=True,allow_deployment=True);check()
                require(observed['public_key']==identity['public_key'])
                deploy=observed.get('active') is False
                with self.money.db() as db: reserved=self.money.reserved_debit(db,policy.source,'mainnet')
                plan=quote(policy,observed,target=recipient['address'],transit_credit_nano='0',reserved_nano=str(reserved),
                    min_transfer_nano=minimum,current_account_context=report['context'],current_ui_context=ui_context,
                    now=self.money.clock(),valid_until=expires,intent_id=ident,deploy=deploy)
                projected=await self.executor.emulator.project(policy,observed,plan,anchor_public_key=anchor,
                    current_account_context=report['context'],current_ui_context=ui_context,confirmed=True)
                require(projected.get('verification')=='provider_reported_simulation_not_receipt'
                    and projected.get('quote_hash')==plan['quote_hash'] and projected.get('observation_hash')==plan['observation_hash'],
                    'Unsigned preview is not bound to this wallet/policy')
                require(not deploy or projected.get('deployment_projected') is True,'Initial deployment not projected; no signing authorized')
                check()
        limits={k:plan[k] for k in ('amount_nano','max_debit_nano','balance_nano','seqno','public_key','min_transfer_nano')}
        limits.update(destination=plan['target'],data_hash=observed['data_hash'])
        token=secrets.token_hex(24)
        review={'account_id':source,'target_id':target.id,'target_username':target.username,'source':policy.source,
            'limits':limits,'fee_cap_nano':policy.fee_cap_nano,'estimated_fee_nano':projected['estimated_fee_nano'],
            'recipient_state':projected.get('recipient_state','unknown'),'money_activated':'unknown',
            'deployment_required':deploy,
            'policy_revision':policy.revision,'key_revision':identity['revision'],'expires':min(expires,self.money.clock()+60)}
        with self.money.db() as db:
            db.execute('DELETE FROM native_reviews WHERE expires<?',(self.money.clock()-3600,))
            db.execute('INSERT INTO native_reviews VALUES(?,?,?,?,?,?,?,?)',
                (hashlib.sha256(token.encode()).hexdigest(),source,policy.revision,identity['revision'],self.money.clock(),review['expires'],0,json.dumps(review,sort_keys=True)))
        return {**review,'review_token':token,'signed':False,'verification':'provider_reported_simulation_not_receipt'}

    async def send(self,source,token,*,scope_ids,ui_context,confirmation,scope_check=lambda:True):
        require(isinstance(token,str) and re.fullmatch('[a-f0-9]{48}',token))
        report,policy,identity,anchor,target=self._load(source,scope_ids,ui_context)
        def check():
            require(scope_check())
            fresh=self.report(source,scope_ids=scope_ids,ui_context=ui_context)
            require(fresh['policy_enabled'] and fresh['policy']['revision']==policy.revision
                and fresh['key']['revision']==identity['revision'])
            return True
        check();require(self.executor.allow(),'Enable both native/mainnet flags locally before sending')
        with self.money.db() as db:
            row=db.execute('SELECT * FROM native_reviews WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            require(row and row['account']==source and row['used']==0 and row['expires']>self.money.clock()
                and row['policy_revision']==policy.revision and row['key_revision']==identity['revision'],'Review stale/used/expired; prepare again')
            review=json.loads(row['review'])
            deploy=review.get('deployment_required',False)
            require(type(deploy) is bool)
            prefix='DEPLOY AND SEND ' if deploy else 'SEND '
            require(confirmation==prefix+source+' TO '+target.id and review['target_id']==target.id,'Exact source/recipient/deployment confirmation required')
            db.execute('UPDATE native_reviews SET used=1 WHERE token_hash=?',(row['token_hash'],))
        # Consume BEFORE async admission; a rejected/uncertain attempt is not retried.
        return await self.executor.execute(policy,identity,anchor_public_key=anchor,target_account_id=target.id,
            scope_ids=scope_ids,ui_context=ui_context,confirmed=True,scope_check=check,reviewed_limits=review['limits'],
            reviewed_recipient_state=review.get('recipient_state','unknown'),deploy=deploy)

    async def recover(self,source,intent_id,*,scope_ids,ui_context,confirmation,scope_check=lambda:True):
        from modules.money_recovery import NativeNonceRecovery
        context,_=self._scope(source,scope_ids,ui_context)
        identity,anchor=self._key(source,context)
        def check():
            current,current_anchor=self._key(source,context)
            return scope_check() and current==identity and current_anchor==anchor
        return await NativeNonceRecovery(self.money,self.executor.reader).recover(identity,anchor,intent_id,
            scope_ids=scope_ids,ui_context=ui_context,confirmation=confirmation,scope_check=check)
