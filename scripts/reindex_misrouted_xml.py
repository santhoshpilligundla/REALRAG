"""Targeted re-index of XML files the dispatch bug left unindexed (0 entities).

Parses each with the FIXED parser, persists entities + code_chunks + facts, then
re-embeds the affected repos (cached chunks are free; only new ones hit the API)
and rebuilds their FAISS indexes.
"""
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn
from lib.parser_xml import parse_xml_file
from lib.chunker import _persist_entities_recursive, _persist_chunks_and_facts
from lib.embed_pipeline import embed_repo
from lib.repos_repo import list_repos


def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT repo_id, clone_path FROM repos")
        clone = {r[0]: r[1] for r in cur.fetchall()}
        cur.execute("""
            SELECT rf.file_id, rf.repo_id, rf.path, rf.language
              FROM repo_files rf
             WHERE rf.language = 'xml'
               AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.file_id = rf.file_id)
        """)
        candidates = cur.fetchall()

    affected = []
    for fid, rid, path, lang in candidates:
        ap = Path(clone.get(rid, "")) / path
        if not ap.exists():
            continue
        ents = parse_xml_file(ap)
        if ents:
            affected.append((fid, rid, path, lang, ents))

    print(f"XML files left unindexed by the bug, now parseable: {len(affected)}")
    repos_to_embed = set()
    with get_conn() as c, c.cursor() as cur:
        for fid, rid, path, lang, ents in affected:
            ne = _persist_entities_recursive(cur, rid, fid, ents)
            code, doc, facts = _persist_chunks_and_facts(cur, rid, fid, ents, lang)
            print(f"  + {path}: entities={ne} code_chunks={code} facts={facts}")
            repos_to_embed.add(rid)
        c.commit()

    if not repos_to_embed:
        print("Nothing to re-embed.")
        return

    for repo in list_repos():
        if repo.repo_id in repos_to_embed:
            print(f"Re-embedding {repo.display_name} (rebuilds FAISS; cached chunks are free)…")
            r = embed_repo(repo.repo_id, on_progress=lambda m: print("   ", m))
            print(f"  {repo.display_name}: code_embedded={r.code_chunks_embedded} docs={r.generated_docs_embedded}")


if __name__ == "__main__":
    main()
