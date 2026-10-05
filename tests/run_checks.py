from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline desktop checks")
    parser.add_argument("command", choices=("fast", "unit"))
    parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
        cwd=root,
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
