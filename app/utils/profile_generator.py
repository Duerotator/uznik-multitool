from __future__ import annotations

import random
import re
import shutil
from pathlib import Path
from typing import Any

from core.models import AccountRecord
from core.private_storage import write_private_json


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

MALE_FIRST_NAMES = [
    "Alex", "Adam", "Adrian", "Andrew", "Anton", "Arthur", "Ben", "Brian",
    "Chris", "Daniel", "David", "Dennis", "Dylan", "Ethan", "Felix", "George",
    "Henry", "Ian", "Jack", "James", "Jason", "Leo", "Liam", "Logan",
    "Lucas", "Mark", "Martin", "Mason", "Max", "Michael", "Nick", "Noah",
    "Oliver", "Oscar", "Paul", "Ryan", "Sam", "Simon", "Tim", "Victor",
    "Ivan", "Nikita", "Mikhail", "Artem", "Denis", "Roman", "Kirill", "Pavel",
]

FEMALE_FIRST_NAMES = [
    "Alice", "Alina", "Anna", "Ava", "Bella", "Chloe", "Clara", "Daria",
    "Diana", "Elena", "Ella", "Emma", "Eva", "Grace", "Ivy", "Julia",
    "Kate", "Kira", "Lana", "Laura", "Lina", "Lisa", "Maya", "Mia",
    "Mila", "Mira", "Nina", "Nora", "Olivia", "Polina", "Ruby", "Sara",
    "Sofia", "Tanya", "Vera", "Victoria", "Zoe", "Maria", "Irina", "Olga",
    "Anya", "Karina", "Rita", "Lily", "Sasha", "Eva", "Anastasia", "Dasha",
]

MALE_FIRST_NAMES.extend([
    "Sergey", "Dmitry", "Andrey", "Ilya", "Vasily", "Egor", "Matvey", "Danil",
    "Ruslan", "Albert", "Amir", "Arsen", "Lev", "Yuri", "Maxim", "Vanya",
    "Mike", "Jon", "Sean", "Tyler", "Kevin", "Aaron", "Nathan", "Caleb",
])

FEMALE_FIRST_NAMES.extend([
    "Oksana", "Natalia", "Svetlana", "Marina", "Valeria", "Yana", "Lera",
    "Masha", "Katya", "Nastya", "Alyona", "Vlada", "Milana", "Arina",
    "Emily", "Hannah", "Megan", "Nicole", "Sophie", "Amelia", "Isla", "Leah",
])

LAST_NAMES = [
    "Adams", "Allen", "Bailey", "Baker", "Bell", "Bennett", "Blake", "Brooks",
    "Brown", "Carter", "Cole", "Collins", "Cook", "Cooper", "Davis", "Evans",
    "Fisher", "Ford", "Foster", "Gray", "Green", "Hall", "Harris", "Hart",
    "Hill", "Howard", "Jackson", "James", "King", "Knight", "Lane", "Lewis",
    "Miller", "Morgan", "Murphy", "Nelson", "Parker", "Price", "Reed", "Ross",
    "Scott", "Shaw", "Smith", "Stone", "Taylor", "Turner", "Walker", "Ward",
    "West", "White", "Wilson", "Wood", "Young", "Volkov", "Sokolov", "Orlov",
    "Morozov", "Smirnov", "Ivanov", "Petrov", "Novikov", "Pavlov", "Romanov",
]

LAST_NAMES.extend([
    "Kovalev", "Kovalenko", "Popov", "Vasiliev", "Nikolaev", "Alekseev",
    "Stepanov", "Kozlov", "Kiselev", "Medvedev", "Kravchenko", "Bondarenko",
    "Volkova", "Sokolova", "Orlova", "Morozova", "Smirnova", "Ivanova",
    "Petrova", "Novikova", "Pavlova", "Romanova", "Makarova", "Egorova",
    "Anderson", "Thompson", "Roberts", "Edwards", "Hughes", "Phillips",
    "Campbell", "Mitchell", "Morris", "Rogers", "Peterson", "Sanders",
])

NICK_WORDS = [
    "nova", "pixel", "shift", "byte", "echo", "orbit", "wave", "metro",
    "neon", "vertex", "signal", "north", "river", "stone", "cloud", "drift",
    "prime", "atlas", "field", "lunar", "solar", "urban", "mint", "clear",
    "rapid", "smart", "daily", "native", "fresh", "solid", "bright", "quiet",
]

BIO_PARTS = [
    "coffee and late walks",
    "work, music, weekends",
    "quiet days, good playlists",
    "learning as I go",
    "small plans, long roads",
    "city lights and simple things",
    "books, calls, and good weather",
    "mostly offline, sometimes here",
    "building a better routine",
    "new places, old songs",
    "photos, notes, daily life",
    "slow mornings, busy evenings",
    "keeping it simple",
    "travel plans and clean inboxes",
    "music, work, and a little chaos",
    "just here for updates",
]


def write_profile_plan(
    accounts: list[AccountRecord],
    avatar_root: Path,
    output_path: Path,
    seed: int | None = None,
    used_avatars: set[str] | None = None,
) -> list[dict[str, Any]]:
    plan = generate_profile_plan(accounts, avatar_root, seed=seed, used_avatars=used_avatars)
    write_private_json(output_path, {"profiles": plan})
    return plan


