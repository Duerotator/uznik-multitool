"""Download the user's avatar pack or organize existing local images."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Download your own Google Drive avatar folder; no preset packs are supplied.")
    parser.add_argument("--url", help="Your Google Drive folder URL.")
    parser.add_argument("--raw-dir", type=Path, default=ROOT / "assets/avatar_packs_raw")
    parser.add_argument("--pack-dir", type=Path, default=ROOT / "assets/avatar_packs")
    parser.add_argument("--max-files", type=int, default=15000)
    parser.add_argument("--organize-only", action="store_true")
    parser.add_argument("--include", nargs="*", default=[], help="Optional filename/folder terms for unknown-category photos; default: all images")
    args = parser.parse_args()
    if not args.organize_only and not args.url:
        parser.error("--url is required unless --organize-only is used")
    if args.max_files < 1:
        parser.error("--max-files must be positive")
    os.chdir(ROOT)
    from utils.avatar_pack_downloader import download_google_drive_avatar_pack, organize_avatar_pack
    if args.organize_only:
        result = organize_avatar_pack(args.raw_dir, args.pack_dir, include_terms=tuple(args.include), max_files=args.max_files)
    else:
        result = download_google_drive_avatar_pack(args.url, args.raw_dir, args.pack_dir, include_terms=tuple(args.include), max_files=args.max_files)
    print(f"Organized: {result.copied} images; male={result.male}, female={result.female}, unknown={result.unknown}")
    if result.download_warning:
        print(f"Warning: {result.download_warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
