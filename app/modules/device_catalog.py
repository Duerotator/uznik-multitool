"""Device labels and compatible OS families for new MTProto authorizations.

These are connection descriptors, not hardware emulation. Keep systems explicit:
an iPad uses iPadOS, a Mac uses macOS, and a desktop never uses a mobile OS.
The catalogue intentionally uses established releases, not a moving 'latest'.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceTemplate:
    model: str
    device_type: str
    platform: str
    systems: tuple[str, ...]


def _family(
    brand: str, models: tuple[str, ...], device_type: str,
    platform: str, systems: tuple[str, ...],
) -> tuple[DeviceTemplate, ...]:
    return tuple(
        DeviceTemplate(f"{brand} {model}".strip(), device_type, platform, systems)
        for model in models
    )


PLATFORM_LANG_PACKS = {
    "android": "android", "ios": "ios", "macos": "macos",
    "windows": "tdesktop", "linux": "tdesktop",
}

DEVICE_TYPES = ("phone", "tablet", "laptop", "desktop")

# Supported upgrades as well as factory OS versions are allowed. Do not add
# model codes guessed from names: those codes are region-specific and unused.
DEVICES = (
    *_family("Samsung Galaxy", (
        "S21", "S21+", "S21 Ultra", "S21 FE", "S22", "S22+", "S22 Ultra",
        "S23", "S23+", "S23 Ultra", "S23 FE", "A14", "A24", "A34", "A54",
        "A53", "A33", "A73", "A23", "M14", "M23", "M33", "M34", "M54",
        "F14", "F23", "Z Fold4", "Z Flip4", "Z Fold5", "Z Flip5",
    ), "phone", "android", ("Android 14",)),
    *_family("Samsung Galaxy", (
        "S24", "S24+", "S24 Ultra", "A15", "A25", "A35", "A55",
        "M15", "M35", "M55", "Z Fold6", "Z Flip6",
    ), "phone", "android", ("Android 14",)),
    *_family("Google Pixel", (
        "6", "6 Pro", "6a", "7", "7 Pro", "7a", "8", "8 Pro", "8a",
        "Fold", "9", "9 Pro", "9 Pro XL", "9 Pro Fold",
    ), "phone", "android", ("Android 14",)),
    *_family("iPhone", (
        "XR", "XS", "XS Max", "11", "11 Pro", "11 Pro Max", "12 mini",
        "12", "12 Pro", "12 Pro Max", "13 mini", "13", "13 Pro", "13 Pro Max",
        "14", "14 Plus", "14 Pro", "14 Pro Max", "15", "15 Plus", "15 Pro",
        "15 Pro Max", "SE (2nd generation)", "SE (3rd generation)",
    ), "phone", "ios", ("iOS 17", "iOS 18")),
    *_family("iPhone", (
        "16", "16 Plus", "16 Pro", "16 Pro Max", "16e",
    ), "phone", "ios", ("iOS 18",)),
    *_family("OnePlus", (
        "9", "9 Pro", "9R", "9RT", "10 Pro", "10T", "10R", "11", "11R",
        "12", "12R", "Open", "Nord 2T", "Nord 3", "Nord CE 3", "Nord CE 3 Lite",
    ), "phone", "android", ("Android 14",)),
    *_family("Xiaomi", (
        "12", "12 Pro", "12T", "12T Pro", "13", "13 Pro", "13 Ultra",
        "13 Lite", "13T", "13T Pro", "Redmi Note 12", "Redmi Note 12 Pro",
        "Redmi Note 12 Pro+", "Redmi Note 13", "Redmi Note 13 Pro",
        "Redmi Note 13 Pro+", "Poco F5", "Poco F5 Pro", "Poco X6",
    ), "phone", "android", ("Android 13", "Android 14")),
    *_family("Xiaomi", (
        "14", "14 Ultra", "Poco X6 Pro",
    ), "phone", "android", ("Android 14",)),
    *_family("Oppo", ("Find X7", "Find X7 Ultra"), "phone", "android", ("Android 14",)),
    *_family("Vivo", ("X100", "X100 Pro"), "phone", "android", ("Android 14",)),
    *_family("Realme", ("GT 5",), "phone", "android", ("Android 13",)),
    *_family("Samsung Galaxy", (
        "Tab S6 Lite (2022)", "Tab S6 Lite (2024)", "Tab S7 FE", "Tab S8",
        "Tab S8+", "Tab S8 Ultra", "Tab S9", "Tab S9+", "Tab S9 Ultra",
        "Tab S9 FE", "Tab S9 FE+", "Tab A8", "Tab A9", "Tab A9+",
    ), "tablet", "android", ("Android 14",)),
    *_family("Google Pixel", ("Tablet",), "tablet", "android", ("Android 14",)),
    *_family("OnePlus", ("Pad",), "tablet", "android", ("Android 13", "Android 14")),
    *_family("OnePlus", ("Pad 2",), "tablet", "android", ("Android 14",)),
    *_family("Xiaomi", ("Pad 6", "Redmi Pad", "Redmi Pad SE"),
             "tablet", "android", ("Android 13",)),
    *_family("Xiaomi", ("Pad 6S Pro", "Poco Pad"), "tablet", "android", ("Android 14",)),
    *_family("iPad", (
        "(7th generation)", "(8th generation)", "(9th generation)",
        "(10th generation)", "mini (5th generation)", "mini (6th generation)",
        "Air (3rd generation)", "Air (4th generation)", "Air (5th generation)",
        "Pro 11-inch (1st generation)", "Pro 11-inch (2nd generation)",
        "Pro 11-inch (3rd generation)", "Pro 11-inch (4th generation)",
        "Pro 12.9-inch (3rd generation)", "Pro 12.9-inch (4th generation)",
        "Pro 12.9-inch (5th generation)", "Pro 12.9-inch (6th generation)",
    ), "tablet", "ios", ("iPadOS 17", "iPadOS 18")),
    *_family("iPad", (
        "Air 11-inch (M2)", "Air 13-inch (M2)", "Pro 11-inch (M4)", "Pro 13-inch (M4)",
    ), "tablet", "ios", ("iPadOS 18",)),
    *_family("MacBook", (
        "Air (2018)", "Air (2019)", "Air (2020, Intel)", "Air (M1)",
        "Air 13-inch (M2)", "Air 15-inch (M2)", "Air 13-inch (M3)",
        "Air 15-inch (M3)", "Pro 13-inch (2018)", "Pro 13-inch (2019)",
        "Pro 13-inch (2020, Intel)", "Pro 16-inch (2019)", "Pro 13-inch (M1)",
        "Pro 13-inch (M2)", "Pro 14-inch (M1 Pro)", "Pro 16-inch (M1 Max)",
        "Pro 14-inch (M2 Pro)", "Pro 16-inch (M2 Max)", "Pro 14-inch (M3)",
        "Pro 14-inch (M3 Pro)", "Pro 16-inch (M3 Max)",
    ), "laptop", "macos", ("macOS 14.6",)),
    *_family("", (
        "Mac mini (2018)", "Mac mini (M1)", "Mac mini (M2)", "Mac mini (M2 Pro)",
        "Mac Studio (M1 Max)", "Mac Studio (M1 Ultra)", "Mac Studio (M2 Max)",
        "Mac Studio (M2 Ultra)", "iMac 21.5-inch (2019)", "iMac 27-inch (2019)",
        "iMac 27-inch (2020)", "iMac 24-inch (M1)", "iMac 24-inch (M3)",
        "Mac Pro (2019)", "Mac Pro (2023)",
    ), "desktop", "macos", ("macOS 14.6",)),
    *_family("Dell", (
        "XPS 13 9310", "XPS 13 9320", "XPS 15 9520", "XPS 17 9720",
        "Inspiron 15 3520", "Latitude 7420",
    ), "laptop", "windows", ("Windows 11",)),
    *_family("Lenovo", (
        "ThinkPad X1 Carbon Gen 9", "ThinkPad X1 Carbon Gen 10", "ThinkPad T14 Gen 2",
        "ThinkPad T14 Gen 3", "Yoga Slim 7", "IdeaPad 5",
    ), "laptop", "windows", ("Windows 11",)),
    *_family("HP", ("Envy 13", "Spectre x360", "EliteBook 840 G8"),
             "laptop", "windows", ("Windows 11",)),
    *_family("ASUS", ("Zenbook 14 UX3402", "Vivobook 15 X1502"),
             "laptop", "windows", ("Windows 11",)),
    *_family("Acer", ("Swift 3 SF314",), "laptop", "windows", ("Windows 11",)),
    *_family("Microsoft", ("Surface Laptop 4", "Surface Laptop 5"),
             "laptop", "windows", ("Windows 11",)),
    *_family("Microsoft", ("Surface Pro 8", "Surface Pro 9", "Surface Go 3", "Surface Go 4"),
             "tablet", "windows", ("Windows 11",)),
    *_family("Lenovo", ("ThinkPad X12 Detachable",), "tablet", "windows", ("Windows 11",)),
    *_family("", (
        "Dell XPS 13 9310 (Linux)", "Lenovo ThinkPad T14 Gen 3 (Linux)",
        "Framework Laptop 13 (Linux)", "System76 Lemur Pro", "System76 Galago Pro",
    ), "laptop", "linux", ("Ubuntu 22.04 LTS", "Ubuntu 24.04 LTS")),
    *_family("", (
        "Dell OptiPlex 7090", "Dell OptiPlex 7000", "HP EliteDesk 800 G6",
        "Lenovo ThinkCentre M70q", "Lenovo ThinkCentre M90q", "Intel NUC 11", "Intel NUC 12",
    ), "desktop", "windows", ("Windows 11",)),
    *_family("", (
        "Dell Precision 3650 (Linux)", "Lenovo ThinkStation P350 (Linux)", "System76 Thelio",
    ), "desktop", "linux", ("Ubuntu 22.04 LTS", "Ubuntu 24.04 LTS")),
)
