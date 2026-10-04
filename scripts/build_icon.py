"""Encode the generated PNG as a multi-resolution Windows ICO asset."""
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SIZES = [(side, side) for side in (16, 24, 32, 48, 64, 128, 256)]


def main() -> None:
    source = ROOT / "assets/branding/uznik-multitool.png"
    destination = source.with_suffix(".ico")
    with Image.open(source) as icon:
        icon.save(destination, format="ICO", sizes=SIZES)
    print(f"Windows icon: {destination}")


if __name__ == "__main__":
    main()
