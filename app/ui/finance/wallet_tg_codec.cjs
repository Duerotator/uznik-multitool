'use strict';
// Local public-data codec, NOT imported by index.js or served as a web asset.
// Protocol reference: ton-blockchain/tg-wallet-contract @ 1a29ae5d90e03c3d585d3e42e6ecef691d727a3c.
const {beginCell, Cell, Address, contractAddress, internal, external, loadMessage, loadMessageRelaxed, storeMessage, storeMessageRelaxed} = require('@ton/core');
const {createPublicKey, verify} = require('node:crypto');
if (require('@ton/core/package.json').version !== '0.63.1') throw new Error('Unsupported core version');

const SAFE_ERROR_MESSAGES = new Set([
  'Unsupported core version', 'Trampoline mismatch', 'Unexpected fields', 'Invalid hex',
  'Invalid uint32', 'Invalid public cell', 'Invalid public cell encoding', 'Expected one root cell',
  'Unknown network', 'Invalid initial deployment', 'Invalid dedicated drain request',
  'Unsupported channel/mode', 'Invalid recipient', 'Invalid intent ID', 'Invalid bounded amount',
  'Unknown initial implementation', 'Unknown WalletTg implementation', 'Unsupported WalletTg storage',
  'Unknown operation', 'Only external pre-sign emulation supported', 'Invalid signature',
  'Invalid external message', 'Invalid external payload', 'Codec input too large',
]);
function errorKind(err) {
  if (!err) return 'Unknown error';
  if (err instanceof SyntaxError) return 'Invalid JSON input';
  const msg = typeof err.message === 'string' ? err.message : '';
  if (SAFE_ERROR_MESSAGES.has(msg)) return msg;
  const name = typeof err.name === 'string' ? err.name.replace(/[^A-Za-z0-9_.-]/g, '') : 'Error';
  return name || 'Error';
}

const CODE_HASH = '9149ae51c1e4689710cebf7830297b16acfbadb363a920a537893e7ffeeca768';
const CODE_BOC = 'b5ee9c7241010101001a000030ff00209821d7498308b9f240df8085f833d0ed1e20ed53d969427e39';
const SUBWALLET = {mainnet:0x7fff7f11, testnet:0x7fff7ffd};
const IMPLEMENTATION_HASH = 'e30911420bef1191c09dce58b9df2b4ca4c2d9c383cc3b6a91170349ffa70e2c';
const CODE = Cell.fromBoc(Buffer.from(CODE_BOC,'hex'))[0];
if (CODE.hash().toString('hex') !== CODE_HASH) throw new Error('Trampoline mismatch');

