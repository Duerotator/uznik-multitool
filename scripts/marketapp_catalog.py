"""Explicit one-page read. Token comes from a dedicated vault or environment, never CLI arguments."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (str(ROOT / "app"), str(ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from modules.marketapp_catalog import CatalogError, MarketappCatalogReader, SORTS
from modules.marketapp_credentials import MarketappCredentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-provider-read",
        action="store_true",
        help="Allow one authenticated GET to api.marketapp.org (no purchase)",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--sort-by", choices=sorted(SORTS), default="price_per_day")
    parser.add_argument("--cursor")
    parser.add_argument("--collection-address")
    parser.add_argument("--model")
    parser.add_argument("--symbol")
    parser.add_argument("--backdrop")
    parser.add_argument("--include-sellable", action="store_true")
    args = parser.parse_args()
    try:
        if not args.confirm_provider_read:
            raise CatalogError("Explicit provider-read confirmation is required")
        result = asyncio.run(
            MarketappCatalogReader().read(
                token=MarketappCredentials(args.data_dir).get(),
                confirmed=True,
                limit=args.limit,
                sort_by=args.sort_by,
                cursor=args.cursor,
                collection_address=args.collection_address,
                model=args.model,
                symbol=args.symbol,
                backdrop=args.backdrop,
                include_sellable=args.include_sellable,
            )
        )
    except CatalogError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
