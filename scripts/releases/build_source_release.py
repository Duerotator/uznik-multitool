"""Build a source-only ZIP from an exact Git commit, never from local data."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[2]


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
    ).stdout


def validate_path(name: str) -> None:
    path = PurePosixPath(name)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError(f"Unsafe archive path: {name}")
    lower = name.lower()
    basename = path.name.lower()
    if lower == "config/.env.example":
        return
    if (basename.startswith(".env") or re.search(r"\.(session|sqlite|db)", lower)
            or path.suffix.lower() in {".log", ".pem", ".key", ".p12", ".ovpn", ".uzbk"}
            or basename in {"batch_phones.txt", "промт.txt"}
            or path.parts[0] in {".venv", "venv", "dist", "build", ".git"}):
        raise ValueError(f"Sensitive/runtime file tracked in Git: {name}")
    private_tree = path.parts[0] in {"data", "imports", "templates"}
    private_tree |= lower.startswith(("assets/avatar_packs/", "assets/avatar_packs_raw/"))
    if private_tree and basename not in {"readme.md", ".gitkeep"}:
        raise ValueError(f"Personal data tracked in Git: {name}")
    if path.parts[0] == "sessions" and basename not in {"readme.md", ".gitkeep"}:
        if path.suffix.lower() != ".bat":
            raise ValueError(f"Unexpected session input tracked in Git: {name}")


def build(root: Path, ref: str, output: Path) -> dict[str, str]:
    # --end-of-options prevents a supplied ref from becoming a Git option.
    commit = git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    tree = git(root, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    files: dict[str, tuple[bytes, int]] = {}
    with tarfile.open(fileobj=io.BytesIO(git(root, "archive", "--format=tar", commit))) as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            validate_path(member.name)
            if not member.isfile():
                raise ValueError(f"Links and special files are not allowed: {member.name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Missing archive content: {member.name}")
            files[member.name] = (stream.read(), member.mode)
    info = {"format": "uznik-source-v1", "commit": commit, "tree": tree}
    files["BUILD_INFO.json"] = ((json.dumps(info, indent=2) + "\n").encode(), 0o644)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, (content, mode) in sorted(files.items()):
            entry = zipfile.ZipInfo(f"uznik-multitool/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100000 | mode) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    content = buffer.getvalue()
    digest = hashlib.sha256(content).hexdigest()
    filename = f"uznik-multitool-{commit[:12]}-source.zip"
    output.mkdir(parents=True, exist_ok=True)
    (output / filename).write_bytes(content)
    (output / "SHA256SUMS.txt").write_text(f"{digest}  {filename}\n", encoding="utf-8")
    manifest = {**info, "archive": filename, "sha256": digest}
    (output / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Existing Git commit or tag")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/source")
    args = parser.parse_args()
    try:
        result = build(ROOT, args.ref, args.output.resolve())
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Source release refused: {exc}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
