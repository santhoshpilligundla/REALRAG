"""Walk + parse + chunk every repo whose status is 'cloned' (or 'error' / 'ready' for re-parse)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.chunker import chunk_repo  # noqa: E402
from lib.db import apply_schema  # noqa: E402
from lib.repos_repo import list_repos  # noqa: E402


def main() -> int:
    apply_schema()
    repos = list_repos()
    pending = [r for r in repos if r.status in ("cloned", "ready", "error")]

    if not pending:
        print("Nothing to parse (no repos in cloned/ready/error status).")
        return 0

    print(f"Parsing {len(pending)} repos…")
    fail = 0
    for r in pending:
        t0 = time.time()
        print(f"  → {r.display_name} … ", end="", flush=True)

        def progress(msg: str) -> None:
            print(f"\n     {msg}", end="", flush=True)

        result = chunk_repo(r, on_progress=progress)
        elapsed = time.time() - t0
        if result.success:
            print(
                f"\n  OK  files={result.files_seen} parsed={result.files_parsed} "
                f"entities={result.entities} chunks={result.chunks} "
                f"facts={result.facts}  ({elapsed:.1f}s)"
            )
        else:
            fail += 1
            print(f"\n  FAIL  {result.error}  ({elapsed:.1f}s)")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
