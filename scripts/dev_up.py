"""Bring up pgserver and apply schema. Idempotent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import apply_schema, get_dsn  # noqa: E402


def main() -> None:
    dsn = get_dsn()
    print(f"pgserver listening at: {dsn}")
    apply_schema()
    print("schema applied.")


if __name__ == "__main__":
    main()
