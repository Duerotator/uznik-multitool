"""Read-only story listing shared by both MTProto adapters."""
from datetime import datetime, timezone


def describe_story(story):
    media = getattr(story, "media", None)
    photo = getattr(media, "photo", None)
    document = getattr(media, "document", None)
    kind = "photo" if photo else "video" if str(getattr(document, "mime_type", "")).startswith("video/") else "unknown"
    date = getattr(story, "date", 0)
    if isinstance(date, datetime):
        date = int((date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp())
    return {
        "id": int(story.id), "kind": kind, "media_id": str(getattr(photo or document, "id", "")),
        "pinned": bool(getattr(story, "pinned", False)), "public": bool(getattr(story, "public", False)),
        "date": int(date or 0), "caption": str(getattr(story, "caption", "") or "")[:500],
    }


async def collect_stories(invoke, functions, types, peer, *, suffix="", limit=500):
    def request(name, **kwargs):
        return getattr(functions, name+suffix)(peer=peer, **kwargs)
    entries = {}
    complete = True
    def collect(items):
        nonlocal complete
        for story in items:
            if not isinstance(story, types.StoryItem) or getattr(story, "min", False):
                complete = False
                continue
            row = describe_story(story)
            if row["kind"] == "unknown" or not row["media_id"]:
                complete = False
            if row["id"] in entries and entries[row["id"]] != row:
                raise RuntimeError("Stories changed while scanning; reopen the review")
            entries[row["id"]] = row
    active = await invoke(request("GetPeerStories"))
    container = getattr(active, "stories", None)
    active_items = getattr(container, "stories", None)
    if not isinstance(active_items, (list, tuple)):
        raise RuntimeError("Telegram returned an incomplete active-story snapshot")
    collect(active_items)
    for method in ("GetPinnedStories", "GetStoriesArchive"):
        offset = 0
        seen = set()
        for _ in range(limit//100+1):
            response = await invoke(request(method, offset_id=offset, limit=100))
            page = list(getattr(response, "stories", []) or [])
            collect(page)
            ids = {int(story.id) for story in page if getattr(story, "id", 0) > 0}
            seen.update(ids)
            count = getattr(response, "count", None)
            if type(count) is not int or count < len(seen):
                complete = False
            if not page or len(page) < 100 or (type(count) is int and len(seen) >= count):
                if type(count) is int and len(seen) < count:
                    complete = False
                break
            cursor = min(ids) if ids else 0
            if len(seen) >= limit or not cursor or (offset and cursor >= offset):
                complete = False
                break
            offset = cursor
        else:
            complete = False
    if len(entries) > limit:
        complete = False
    return {"stories": sorted(entries.values(), key=lambda row: row["id"], reverse=True)[:limit],
            "complete": complete}
