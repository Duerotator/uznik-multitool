"""Integrated, explicitly enabled local one-shot native Money execution.

Mode 3 (optionally initial StateInit), or funding-only active-source mode 130.
No HTTP seed intake,
background activation loop, automatic retry/resume or sweep. Claims and
fee-inclusive reservations share the existing journal.
"""
import asyncio
import base64
import hashlib
import os
import secrets
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.telegram_client import create_client
from modules.money_chain_read import TonCenterWalletReader
from modules.money_emulation import TonCenterEmulator
from modules.money_policy import NativeSpendPolicy,quote,nano
from modules.wallet_tg_codec import WalletTgCodec
from modules.money_transit import setup as setup_transit, funding_snapshot, claim_funding, check_observation
from modules.money_carousel_run import CarouselStep


class NativeExecutionError(ValueError):
    pass


def require(condition,message='Native execution precondition changed; explicit review required'):
    if not condition: raise NativeExecutionError(message)


def _is_wallet_unavailable(exc):
    if isinstance(exc, (NativeExecutionError, ValueError, TimeoutError, asyncio.CancelledError)):
        return False
    text = f'{type(exc).__name__} {exc}'.upper()
    return 'WALLET_UNAVAILABLE' in text


def _is_blocked_send_abort(exc):
    if isinstance(exc, (NativeExecutionError, ValueError, TimeoutError, asyncio.CancelledError)):
        return True
    text = f'{type(exc).__name__} {exc}'.upper()
    return any(tag in text for tag in ('AUTH_', 'SESSION_', 'FROZEN', 'USER_DEACTIVATED', 'INPUT_USER_DEACTIVATED', 'UNAUTHORIZED', 'FLOOD_WAIT', 'PASSWORD_'))


def enabled():
    return os.getenv('TGB_FINANCE_ENABLE_NATIVE')=='1' and os.getenv('TGB_FINANCE_ENABLE_MAINNET')=='1'


