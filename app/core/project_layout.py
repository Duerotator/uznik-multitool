"""Create the desktop's empty working folders without importing user data."""
from __future__ import annotations

from pathlib import Path

DATA_FOLDERS = (
    "sessions/pyrogram", "sessions/telethon", "sessions/archive", "passkeys",
    "logs", "groups", "scenarios", "profile_snapshots", "profiles_archive",
    "browser_profiles", "vpn_gateway", "backups",
)
IMPORT_FOLDERS = ("auth_input", "auth_input/processed", "emails")
RESOURCE_FOLDERS = (
    "templates", "assets/avatar_packs/male", "assets/avatar_packs/female",
    "assets/avatar_packs/unknown", "assets/avatar_packs_raw",
)
EMPTY_TEMPLATES = ("first_names.txt", "last_names.txt", "usernames.txt", "bios.txt", "genders.txt")


def ensure_project_layout(root: Path, data_dir: Path, import_dir: Path,
                          batch_phones_file: Path | None = None) -> None:
    for base, folders in ((data_dir, DATA_FOLDERS), (import_dir, IMPORT_FOLDERS),
                          (root, (*RESOURCE_FOLDERS, "sessions"))):
        for folder in folders:
            (base / folder).mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves every existing user template.
    for name in EMPTY_TEMPLATES:
        try:
            (root / "templates" / name).touch(exist_ok=False)
        except FileExistsError:
            pass
    # Keep all session input and launchers together in the visible project-root
    # folder. Migrate the previous import-folder location without overwriting.
    phone_list = batch_phones_file or root / "sessions" / "batch_phones.txt"
    phone_list.parent.mkdir(parents=True, exist_ok=True)
    old_phone_lists = (import_dir / "sessions" / "batch_phones.txt", import_dir / "batch_phones.txt")
    if not phone_list.exists():
        for old_phone_list in old_phone_lists:
            if old_phone_list.exists():
                old_phone_list.rename(phone_list)
                break
    if not phone_list.exists():
        phone_list.parent.mkdir(parents=True, exist_ok=True)
        try:
            with phone_list.open("x", encoding="utf-8") as file:
                file.write("# Your own phone numbers, one per line in international format.\n")
        except FileExistsError:
            pass
    mail_list = import_dir / "emails/accounts.txt"
    try:
        with mail_list.open("x", encoding="utf-8") as file:
            file.write("# Your own mailboxes: email:password;host;port (one per line).\n")
    except FileExistsError:
        pass
