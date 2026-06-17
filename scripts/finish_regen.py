"""Finish the downstream regen: Pass-1 doc-gen for all pending po entities
(the newly-indexed XML services + SQL functions), then re-embed po. Idempotent
and resumable — doc-gen skips entities that already have docs."""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.repos_repo import list_repos
from lib.doc_gen import count_pending_docs, doc_gen_repo
from lib.embed_pipeline import count_pending_embeds, embed_repo


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    po = next(r for r in list_repos() if r.display_name == "po")
    pend = count_pending_docs(po.repo_id, "meaningful", po.critical_entry_points)
    log(f"po doc-gen pending = {pend}")
    if pend:
        r = doc_gen_repo(po.repo_id, "meaningful", po.critical_entry_points,
                         on_progress=lambda m: log(f"  [docgen] {m}"))
        log(f"doc-gen done ok={r.succeeded} fail={r.failed} "
            f"tokens(in/out)={r.prompt_tokens}/{r.completion_tokens}")
    pe = count_pending_embeds(po.repo_id)
    log(f"embedding po (pending code={pe['code_chunks']} docs={pe['generated_docs']})…")
    er = embed_repo(po.repo_id, on_progress=lambda m: log(f"  [embed] {m}"))
    log(f"embed done code={er.code_chunks_embedded} docs={er.generated_docs_embedded}")
    log("DONE")


if __name__ == "__main__":
    main()
