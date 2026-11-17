"""Own hosted collectible metadata and visibility only; never transfer/withdraw."""
import re
from core.ton_address import normalize_address


async def list_own_gifts(invoke, functions, types, peer, *, suffix=""):
    request = getattr(functions, "GetSavedStarGifts" + suffix, None)
    if request is None or not hasattr(types, "InputSavedStarGiftSlug"):
        raise NotImplementedError("Hosted gifts require a newer Telegram layer")
    rows, seen_offsets, offset, received = {}, set(), "", 0
    for _ in range(5):
        response = await invoke(request(peer=peer, offset=offset, limit=100))
        page = getattr(response, "gifts", None)
        count = getattr(response, "count", None)
        if not isinstance(page, (list, tuple)) or len(page) > 100 or type(count) is not int or count < 0:
            raise ValueError("Invalid own gifts response")
        received += len(page)
        for saved in page:
            gift = getattr(saved, "gift", None)
            if not isinstance(gift, types.StarGiftUnique) or not getattr(gift, "gift_address", None):
                continue
            address = normalize_address(gift.gift_address, "mainnet").raw
            slug = getattr(gift, "slug", None)
            if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}-[0-9]{1,20}", slug):
                raise ValueError("Unsupported collectible slug")
            host = getattr(gift, "host_id", None)
            host_id = getattr(host, "user_id", None) if isinstance(host, types.PeerUser) else None
            row = {"nft_address": address, "slug": slug,
                   "host_user_id": host_id, "visible": not bool(getattr(saved, "unsaved", False))}
            if address in rows:
                raise ValueError("Duplicate gift while reading Telegram")
            rows[address] = row
        cursor = getattr(response, "next_offset", None)
        if not cursor:
            return {"gifts": list(rows.values()), "complete": received == count}
        if not isinstance(cursor, str) or len(cursor) > 2048 or cursor in seen_offsets or not page:
            raise ValueError("Invalid gift pagination")
        seen_offsets.add(cursor)
        offset = cursor
    return {"gifts": list(rows.values()), "complete": False}


async def set_visibility(invoke, functions, types, slug, visible, *, suffix=""):
    if type(visible) is not bool or not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,100}-[0-9]{1,20}", slug):
        raise ValueError("Choose exact collectible and visibility")
    method = getattr(functions, "SaveStarGift" + suffix, None)
    if method is None or not hasattr(types, "InputSavedStarGiftSlug"):
        raise NotImplementedError("Gift visibility requires a newer Telegram layer")
    result = await invoke(method(stargift=types.InputSavedStarGiftSlug(slug=slug), unsave=not visible))
    if result is not True:
        raise ValueError("Telegram did not confirm visibility")
