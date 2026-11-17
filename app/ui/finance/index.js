import { TonConnectUI } from '@tonconnect/ui';
import TonConnect from '@tonconnect/sdk';
import { beginCell, Cell } from '@ton/core';

// Session bridge secrets stay in this browser tab, not localStorage or the backend.
const session = new Map();
export function connectUI(manifestUrl, buttonRootId) {
  const connector = new TonConnect({manifestUrl, analytics:{mode:'off'},
    storage: {getItem: async k => session.get(k) ?? null,
      setItem: async (k,v) => {session.set(k,v);}, removeItem: async k => {session.delete(k);}}});
  return new TonConnectUI({connector, buttonRootId, language:'ru', restoreConnection:false, analytics:{mode:'off'}});
}
export function commentPayload(text) {
  return beginCell().storeUint(0,32).storeStringTail(text).endCell().toBoc().toString('base64');
}
export function receiptHash(boc) { return Cell.fromBase64(boc).hash().toString('hex'); }