def generate_profile_plan(
    accounts: list[AccountRecord],
    avatar_root: Path,
    seed: int | None = None,
    used_avatars: set[str] | None = None,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    male_avatars = _load_avatars(avatar_root / "male")
    female_avatars = _load_avatars(avatar_root / "female")
    neutral_avatars = _load_avatars(avatar_root / "unknown")
    used = set(used_avatars or set())
    used_usernames: set[str] = set()
    plan: list[dict[str, Any]] = []

    for account in accounts:
        saved_gender = str(account.metadata.get("profile_gender", "")).lower()
        gender = saved_gender if saved_gender in {"m", "f"} else rng.choice(["m", "f"])
        first_names = MALE_FIRST_NAMES if gender == "m" else FEMALE_FIRST_NAMES
        avatars = male_avatars if gender == "m" else female_avatars
        if not avatars:
            avatars = neutral_avatars or male_avatars or female_avatars
        available_avatars = [avatar for avatar in avatars if _avatar_key(avatar) not in used]
        if available_avatars:
            avatars = available_avatars

        first_name = rng.choice(first_names)
        last_name = rng.choice(LAST_NAMES)
        username = _make_unique_username(first_name, last_name, rng, used_usernames)
        bio = _make_bio(rng)
        avatar_path = rng.choice(avatars) if avatars else None
        if avatar_path:
            used.add(_avatar_key(avatar_path))
        avatar = str(avatar_path) if avatar_path else None

        plan.append(
            {
                "account_id": account.id,
                "gender": gender,
                "first_name": first_name,
                "last_name": last_name,
                "username": username,
                "bio": bio,
                "avatar": avatar,
            }
        )
    return plan


def export_plan_text_files(plan: list[dict[str, Any]], templates_dir: Path) -> None:
    templates_dir.mkdir(parents=True, exist_ok=True)
    mapping = {
        "profile_genders.txt": "gender",
        "first_names.txt": "first_name",
        "last_names.txt": "last_name",
        "usernames.txt": "username",
        "bios.txt": "bio",
    }
    for filename, key in mapping.items():
        lines = [str(item.get(key) or "") for item in plan]
        (templates_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_avatars(folder: Path) -> list[Path]:
    if not folder.exists():
        return []
    return [
        path
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]


def _avatar_key(avatar: Path) -> str:
    try:
        return str(avatar.resolve())
    except OSError:
        return str(avatar)


def _make_unique_username(
    first_name: str,
    last_name: str,
    rng: random.Random,
    used: set[str],
) -> str:
    for _ in range(100):
        candidate = _username_candidate(first_name, last_name, rng)
        if candidate not in used:
            used.add(candidate)
            return candidate
    fallback = f"{_clean(first_name)}{rng.randrange(100000, 999999)}"
    used.add(fallback)
    return fallback


def generate_username_candidates(
    first_name: str | None,
    last_name: str | None,
    count: int = 12,
    seed: int | None = None,
) -> list[str]:
    rng = random.Random(seed)
    first = first_name or rng.choice(MALE_FIRST_NAMES + FEMALE_FIRST_NAMES)
    last = last_name or rng.choice(LAST_NAMES)
    used: set[str] = set()
    return [_make_unique_username(first, last, rng, used) for _ in range(max(1, count))]


def _username_candidate(first_name: str, last_name: str, rng: random.Random) -> str:
    first = _clean(first_name)
    last = _clean(last_name)
    word = rng.choice(NICK_WORDS)
    year = str(rng.randrange(1988, 2006))
    short_year = year[-2:]
    number = str(rng.randrange(10, 999))
    patterns = [
        f"{first}{last}",
        f"{first}{last}{short_year}",
        f"{first}_{last}",
        f"{first}_{last}{number}",
        f"{first}{last[:1]}{year}",
        f"{first}{last[:3]}{number}",
        f"{first}_{word}",
        f"{first}{rng.choice(['go', 'life', 'notes', 'here'])}{number}",
        f"{word}_{first}{short_year}",
    ]
    return _fit_username(rng.choice(patterns))


def _clean(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", value.lower())


def _fit_username(value: str) -> str:
    value = value.strip("._")
    value = re.sub(r"[^a-z0-9_]", "_", value.lower())
    value = re.sub(r"_+", "_", value)
    if len(value) < 5:
        value = f"{value}_id"
    value = value[:32].strip("_")
    if not value or not value[0].isalpha():
        value = f"u_{value}"
    return value[:32].strip("_")


def _make_bio(rng: random.Random) -> str:
    bio = rng.choice(BIO_PARTS)
    if rng.random() < 0.25:
        bio = f"{bio} | {rng.choice(['no rush', 'be kind', 'daily notes', 'slow mode'])}"
    return bio[:70]


def split_unknown_avatars(
    avatar_root: Path,
    male_ratio: float = 0.5,
    move: bool = True,
    seed: int | None = None,
) -> dict[str, int]:
    rng = random.Random(seed)
    unknown_dir = avatar_root / "unknown"
    male_dir = avatar_root / "male"
    female_dir = avatar_root / "female"
    male_dir.mkdir(parents=True, exist_ok=True)
    female_dir.mkdir(parents=True, exist_ok=True)
    avatars = _load_avatars(unknown_dir)
    counters = {"male": 0, "female": 0, "skipped": 0}

    for source in avatars:
        target_dir = male_dir if rng.random() < male_ratio else female_dir
        target = _unique_path(target_dir / source.name)
        if move:
            shutil.move(str(source), str(target))
        else:
            shutil.copy2(source, target)
        counters["male" if target_dir == male_dir else "female"] += 1
    return counters


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot find free filename for {path}")
