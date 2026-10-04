"""Initialize the same empty working layout used by the desktop."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    from core.config import AppConfig
    os.chdir(ROOT)
    config = AppConfig.load(ROOT / ".env")
    print(f"Session reauthorization queue: {config.import_dir / 'auth_input'}")
    print(f"Ordinary import folder: {config.import_dir}")
    print(f"Runtime data: {config.data_dir}")


if __name__ == "__main__":
    main()
