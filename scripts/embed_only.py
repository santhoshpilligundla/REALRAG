"""Embed-only pass: build per-repo FAISS for all repos. Doc-gen is already complete.

Idempotent: embed_repo skips chunks/docs already marked embedded.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.embed_pipeline import count_pending_embeds, embed_repo  # noqa: E402
from lib.repos_repo import list_repos  # noqa: E402


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    log("=== RealRAG embed-only pass ===")
    t0 = time.time()
    for repo in list_repos():
        pending = count_pending_embeds(repo.repo_id)
        total = pending["code_chunks"] + pending["generated_docs"]
        if total == 0:
            log(f"  {repo.display_name}: 0 pending — skip")
            continue
        log(f"  {repo.display_name}: code={pending['code_chunks']} docs={pending['generated_docs']} — starting")
        r = embed_repo(repo.repo_id, on_progress=lambda m: log(f"    [{repo.display_name}] {m}"))
        status = "OK" if r.success else f"ERROR: {r.error}"
        log(f"  {repo.display_name}: {status} — code={r.code_chunks_embedded} docs={r.generated_docs_embedded}")
    log(f"=== embed-only complete in {(time.time() - t0) / 60:.1f} min ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
