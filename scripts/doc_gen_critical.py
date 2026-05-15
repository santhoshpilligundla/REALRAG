"""De-risk script: doc-gen for the critical_entry_points of one repo.

Validates the schema + prompt before spending real money on the full pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import apply_schema  # noqa: E402
from lib.doc_gen import generate_entity_doc, list_critical_entry_point_entities  # noqa: E402
from lib.repos_repo import list_repos  # noqa: E402
from lib.runs_repo import finish_run, start_run  # noqa: E402


def main(repo_name: str = "po") -> int:
    apply_schema()
    repos = list_repos()
    target = next((r for r in repos if r.display_name == repo_name), None)
    if target is None:
        print(f"Repo '{repo_name}' not found.")
        return 1

    if not target.critical_entry_points:
        print(f"Repo '{repo_name}' has no critical_entry_points configured.")
        return 1

    entity_ids = list_critical_entry_point_entities(target.repo_id, target.critical_entry_points)
    if not entity_ids:
        print(f"No entities matched critical_entry_points: {target.critical_entry_points}")
        print("Has this repo been parsed? (status should be 'ready')")
        return 1

    print(f"Doc-genning {len(entity_ids)} critical-entry-point entities in {repo_name}…")
    run_id = start_run(target.repo_id, "doc_gen_critical", notes=f"{len(entity_ids)} entities")

    total_in = 0
    total_out = 0
    fail = 0
    for i, eid in enumerate(entity_ids, 1):
        result = generate_entity_doc(eid)
        if result.success:
            total_in += result.prompt_tokens
            total_out += result.completion_tokens
            print(f"  [{i}/{len(entity_ids)}] OK  doc={result.doc_id}  tokens(in/out)={result.prompt_tokens}/{result.completion_tokens}")
        else:
            fail += 1
            print(f"  [{i}/{len(entity_ids)}] FAIL  {result.error}")

    counts = {
        "entities": len(entity_ids),
        "succeeded": len(entity_ids) - fail,
        "failed": fail,
        "prompt_tokens": total_in,
        "completion_tokens": total_out,
    }
    finish_run(run_id, "success" if fail == 0 else "error", counts=counts)
    print()
    print(f"Done. tokens in/out = {total_in}/{total_out}")
    return 1 if fail else 0


if __name__ == "__main__":
    repo = sys.argv[1] if len(sys.argv) > 1 else "po"
    sys.exit(main(repo))
