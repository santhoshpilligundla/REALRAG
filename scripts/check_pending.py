"""Report exactly what pipeline work remains, per stage, using lib counters."""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.doc_gen import count_pending_docs
from lib.doc_gen_l4_enrich import count_pending_l4_enrich
from lib.doc_gen_pass2 import count_pending_pass2
from lib.doc_gen_pass3 import count_pending_pass3
from lib.embed_pipeline import count_pending_embeds
from lib.repos_repo import list_repos


def main() -> None:
    repos = list_repos()

    print("=== Pass 1 (entity) pending ===")
    t = 0
    for r in repos:
        p = count_pending_docs(r.repo_id, "meaningful", r.critical_entry_points)
        t += p
        print(f"  {r.display_name:<12} {p}")
    print(f"  TOTAL pending: {t}")

    print("\n=== Pass 2 (module) pending ===")
    t = 0
    for r in repos:
        p = count_pending_pass2(r.repo_id)
        t += p
        print(f"  {r.display_name:<12} {p}")
    print(f"  TOTAL pending: {t}")

    print("\n=== L4 enrich (PO) pending ===")
    po = next((r for r in repos if r.display_name == "po"), None)
    if po:
        print(f"  po           {count_pending_l4_enrich(po.repo_id)}")

    print("\n=== Pass 3 (narrative) pending workflows ===")
    print(f"  {count_pending_pass3()}")

    print("\n=== Embed pending (code_chunks + generated_docs) ===")
    tc = td = 0
    for r in repos:
        p = count_pending_embeds(r.repo_id)
        tc += p["code_chunks"]
        td += p["generated_docs"]
        print(f"  {r.display_name:<12} code={p['code_chunks']:>6}  docs={p['generated_docs']:>6}")
    print(f"  TOTAL pending: code={tc:,}  docs={td:,}")


if __name__ == "__main__":
    main()