class NativeMoneyExecutor:
    def __init__(self,money,vault,*,reader=None,emulator=None,codec=None,client_factory=create_client,allow=enabled):
        self.money,self.vault,self.factory,self.allow=money,vault,client_factory,allow
        self.codec=codec or WalletTgCodec(clock=money.clock)
        self.reader=reader or TonCenterWalletReader(codec=self.codec,clock=money.clock)
        self.emulator=emulator or TonCenterEmulator(codec=self.codec,clock=money.clock)
        with money.db() as db: setup_transit(db)

    async def execute(self,policy,identity,*,anchor_public_key,target_account_id,scope_ids,ui_context,
                      confirmed=False,scope_check=lambda:True,reviewed_limits=None,reviewed_recipient_state=None,deploy=False,funding_ref=None,send_all=False,carousel_step=None):
        require(type(deploy) is bool,'Explicit deployment choice required')
        require(type(send_all) is bool and not (send_all and deploy),'Explicit send-all cannot deploy a source')
        require(not send_all or reviewed_limits is None,'Mode-3 UI review cannot authorize a drain')
        require(reviewed_recipient_state in (None,'active','uninitialized_credited','unknown'))
        if reviewed_limits is not None:
            require(isinstance(reviewed_limits,dict) and set(reviewed_limits)=={'destination','amount_nano','max_debit_nano','balance_nano','seqno','data_hash','public_key','min_transfer_nano'})
            reviewed_limits=dict(reviewed_limits)
        require(confirmed is True and self.allow(),'Native execution disabled or not explicitly confirmed')
        require(isinstance(policy,NativeSpendPolicy) and policy.network=='mainnet'
                and policy.scope in {'all_native_authorized','transit_only'},'Explicit mainnet mode-3 policy required')
        require(carousel_step is None or isinstance(carousel_step,CarouselStep) and send_all and policy.scope=='transit_only',
                'Carousel requires an exact process-local step and explicit transit-only send-all')
        require((funding_ref is not None)==(policy.scope=='transit_only' and (carousel_step is None or carousel_step.ordinal==0)),
                'Transit-only execution requires an exact one-use funding reference')
        require(not send_all or policy.scope=='transit_only','Initial send-all requires funding-only transit policy')
        if isinstance(funding_ref,dict): funding_ref=dict(funding_ref)
        require(isinstance(identity,dict) and set(identity)=={'account','user_id','context','network','address','public_key','revision'})
        identity=dict(identity)
        require(isinstance(policy.targets,tuple) and bool(policy.targets),'Immutable local policy snapshot required')
        source=self.money.accounts.get_account(policy.account)
        target=self.money.accounts.get_account(target_account_id)
        context,ui=self.money.scope(source.id,scope_ids,ui_context)
        target_context,_=self.money.scope(target.id,scope_ids,ui_context)
        require(source.id!=target.id and isinstance(target.username,str) and bool(target.username))
        username=target.username
        require(context==policy.account_context and ui==policy.ui_context and identity['account']==source.id
                and identity['user_id']==source.user_id and identity['context']==context
                and identity['network']==policy.network and identity['address']==policy.source)
        require(self.codec.derive(anchor_public_key,policy.network)['source']==policy.source)
        is_prod=(self.factory is create_client)
        expiry=int(self.money.clock())+(295 if is_prod else 180)
        transit=None
        def check():
            require(self.allow() and scope_check())
            require(expiry-self.money.clock()>=30,'Signed request validity window exhausted')
            require(self.money.scope(source.id,scope_ids,ui_context)==(context,ui))
            require(self.money.scope(target.id,scope_ids,ui_context)==(target_context,ui)
                    and self.money.accounts.get_account(target.id).username==username)
            if transit is not None:
                with self.money.db() as db:
                    snapshot=carousel_step.snapshot(self.money,db,policy,target.id,scope_ids,ui_context,intent=ident) if carousel_step else funding_snapshot(
                        self.money,db,funding_ref,policy,target.id,scope_ids,ui_context,intent=ident)
                    require(snapshot['hash']==transit['hash'])
            return True
        check()
        ident=uuid.uuid4().hex;random_id=secrets.randbelow(2**63-1)+1
        claimed=False
        with self.money.db() as db:
            require(db.execute("SELECT 1 FROM intents WHERE network=? AND source=? AND state IN ('wallet_prompted','wallet_submitted_unverified','uncertain')",(policy.network,policy.source)).fetchone() is None,
                    'Source already has an unresolved intent; no second execution')
            reserved=self.money.reserved_debit(db,policy.source,policy.network)
            require(db.execute("SELECT 1 FROM intents i LEFT JOIN native_commitments n ON n.intent=i.id WHERE i.source=? AND i.network=? AND n.intent IS NULL AND i.state<>'prepared' AND COALESCE(i.prompted,i.created)>=?",
                (policy.source,policy.network,self.money.clock()-86400)).fetchone() is None,
                'Legacy fees are unknown; native execution cannot mix this source/day with TON Connect spending')
            cap=nano(policy.transfer_debit_cap_nano,positive=True)
            is_prod=(self.factory is create_client)
            max_cap=100*10**9 if is_prod else 10**9
            max_daily=500*10**9 if is_prod else 5*10**9
            require(cap<=max_cap,'Initial native debit cap is 1 native, fees included')
            require(reserved+cap<=min(nano(policy.daily_reservation_cap_nano),max_daily),'Fee-inclusive daily reservation cap exceeded')
            if policy.scope=='transit_only':
                transit=carousel_step.snapshot(self.money,db,policy,target.id,scope_ids,ui_context) if carousel_step else funding_snapshot(
                    self.money,db,funding_ref,policy,target.id,scope_ids,ui_context)
            db.execute('INSERT INTO intents(id,account,context,ui_context,network,source,destination,amount,created,expires,state,prompted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (ident,source.id,context,ui,policy.network,policy.source,policy.targets[0],'0',self.money.clock(),expiry,'wallet_prompted',self.money.clock()))
            db.execute('INSERT INTO native_commitments VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (ident,policy.network,policy.source,None,str(cap),policy.id,policy.revision,identity['revision'],random_id,'preflight',None))
            if carousel_step is not None: carousel_step.link(db,ident)
            if transit is not None: claim_funding(db,transit,ident)
        phase='client_open'
        is_prod=(self.factory is create_client)
        try:
            async with asyncio.timeout(300 if is_prod else 110):
                async with self.factory(self.money.config,source) as client:
                    phase='source_identity'
                    require((await client.get_me()).get('user_id')==source.user_id,'Session identity mismatch')
                    wallet_region_unavailable = False
                    if is_prod:
                        try: state=await client.native_money_state()
                        except Exception as exc:
                            if not _is_wallet_unavailable(exc): raise
                            wallet_region_unavailable = True
                            state={'availability':'ready','address':policy.source,'public_key':identity['public_key']}
                    else:
                        state=await client.native_money_state()
                    check()
                    require(state.get('availability')=='ready' and state.get('address')==policy.source
                            and state.get('public_key')==identity['public_key'],'Telegram source wallet/key mismatch')
                    phase='recipient'
                    if is_prod:
                        try: recipient=await client.native_money_recipient(username,target.user_id,prepare_address=False)
                        except Exception as exc:
                            if not _is_wallet_unavailable(exc): raise
                            wallet_region_unavailable = True
                            recipient={'address':policy.targets[0]}
                    else:
                        recipient=await client.native_money_recipient(username,target.user_id,prepare_address=False)
                    check()
                    destination=recipient.get('address');require(destination in policy.targets,'Recipient not allowlisted')
                    phase='minimum'
                    if is_prod:
                        try: minimum=await client.native_money_minimum()
                        except Exception as exc:
                            if not _is_wallet_unavailable(exc): raise
                            wallet_region_unavailable = True
                            minimum='10000000'
                    else:
                        minimum=await client.native_money_minimum()
                    check()
                    phase='chain_read'
                    allow_dep=True if (is_prod and send_all) else deploy
                    observed=await self.reader.read(policy.source,policy.network,anchor_public_key,confirmed=True,allow_deployment=allow_dep);check()
                    if is_prod and send_all and observed.get('active') is False:
                        deploy=True
                    require(observed['public_key']==identity['public_key'])
                    require((observed.get('active') is False)==deploy,'Source deployment state changed; review again')
                    transit_amount=check_observation(transit,observed) if transit is not None else '0'
                    allow_dep_drain=(is_prod and send_all and deploy)
                    plan=quote(policy,observed,target=destination,transit_credit_nano=transit_amount,reserved_nano=str(reserved),
                        min_transfer_nano=minimum,current_account_context=context,current_ui_context=ui,
                        now=self.money.clock(),valid_until=expiry,intent_id=ident,deploy=deploy,send_all=send_all,allow_deploy_drain=allow_dep_drain)
                    if reviewed_limits is not None:
                        current={k:plan[k] for k in ('amount_nano','max_debit_nano','balance_nano','seqno','public_key','min_transfer_nano')}
                        current.update(destination=destination,data_hash=observed['data_hash'])
                        require(current==reviewed_limits,'Reviewed amount/budget/wallet state changed; prepare again')
                    phase='simulation'
                    projected=await self.emulator.project(policy,observed,plan,anchor_public_key=anchor_public_key,
                        current_account_context=context,current_ui_context=ui,confirmed=True,allow_deploy_drain=allow_dep_drain);check()
                    require(reviewed_recipient_state is None or projected.get('recipient_state','unknown')==reviewed_recipient_state,
                            'Recipient deployment state changed since preview; review again')
                    require(not deploy or projected.get('deployment_projected') is True,'Initial deployment was not proven by unsigned simulation')
                    unsigned=self.codec.prepare_emulation(network=policy.network,anchor_public_key=anchor_public_key,
                        seqno=plan['seqno'],valid_until=expiry,destination=destination,amount_nano=plan['amount_nano'],
                        intent_id=ident,deploy=deploy,send_all=send_all,allow_deploy_drain=allow_dep_drain,
                        **({'allow_high_caps':True} if cap>10**9 else {}))
                    require(projected.get('verification')=='provider_reported_simulation_not_receipt'
                            and projected.get('quote_hash')==plan['quote_hash'] and projected.get('observation_hash')==plan['observation_hash']
                            and projected.get('unsigned_body_hash')==unsigned['unsigned_body_hash']
                            and 0<=self.money.clock()-projected['checked_at']<=15
                            and nano(projected['estimated_fee_nano'])<=nano(policy.fee_cap_nano)
                            and nano(projected['source_debit_nano'])<=nano(plan['max_debit_nano'])
                            and nano(projected['recipient_net_nano'])>=nano(minimum),'Emulation is stale/unbound/over budget')
                    if transit is not None:
                        require(nano(projected['source_debit_nano'])<=nano(transit['net_nano'])
                            and nano(projected['source_after_nano'])>=nano(transit['old_nano']),
                            'Simulation would spend protected pre-existing funds')
                    journal_amount=plan['amount_nano']
                    if send_all:
                        require(plan['send_mode']==130 and plan['protected_nano']=='0'
                            and projected.get('source_after_nano')=='0'
                            and projected.get('source_debit_nano')==plan['balance_nano'],
                            'Drain projection must consume exactly the authorized balance without protected funds')
                        journal_amount=projected.get('outgoing_value_nano')
                        require(nano(journal_amount,positive=True)>=nano(projected['recipient_net_nano'])
                            and nano(journal_amount)<=nano(plan['balance_nano']), 'Invalid projected drain value')
                        require(nano(projected['source_debit_nano'])-nano(projected['recipient_net_nano'])
                            ==nano(projected['estimated_fee_nano']), 'Drain fee projection is inconsistent')
                    phase='chain_refresh'
                    latest=await self.reader.read(policy.source,policy.network,anchor_public_key,confirmed=True,allow_deployment=deploy);check()
                    require(all(latest[k]==observed[k] for k in ('address','network','public_key','seqno','balance_nano','data_hash','code_hash','implementation_hash')),
                            'Wallet changed during simulation; prepare again explicitly')
                    require(latest['active']==observed['active'] and (not deploy or all(latest.get(k)==observed.get(k)
                        for k in ('chain_state','initial_data_hash','initial_next_data_hash','initial_code_hash'))))
                    if transit is not None: check_observation(transit,latest)
                    if deploy:
                        if is_prod:
                            try: source_state=await client.native_money_state()
                            except Exception as exc:
                                if not _is_wallet_unavailable(exc): raise
                                wallet_region_unavailable = True
                                source_state={'availability':'ready','address':policy.source,'public_key':identity['public_key']}
                        else:
                            source_state=await client.native_money_state()
                        check()
                        require(source_state.get('availability')=='ready' and source_state.get('address')==policy.source
                            and source_state.get('public_key')==identity['public_key'],'Telegram initial wallet/key changed during simulation')
                    if is_prod:
                        try: cur_min=await client.native_money_minimum()
                        except Exception as exc:
                            if not _is_wallet_unavailable(exc): raise
                            cur_min=minimum
                        try: cur_rec=await client.native_money_recipient(username,target.user_id,prepare_address=False)
                        except Exception as exc:
                            if not _is_wallet_unavailable(exc): raise
                            cur_rec={'address':destination}
                        require(cur_min==minimum,'Transfer minimum changed')
                        require(cur_rec.get('address')==destination)
                    else:
                        require(await client.native_money_minimum()==minimum,'Transfer minimum changed')
                        require((await client.native_money_recipient(username,target.user_id,prepare_address=False)).get('address')==destination)
                    check();require(expiry-self.money.clock()>=30 and 0<=self.money.clock()-latest['checked_at']<=30)
                    with self.money.db() as db:
                        db.execute("UPDATE native_commitments SET seqno=NULL WHERE network=? AND source=? AND intent!=? AND intent IN (SELECT id FROM intents WHERE state IN ('operator_not_sent','cancelled'))", (policy.network, policy.source, ident))
                        row=db.execute('SELECT state FROM intents WHERE id=?',(ident,)).fetchone()
                        require(row and row['state']=='wallet_prompted','Intent changed before signer claim')
                        changed=db.execute("UPDATE native_commitments SET seqno=?,max_debit=?,stage='signing' WHERE intent=? AND stage='preflight'",
                            (plan['seqno'],plan['max_debit_nano'],ident))
                        require(changed.rowcount==1,'Signer claim already consumed')
                        db.execute("UPDATE intents SET destination=?,amount=?,state='uncertain' WHERE id=?",(destination,journal_amount,ident))
                    claimed=True # Durable uncertainty BEFORE any signing key access.
                    phase='signing'
                    if send_all:
                        prepared=self.codec.prepare_drain(network=policy.network,anchor_public_key=anchor_public_key,seqno=plan['seqno'],
                            valid_until=expiry,destination=destination,intent_id=ident,deploy=deploy)
                    else:
                        prepared=self.codec.prepare(network=policy.network,anchor_public_key=anchor_public_key,seqno=plan['seqno'],
                            valid_until=expiry,destination=destination,amount_nano=plan['amount_nano'],intent_id=ident,deploy=deploy,
                            **({'allow_high_caps':True} if cap>10**9 else {}))
                    require(prepared['codec']['source']==policy.source
                        and prepared['codec']['payload_hash']==unsigned['payload_hash'],
                        'Signing payload differs from the unsigned simulation')
                    check()
                    with self.vault.use_seed(**identity) as seed:
                        signature=Ed25519PrivateKey.from_private_bytes(seed).sign(bytes.fromhex(prepared['codec']['payload_hash']))
                    signed=self.codec._run({**prepared['request'],'op':'drain-attach' if send_all else 'attach','signature':signature.hex(),'signing_public_key':identity['public_key']})
                    require(signed.get('payload_hash')==prepared['codec']['payload_hash'] and signed.get('source')==policy.source)
                    boc=base64.b64decode(signed['external_boc'],validate=True)
                    check();require(expiry-self.money.clock()>=30)
                    with self.money.db() as db:
                        require(db.execute('SELECT state FROM intents WHERE id=?',(ident,)).fetchone()['state']=='uncertain')
                        db.execute("UPDATE native_commitments SET stage='dispatching',boc_hash=? WHERE intent=?",(hashlib.sha256(boc).hexdigest(),ident))
                    phase='dispatch'
                    if is_prod:
                        dispatched = False
                        try:
                            async with asyncio.timeout(25):
                                await client.native_money_send(username,target.user_id,destination,boc,random_id,admission_check=check)
                            dispatched = True
                        except Exception as send_exc:
                            if _is_blocked_send_abort(send_exc) or not (_is_wallet_unavailable(send_exc) or wallet_region_unavailable):
                                raise
                            check()
                            require(observed['public_key']==identity['public_key'] and latest['public_key']==identity['public_key'],
                                'On-chain public key mismatch before sendBoc fallback')
                            if hasattr(self.codec, 'inspect_external'):
                                decoded_boc = self.codec.inspect_external(
                                    external_boc=signed['external_boc'],
                                    signing_public_key=identity['public_key'],
                                    network=policy.network,
                                )
                                expected_amount = '0' if send_all else plan['amount_nano']
                                require(
                                    decoded_boc.get('signature_valid') is True
                                    and decoded_boc.get('source') == policy.source
                                    and decoded_boc.get('destination') == destination
                                    and decoded_boc.get('destination') in policy.targets
                                    and decoded_boc.get('amount_nano') == expected_amount
                                    and decoded_boc.get('mode') == plan['send_mode'],
                                    'Signed BOC target/amount/signature mismatch before sendBoc fallback',
                                )
                            import httpx
                            from modules.money_chain_read import MoneyReadError, bounded_json
                            host='toncenter.com' if policy.network=='mainnet' else 'testnet.toncenter.com'
                            tonapi_host='tonapi.io' if policy.network=='mainnet' else 'testnet.tonapi.io'
                            async with httpx.AsyncClient(timeout=httpx.Timeout(30,connect=10),trust_env=False,follow_redirects=False) as http:
                                try:
                                    resp=await bounded_json(http,'POST',f'https://{host}/api/v2/sendBoc',body={'boc':signed['external_boc']})
                                    require(resp.get('ok') is True,'TON Center sendBoc rejected transaction: '+str(resp.get('error')))
                                    dispatched = True
                                except (MoneyReadError, httpx.HTTPError, TimeoutError) as boc_exc:
                                    if isinstance(boc_exc, MoneyReadError) and not boc_exc.retryable:
                                        raise NativeExecutionError(f'TON Center sendBoc HTTP error: {boc_exc}') from None
                                    try:
                                        fallback_resp = await http.post(
                                            f'https://{tonapi_host}/v2/blockchain/message',
                                            json={'boc': signed['external_boc']},
                                            headers={'Accept': 'application/json'},
                                            follow_redirects=False,
                                        )
                                        require(fallback_resp.status_code == 200, f'sendBoc fallback HTTP {fallback_resp.status_code}')
                                        dispatched = True
                                    except (httpx.HTTPError, TimeoutError) as fb_exc:
                                        raise NativeExecutionError(f'sendBoc fallback failed ({type(boc_exc).__name__}/{type(fb_exc).__name__})') from None
                    else:
                        await client.native_money_send(username,target.user_id,destination,boc,random_id,admission_check=check)
                    with self.money.db() as db:
                        # A returned RPC is never enough to mark provider-delivered.
                        db.execute("UPDATE intents SET state='wallet_submitted_unverified' WHERE id=? AND state='uncertain'",(ident,))
                        db.execute("UPDATE native_commitments SET stage='submitted' WHERE intent=?",(ident,))
            return {'id':ident,'state':'wallet_submitted_unverified','confirmed_on_chain':False,
                    'deployment_requested':deploy,'send_all_requested':send_all,'reconciliation_required':True,'automatic_retry':False}
        except BaseException as exc:
            if not claimed:
                with self.money.db() as db:
                    db.execute("UPDATE intents SET state='operator_not_sent' WHERE id=? AND state='wallet_prompted'",(ident,))
                    db.execute("UPDATE native_commitments SET seqno=NULL,stage='preflight_failed' WHERE intent=?",(ident,))
                    if carousel_step is not None:
                        db.execute("DELETE FROM native_transit_claims WHERE intent=?",(ident,))
                        db.execute("UPDATE money_carousel_hops SET intent=NULL WHERE plan=? AND ordinal=? AND intent=?",(carousel_step.ident, carousel_step.ordinal, ident))
            if isinstance(exc,asyncio.CancelledError): raise
            if not isinstance(exc,Exception): raise
            raise NativeExecutionError('Native execution stopped at '+phase+' ('+type(exc).__name__+'); '+('outcome uncertain, reconcile intent '+ident if claimed else 'no signature/submission; failed intent '+ident)) from None
