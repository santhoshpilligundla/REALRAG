"""Clone every registered repo whose status is 'registered' or 'error'."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.clone import clone_repo  # noqa: E402
from lib.db import apply_schema  # noqa: E402
from lib.repos_repo import list_products, list_repos  # noqa: E402


def main() -> int:
    apply_schema()
    products_by_id = {p.product_id: p.name for p in list_products()}
    repos = list_repos()
    pending = [r for r in repos if r.status in ("registered", "error", "cloning")]

    if not pending:
        print("No repos pending clone.")
        return 0

    print(f"Cloning {len(pending)} repos...")
    fail = 0
    for r in pending:
        product = products_by_id.get(r.product_id, "?")
        print(f"  → {r.display_name} ({product}) ... ", end="", flush=True)
        result = clone_repo(r, product)
        if result.success:
            print(f"OK  sha={result.sha[:8] if result.sha else '?'}  {result.message}")
        else:
            fail += 1
            print(f"FAIL  {result.message[:200]}")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
