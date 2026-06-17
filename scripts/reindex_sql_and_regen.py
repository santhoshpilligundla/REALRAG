"""Fill the SQL Phase-1 gap, then regenerate downstream (Pass-1 doc-gen + embed)
for all newly-indexed entities (SQL functions + the previously-misrouted XML
services). Targeted to affected repos; existing correct docs are untouched.
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn
from lib.parser_sql import parse_sql_file
from lib.parser_ddl import looks_like_ddl
from lib.chunker import _persist_entities_recursive, _persist_chunks_and_facts
from lib.doc_gen import count_pending_docs, doc_gen_repo
from lib.embed_pipeline import embed_repo
from lib.repos_repo import list_repos


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    # 1. Re-index non-DDL SQL files that have no entities.
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT repo_id, clone_path FROM repos")
        clone = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("""
            SELECT rf.file_id, rf.repo_id, rf.path FROM repo_files rf
             WHERE rf.language = 'sql'
               AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.file_id = rf.file_id)
        """)
        sqlfiles = cur.fetchall()

    affected = []
    for fid, rid, path in sqlfiles:
        ap = Path(clone.get(rid, "")) / path
        if not ap.exists() or looks_like_ddl(ap):
            continue
        ents = parse_sql_file(ap)
        if ents:
            affected.append((fid, rid, path, ents))

    log(f"SQL gap files to index: {len(affected)}")
    repos_to = set()
    with get_conn() as c, c.cursor() as cur:
        for fid, rid, path, ents in affected:
            _persist_entities_recursive(cur, rid, fid, ents)
            _persist_chunks_and_facts(cur, rid, fid, ents, "sql")
            repos_to.add(rid)
        c.commit()
    log(f"SQL entities persisted; affected repos: {len(repos_to)}")

    # 2. Doc-gen (Pass 1) for all pending entities in affected repos, then embed.
    for repo in list_repos():
        if repo.repo_id not in repos_to:
            continue
        pend = count_pending_docs(repo.repo_id, "meaningful", repo.critical_entry_points)
        log(f"{repo.display_name}: doc-gen pending = {pend}")
        if pend:
            r = doc_gen_repo(repo.repo_id, "meaningful", repo.critical_entry_points,
                             on_progress=lambda m: log(f"  [docgen] {m}"))
            log(f"{repo.display_name}: doc-gen ok={r.succeeded} fail={r.failed} "
                f"tokens(in/out)={r.prompt_tokens}/{r.completion_tokens}")
        log(f"{repo.display_name}: embedding…")
        er = embed_repo(repo.repo_id, on_progress=lambda m: log(f"  [embed] {m}"))
        log(f"{repo.display_name}: embedded code={er.code_chunks_embedded} docs={er.generated_docs_embedded}")

    log("DONE")


if __name__ == "__main__":
    main()
