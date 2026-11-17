"""Narrow native send wire. No secret export, gasless alternative or retry loop.

Pinned Telegram Desktop api.tl a7f453612f4896ac04d0007a36ab0d377e5bcbcf.
Only the integrated local executor should call this financial mutation primitive.
"""
import asyncio
import math
import struct

from modules.money_native_tl import install
from modules.money_native_transport import constructor

SEND=0xd9e5d9e2


def send_request(backend,boc,*,user_id,access_hash,random_id):
    if (not isinstance(boc,bytes) or not 1<=len(boc)<=16384
            or type(user_id) is not int or not 0<user_id<2**63
            or type(access_hash) is not int or not -(2**63)<=access_hash<2**63
            or type(random_id) is not int or not 0<random_id<2**63):
        raise ValueError('Invalid bounded native submission')
    prefix=bytes([len(boc)]) if len(boc)<254 else b'\xfe'+len(boc).to_bytes(3,'little')
    wire=struct.pack('<II',SEND,0)+prefix+boc+bytes(-(len(prefix)+len(boc))%4)
    wire+=struct.pack('<Iqqq',0xf21158c6,user_id,access_hash,random_id)
    install(backend)
    if backend=='pyrogram':
        from pyrogram.raw.core import TLObject
        class Request(TLObject):
            ID=SEND
            QUALNAME='functions.WalletSendTransfer'
            def write(self,*args): return wire
        return Request()
    if backend=='telethon':
        from telethon.tl.tlobject import TLRequest
        class Request(TLRequest):
            CONSTRUCTOR_ID=SEND
            def _bytes(self): return wire
            def to_dict(self): return {'_':'WalletSendTransfer','financial_payload':'redacted'}
        return Request()
    raise ValueError('Unsupported native send backend')


def admit(check):
    if check is not None and (not callable(check) or check() is not True):
        raise ValueError('Native submission admission revoked')


async def submit(invoke,backend,boc,*,user_id,access_hash,random_id,admission_check=None):
    request=send_request(backend,boc,user_id=user_id,access_hash=access_hash,random_id=random_id)
    async with asyncio.timeout(20):
        admit(admission_check)
        await invoke(request)
    # An Updates response alone is NOT a transaction receipt or delivery proof.
    return {'rpc_returned':True,'confirmed_on_chain':False,'automatic_retry':False}


def transfer_minimum(response):
    config=getattr(response,'config',None)
    if constructor(config)!=0x99c1d49d: raise ValueError('Missing Money app configuration')
    rows=getattr(config,'value',None)
    if not isinstance(rows,(tuple,list)) or len(rows)>10000: raise ValueError('Invalid app configuration')
    matches=[row for row in rows if getattr(row,'key',None)=='wallet_transfer_min_nanos']
    if len(matches)!=1: raise ValueError('Current wallet transfer minimum unavailable; no guessed default')
    value=getattr(matches[0],'value',None);number=getattr(value,'value',None)
    if (constructor(value)!=0x2be0dfa4 or type(number) not in (int,float)
            or not math.isfinite(number) or int(number)!=number or not 0<number<=10**9):
        raise ValueError('Invalid exact wallet transfer minimum')
    return str(int(number))
