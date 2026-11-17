from __future__ import annotations

import random


MALE_FIRST_NAMES = [
    "Alex", "Max", "Daniel", "Chris", "Sam", "Nick", "Tony", "Leo", "Mark", "David",
    "Ivan", "Nikita", "Mikhail", "Artem", "Denis", "Roman", "Kirill", "Pavel", "Oleg", "Vlad",
    "Tim", "Victor", "George", "Andrew", "Ethan", "Noah", "Liam", "Mason", "Logan", "Ryan",
    "Adam", "Oscar", "Felix", "Lucas", "Oliver", "Henry", "Jack", "Arthur", "Simon", "Martin",
]

FEMALE_FIRST_NAMES = [
    "Mira", "Anna", "Kate", "Lisa", "Nina", "Sofia", "Maria", "Diana", "Eva", "Lina",
    "Mila", "Alina", "Polina", "Vera", "Kira", "Tanya", "Julia", "Elena", "Irina", "Olga",
    "Emma", "Ava", "Mia", "Nora", "Lily", "Grace", "Chloe", "Zoe", "Ruby", "Ivy",
    "Alice", "Maya", "Sara", "Laura", "Clara", "Sasha", "Rita", "Karina", "Daria", "Anya",
]

FIRST_NAMES = MALE_FIRST_NAMES + FEMALE_FIRST_NAMES

NEUTRAL_LAST_NAMES = [
    "Stone", "Black", "White", "Green", "Brown", "Gray", "Fox", "Reed", "Hill", "Wood",
    "King", "Bell", "Cole", "West", "Ford", "Lane", "Cross", "Page", "Hart", "Wells",
    "Miller", "Taylor", "Walker", "Young", "Allen", "Scott", "Baker", "Carter", "Turner", "Parker",
    "Cooper", "Morgan", "Bailey", "Howard", "Ward", "Brooks", "Price", "Bennett", "Cook", "Ross",
    "Frost", "Blake", "Shaw", "Miles", "Hunter", "Palmer", "Grant", "Knight", "Webb", "Sharp",
    "North", "Stonefield", "Rivers", "Lake", "Winter", "Summer", "Spring", "Autumn", "Silver", "Bright",
]

MALE_SLAVIC_LAST_NAMES = [
    "Volkov", "Sokolov", "Orlov", "Morozov", "Smirnov", "Kuznetsov", "Ivanov", "Petrov", "Sidorov", "Fedorov",
    "Novikov", "Pavlov", "Romanov", "Makarov", "Egorov", "Nikitin", "Belov", "Kirov", "Zaitsev", "Lebedev",
]

FEMALE_SLAVIC_LAST_NAMES = [f"{name}a" for name in MALE_SLAVIC_LAST_NAMES]

LAST_NAMES = NEUTRAL_LAST_NAMES + MALE_SLAVIC_LAST_NAMES + FEMALE_SLAVIC_LAST_NAMES


def generate_name_lists(count: int, seed: int | None = None) -> tuple[list[str], list[str]]:
    """Generate gender-consistent first/last name pairs."""
    rng = random.Random(seed)
    total = max(1, count)
    male_last = NEUTRAL_LAST_NAMES + MALE_SLAVIC_LAST_NAMES
    female_last = NEUTRAL_LAST_NAMES + FEMALE_SLAVIC_LAST_NAMES
    first_names: list[str] = []
    last_names: list[str] = []
    for _ in range(total):
        if rng.random() < 0.5:
            first_names.append(rng.choice(MALE_FIRST_NAMES))
            last_names.append(rng.choice(male_last))
        else:
            first_names.append(rng.choice(FEMALE_FIRST_NAMES))
            last_names.append(rng.choice(female_last))
    return first_names, last_names