function exact(value, keys) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join(',') !== keys.slice().sort().join(',')) throw new Error('Unexpected fields');
}
function hex(value, length) {
  if (typeof value !== 'string' || value.length !== length*2 || !/^[0-9a-f]+$/.test(value)) throw new Error('Invalid hex');
  return Buffer.from(value,'hex');
}
function uint32(value) { if (!Number.isInteger(value) || value<0 || value>0xffffffff) throw new Error('Invalid uint32'); return value; }
function cell(value) {
  if(typeof value!=='string' || value.length>32768 || !/^[A-Za-z0-9+/]+={0,2}$/.test(value))throw new Error('Invalid public cell');
  const bytes=Buffer.from(value,'base64');if(bytes.toString('base64')!==value)throw new Error('Invalid public cell encoding');
  const cells=Cell.fromBoc(bytes);if(cells.length!==1)throw new Error('Expected one root cell');return cells[0];
}
function identity(p) {
  if (typeof p.network !== 'string' || !Object.hasOwn(SUBWALLET,p.network)) throw new Error('Unknown network');
  const anchor = hex(p.anchor_public_key,32);
  const data = beginCell().storeUint(0,8).storeUint(0,32).storeUint(SUBWALLET[p.network],32).storeBuffer(anchor).endCell();
  const init = {code:CODE,data};
  return {source:contractAddress(0,init).toRawString(), init, subwallet_id:SUBWALLET[p.network]};
}
const FIELDS = ['op','network','anchor_public_key','seqno','valid_until','destination','amount_nano','intent_id','channel','mode'];
function prepare(p) {
  const owner = identity(p);
  uint32(p.seqno); uint32(p.valid_until);
  if(Object.hasOwn(p,'deploy') && (p.deploy!==true || p.channel!=='external' || p.seqno!==0 || ![3,130].includes(p.mode)))throw new Error('Invalid initial deployment');
  if(Object.hasOwn(p,'allow_high_caps') && p.allow_high_caps!==true)throw new Error('Unexpected fields');
  const drain=['emulate','drain-prepare','drain-attach'].includes(p.op) && p.channel==='external' && p.mode===130 && p.amount_nano==='0';
  if(p.op.startsWith('drain-') && !drain)throw new Error('Invalid dedicated drain request');
  if (!['external','internal'].includes(p.channel) || (p.mode!==3 && !drain)) throw new Error('Unsupported channel/mode');
  if (typeof p.destination!=='string' || !/^(0|-1):[0-9a-f]{64}$/.test(p.destination) || p.destination===owner.source) throw new Error('Invalid recipient');
  if (typeof p.intent_id!=='string' || !/^[0-9a-f]{32}$/.test(p.intent_id)) throw new Error('Invalid intent ID');
  const allowHigh = p.allow_high_caps === true;
  const maxNano = allowHigh ? 100000000000n : 1000000000n;
  const amtPattern = allowHigh ? /^[1-9][0-9]{0,11}$/ : /^[1-9][0-9]{0,9}$/;
  if (!drain && (typeof p.amount_nano!=='string' || !amtPattern.test(p.amount_nano) || BigInt(p.amount_nano)>maxNano)) throw new Error('Invalid bounded amount');
  const outgoing=internal({to:Address.parseRaw(p.destination),value:BigInt(p.amount_nano),bounce:false,body:'accmanager:'+p.intent_id});
  const message=beginCell().store(storeMessageRelaxed(outgoing)).endCell();
  const opcode=p.channel==='external'?0x63896e75:0x63896e74;
  const payload=beginCell().storeUint(opcode,32).storeUint(owner.subwallet_id,32).storeUint(p.valid_until,32).storeUint(p.seqno,32).storeUint(p.mode,8).storeRef(message).endCell();
  return {owner,message,payload,opcode};
}
function run(p) {
  if(p && p.op==='inspect-initial') {
    exact(p,['op','network','anchor_public_key','implementation_boc']);
    const owner=identity(p),implementation=cell(p.implementation_boc);
    if(implementation.hash().toString('hex')!==IMPLEMENTATION_HASH)throw new Error('Unknown initial implementation');
    const next=beginCell().storeUint(0,8).storeUint(1,32).storeUint(owner.subwallet_id,32).storeBuffer(hex(p.anchor_public_key,32)).endCell();
    return {source:owner.source,subwallet_id:owner.subwallet_id,public_key:p.anchor_public_key,seqno:0,
      initial_code_hash:CODE_HASH,initial_data_hash:owner.init.data.hash().toString('hex'),
      initial_next_data_hash:next.hash().toString('hex'),implementation_hash:IMPLEMENTATION_HASH};
  }
  if(p && p.op==='inspect-state') {
    exact(p,['op','network','anchor_public_key','code_boc','data_boc','implementation_boc']);
    const owner=identity(p),code=cell(p.code_boc),implementation=cell(p.implementation_boc);
    if(code.hash().toString('hex')!==CODE_HASH || implementation.hash().toString('hex')!==IMPLEMENTATION_HASH)throw new Error('Unknown WalletTg implementation');
    const dataCell=cell(p.data_boc),data=dataCell.beginParse();const revision=data.loadUint(8),seqno=data.loadUint(32),subwallet=data.loadUint(32),publicKey=data.loadBuffer(32).toString('hex');
    if(revision!==0 || data.remainingBits || data.remainingRefs || subwallet!==owner.subwallet_id)throw new Error('Unsupported WalletTg storage');
    return {source:owner.source,storage_revision:revision,seqno,subwallet_id:subwallet,public_key:publicKey,data_hash:dataCell.hash().toString('hex'),code_hash:CODE_HASH,implementation_hash:IMPLEMENTATION_HASH};
  }
  if (p && p.op==='derive') {
    exact(p,['op','network','anchor_public_key']); const owner=identity(p);
    return {source:owner.source,subwallet_id:owner.subwallet_id,trampoline_hash:CODE_HASH};
  }
  if (p && p.op==='inspect-external') {
    exact(p,['op','network','external_boc','signing_public_key']);
    if (typeof p.network !== 'string' || !Object.hasOwn(SUBWALLET,p.network)) throw new Error('Unknown network');
    const publicKey=hex(p.signing_public_key,32);
    const envelope=cell(p.external_boc);
    const outer=loadMessage(envelope.beginParse());
    if (!outer || outer.info.type!=='external-in' || !Address.isAddress(outer.info.dest)) throw new Error('Invalid external message');
    const bs=outer.body.beginParse();
    if (bs.remainingBits < 512 + 136 || bs.remainingRefs !== 1) throw new Error('Invalid external payload');
    const signature=bs.loadBuffer(64);
    const payloadCell=bs.asCell();
    const key=createPublicKey({key:Buffer.concat([Buffer.from('302a300506032b6570032100','hex'),publicKey]),format:'der',type:'spki'});
    if (!verify(null,payloadCell.hash(),key,signature)) throw new Error('Invalid signature');
    const ps=payloadCell.beginParse();
    const opcode=ps.loadUint(32), subwalletId=ps.loadUint(32), validUntil=ps.loadUint(32), seqno=ps.loadUint(32), mode=ps.loadUint(8);
    const innerCell=ps.loadRef();
    if (ps.remainingBits || ps.remainingRefs || opcode!==0x63896e75 || subwalletId!==SUBWALLET[p.network]) throw new Error('Invalid external payload');
    const inner=loadMessageRelaxed(innerCell.beginParse());
    if (!inner || inner.info.type!=='internal' || !Address.isAddress(inner.info.dest)) throw new Error('Invalid external payload');
    return {source:outer.info.dest.toRawString(),subwallet_id:subwalletId,opcode,valid_until:validUntil,seqno,mode,
      destination:inner.info.dest.toRawString(),amount_nano:inner.info.value.coins.toString(),
      signature_valid:true,payload_hash:payloadCell.hash().toString('hex')};
  }
  const fields=FIELDS.concat(Object.hasOwn(p,'deploy')?['deploy']:[],Object.hasOwn(p,'allow_high_caps')?['allow_high_caps']:[]);
  exact(p,p && ['attach','drain-attach'].includes(p.op)?fields.concat(['signature','signing_public_key']):fields);
  if (!['prepare','attach','emulate','drain-prepare','drain-attach'].includes(p.op)) throw new Error('Unknown operation');
  const {owner,message,payload,opcode}=prepare(p);
  const result={source:owner.source,subwallet_id:owner.subwallet_id,trampoline_hash:CODE_HASH,opcode,
    payload_hash:payload.hash().toString('hex'),payload_boc:payload.toBoc().toString('base64'),
    message_hash:message.hash().toString('hex')};
  if(p.deploy) {
    result.initial_data_hash=owner.init.data.hash().toString('hex');
    result.initial_next_data_hash=beginCell().storeUint(0,8).storeUint(1,32).storeUint(owner.subwallet_id,32).storeBuffer(hex(p.anchor_public_key,32)).endCell().hash().toString('hex');
  }
  if(p.op==='emulate') {
    if(p.channel!=='external')throw new Error('Only external pre-sign emulation supported');
    const body=beginCell().storeBuffer(Buffer.alloc(64)).storeSlice(payload.beginParse()).endCell();
    const envelope=beginCell().store(storeMessage(external({to:Address.parseRaw(owner.source),body,init:p.deploy?owner.init:undefined}),{forceRef:true})).endCell();
    result.unsigned_external_boc=envelope.toBoc().toString('base64');
    result.unsigned_body_hash=body.hash().toString('hex');
    result.signature_valid=false;result.broadcastable=false;
  }
  if(['attach','drain-attach'].includes(p.op)) {
    const signature=hex(p.signature,64),publicKey=hex(p.signing_public_key,32);
    const key=createPublicKey({key:Buffer.concat([Buffer.from('302a300506032b6570032100','hex'),publicKey]),format:'der',type:'spki'});
    if (!verify(null,payload.hash(),key,signature)) throw new Error('Invalid signature');
    const body=beginCell().storeBuffer(signature).storeSlice(payload.beginParse()).endCell();
    result.signed_body_boc=body.toBoc().toString('base64');
    result.signed_body_hash=body.hash().toString('hex');
    if(p.channel==='external') {
      const envelope=beginCell().store(storeMessage(external({to:Address.parseRaw(owner.source),body,init:p.deploy?owner.init:undefined}),{forceRef:true})).endCell();
      result.external_boc=envelope.toBoc().toString('base64');
      result.external_cell_hash=envelope.hash().toString('hex');
    }
  }
  return result;
}
module.exports={run};
if(require.main===module) {
  let input=''; process.stdin.setEncoding('utf8');
  process.stdin.on('data',chunk=>{input+=chunk;if(Buffer.byteLength(input)>65536){process.stderr.write('Codec input too large\n');process.exit(1);}});
  process.stdin.on('end',()=>{try{process.stdout.write(JSON.stringify(run(JSON.parse(input))));}
    catch(err){process.stderr.write(`WalletTg codec rejected input: ${errorKind(err)}\n`);process.exitCode=1;}});
}
