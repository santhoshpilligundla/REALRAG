"""Run the full RealRAG operational pipeline in order.

Order:
  1. Pass 1 — entity-level doc-gen
  2. Cross-repo edges (deterministic; needed before Pass 3 walks chains)
  3. Pass 2 — module narratives (Sonnet)
  4. L4 enrich — PO only (multiple examples + statement annotations + git history)
  5. Pass 3 — cross-module narratives (Opus, uses edges + Pass 1/2 docs)
  6. Embed — LAST. Embeds code_chunks + every generated_doc in one shot
     so Pass 2/3/L4 outputs all land in FAISS (no re-embed needed).

Designed to be safely re-runnable: every stage skips already-completed work
by querying the DB for pending items.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.cross_repo import discover_edges  # noqa: E402
from lib.doc_gen import count_pending_docs, doc_gen_repo  # noqa: E402
from lib.doc_gen_l4_enrich import enrich_l4_repo  # noqa: E402
from lib.doc_gen_pass2 import count_pending_pass2, doc_gen_pass2_repo  # noqa: E402
from lib.doc_gen_pass3 import count_pending_pass3, doc_gen_pass3  # noqa: E402
from lib.embed_pipeline import count_pending_embeds, embed_repo  # noqa: E402
from lib.repos_repo import list_repos  # noqa: E402


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def step1_pass1() -> None:
    log("STEP 1 — Pass 1 doc-gen (drain remaining pending entities)")
    for repo in list_repos():
        pending = count_pending_docs(repo.repo_id, "meaningful", repo.critical_entry_points)
        if pending == 0:
            log(f"  {repo.display_name}: 0 pending — skip")
            continue
        log(f"  {repo.display_name}: {pending} pending — starting")
        result = doc_gen_repo(
            repo.repo_id, "meaningful", repo.critical_entry_points,
            on_progress=lambda m: log(f"    [{repo.display_name}] {m}"),
        )
        log(
            f"  {repo.display_name}: done — "
            f"ok={result.succeeded} fail={result.failed} "
            f"tokens(in/out)={result.prompt_tokens}/{result.completion_tokens}"
        )


def step3_edges() -> None:
    log("STEP 3 — Build cross-repo edges")
    r = discover_edges(on_progress=log)
    log(f"  done — patterns={r.patterns_run} edges={r.edges_created}")


def step4_embed() -> None:
    log("STEP 4 — Embed all repos")
    for repo in list_repos():
        pending = count_pending_embeds(repo.repo_id)
        total = pending["code_chunks"] + pending["generated_docs"]
        if total == 0:
            log(f"  {repo.display_name}: 0 pending — skip")
            continue
        log(f"  {repo.display_name}: code={pending['code_chunks']} docs={pending['generated_docs']}")
        r = embed_repo(repo.repo_id, on_progress=lambda m: log(f"    [{repo.display_name}] {m}"))
        log(
            f"  {repo.display_name}: done — "
            f"code_embedded={r.code_chunks_embedded} docs_embedded={r.generated_docs_embedded}"
        )


def step5a_pass2() -> None:
    log("STEP 5a — Pass 2 module narratives")
    for repo in list_repos():
        pending = count_pending_pass2(repo.repo_id)
        if pending == 0:
            log(f"  {repo.display_name}: 0 pending — skip")
            continue
        log(f"  {repo.display_name}: {pending} pending")
        r = doc_gen_pass2_repo(repo.repo_id, on_progress=lambda m: log(f"    [{repo.display_name}] {m}"))
        log(
            f"  {repo.display_name}: done — "
            f"ok={r.succeeded} fail={r.failed} "
            f"tokens(in/out)={r.prompt_tokens}/{r.completion_tokens}"
        )


def step5b_l4_enrich() -> None:
    log("STEP 5b — L4 enrichment (PO only)")
    po = next((r for r in list_repos() if r.display_name == "po"), None)
    if not po:
        log("  no po repo found — skip")
        return
    r = enrich_l4_repo(po.repo_id, on_progress=lambda m: log(f"    [po] {m}"))
    log(
        f"  po: done — ok={r.succeeded} fail={r.failed} "
        f"examples_added={r.examples_added} annotated={r.annotated} "
        f"tokens(in/out)={r.prompt_tokens}/{r.completion_tokens}"
    )


def step5c_pass3() -> None:
    log("STEP 5c — Pass 3 cross-module narratives")
    pending = count_pending_pass3()
    if pending == 0:
        log("  0 pending workflows — skip (need cross-repo edges first?)")
        return
    log(f"  {pending} workflows pending")
    r = doc_gen_pass3(on_progress=log)
    log(f"  done — ok={r.succeeded} fail={r.failed} tokens(in/out)={r.prompt_tokens}/{r.completion_tokens}")


def main() -> int:
    log("=== RealRAG pipeline chain — full doc-gen then embed ===")
    t0 = time.time()

    step1_pass1()      # Pass 1 entity docs
    step3_edges()      # cross-repo edges (no LLM)
    step5a_pass2()     # Pass 2 module narratives
    step5b_l4_enrich() # L4 enrichment on PO
    step5c_pass3()     # Pass 3 cross-module narratives
    step4_embed()      # Embed everything LAST — all Pass 1/2/3 + L4 docs land in FAISS

    elapsed = (time.time() - t0) / 60
    log(f"=== pipeline chain complete in {elapsed:.1f} min ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
