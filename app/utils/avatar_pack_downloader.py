from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from utils.profile_generator import IMAGE_EXTENSIONS


DEFAULT_INCLUDE_TERMS = ("100", "7000", "jdm", "mem", "meme", "мем")
MALE_TERMS = ("male", "men", "man", "boy", "boys", "m", "муж", "парни")
FEMALE_TERMS = ("female", "women", "woman", "girl", "girls", "f", "жен", "дев")


@dataclass
class AvatarPackImportResult:
    downloaded_to: Path
    copied: int
    male: int
    female: int
    unknown: int
    skipped: int
    stopped_at_limit: bool = False
    download_warning: str = ""


def download_google_drive_avatar_pack(
    url: str,
    raw_dir: Path,
    pack_dir: Path,
    include_terms: tuple[str, ...] = DEFAULT_INCLUDE_TERMS,
    max_files: int = 15000,
) -> AvatarPackImportResult:
    raw_dir.mkdir(parents=True, exist_ok=True)
    pack_dir.mkdir(parents=True, exist_ok=True)
    stopped_at_limit = _download_until_limit(
        url,
        raw_dir,
        include_terms=include_terms,
        max_files=max_files,
    )
    result = organize_avatar_pack(raw_dir, pack_dir, include_terms=include_terms, max_files=max_files)
    result.stopped_at_limit = stopped_at_limit
    if not stopped_at_limit:
        usable = _count_usable_images(raw_dir, include_terms)
        if usable < max_files:
            result.download_warning = (
                f"Google Drive download stopped before the limit. "
                f"Usable files found: {usable}/{max_files}."
            )
    return result


def organize_avatar_pack(
    raw_dir: Path,
    pack_dir: Path,
    include_terms: tuple[str, ...] = DEFAULT_INCLUDE_TERMS,
    max_files: int = 15000,
) -> AvatarPackImportResult:
    counters = {"male": 0, "female": 0, "unknown": 0}
    copied = 0
    skipped = 0
    include_terms_lower = tuple(term.lower() for term in include_terms if term)

    for source in sorted(raw_dir.rglob("*")):
        if copied >= max_files:
            skipped += 1
            continue
        if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        category = _category_for(source, raw_dir)
        if category == "unknown" and include_terms_lower and not _matches_terms(source, raw_dir, include_terms_lower):
            skipped += 1
            continue

        destination_dir = pack_dir / category
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / _stable_name(source)
        if not destination.exists():
            shutil.copy2(source, destination)
        counters[category] += 1
        copied += 1

    return AvatarPackImportResult(
        downloaded_to=raw_dir,
        copied=copied,
        male=counters["male"],
        female=counters["female"],
        unknown=counters["unknown"],
        skipped=skipped,
    )


def _category_for(path: Path, root: Path) -> str:
    tokens = _tokens(path, root)
    if any(token in MALE_TERMS for token in tokens):
        return "male"
    if any(token in FEMALE_TERMS for token in tokens):
        return "female"
    return "unknown"


def _download_until_limit(
    url: str,
    raw_dir: Path,
    include_terms: tuple[str, ...],
    max_files: int,
) -> bool:
    try:
        import gdown
    except ImportError as exc:
        raise RuntimeError(
            "gdown is not installed. Run: .\\.venv\\Scripts\\python.exe -m pip install gdown"
        ) from exc

    output_root = str(raw_dir) + os.sep
    files = gdown.download_folder(
        url=url,
        output=output_root,
        quiet=False,
        use_cookies=False,
        skip_download=True,
        resume=True,
    )

    failed = 0
    usable = _count_usable_images(raw_dir, include_terms)
    for item in files:
        print(f"Usable avatar images found: {usable}/{max_files}", flush=True)
        if usable >= max_files:
            return True

        local_path = Path(item.local_path)
        if local_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        if local_path.exists() and local_path.stat().st_size > 0:
            continue
        if not _is_usable_avatar_path(local_path, raw_dir, include_terms):
            continue

        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            gdown.download(
                url=f"https://drive.google.com/uc?id={item.id}",
                output=str(local_path),
                quiet=False,
                use_cookies=False,
                resume=True,
            )
        except Exception as exc:  # noqa: BLE001 - bad Drive files should not stop the pack
            failed += 1
            print(
                f"Skipped Google Drive file {item.id} ({item.path}): {exc}",
                flush=True,
            )
        usable = _count_usable_images(raw_dir, include_terms)

    print(f"Usable avatar images found: {usable}/{max_files}", flush=True)
    if failed:
        print(
            f"Skipped {failed} failed Google Drive file(s); organizing downloaded files.",
            flush=True,
        )
    return usable >= max_files


def _count_usable_images(
    raw_dir: Path,
    include_terms: tuple[str, ...],
) -> int:
    include_terms_lower = tuple(term.lower() for term in include_terms if term)
    count = 0
    for path in raw_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        category = _category_for(path, raw_dir)
        if category != "unknown" or not include_terms_lower or _matches_terms(path, raw_dir, include_terms_lower):
            count += 1
    return count


def _is_usable_avatar_path(
    path: Path,
    raw_dir: Path,
    include_terms: tuple[str, ...],
) -> bool:
    category = _category_for(path, raw_dir)
    include_terms_lower = tuple(term.lower() for term in include_terms if term)
    return category != "unknown" or not include_terms_lower or _matches_terms(path, raw_dir, include_terms_lower)


def _matches_terms(path: Path, root: Path, terms: tuple[str, ...]) -> bool:
    text = " ".join(_tokens(path, root))
    return any(term in text for term in terms)


def _tokens(path: Path, root: Path) -> list[str]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    return [part.lower() for part in relative.parts]


def _stable_name(path: Path) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8", errors="ignore")).hexdigest()[:12]
    stem = "".join(char if char.isalnum() else "_" for char in path.stem.lower()).strip("_")
    stem = stem[:42] or "avatar"
    return f"{stem}_{digest}{path.suffix.lower()}"
