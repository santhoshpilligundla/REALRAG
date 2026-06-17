"""One-shot pipeline state check against the live pgserver DB."""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn


def main() -> None:
    with get_conn() as conn:
        print("=== repos ===")
        for row in conn.execute(
            "SELECT display_name, status FROM repos ORDER BY display_name"
        ).fetchall():
            print(f"  {row[0]:<14} {row[1]}")

        print("\n=== generated_docs by pass_level ===")
        for row in conn.execute(
            "SELECT pass_level, count(*) FROM generated_docs GROUP BY pass_level ORDER BY pass_level"
        ).fetchall():
            print(f"  {row[0]:<12} {row[1]:,}")

        print("\n=== examples (worked examples, incl. L4 extra) ===")
        n = conn.execute("SELECT count(*) FROM examples").fetchone()[0]
        print(f"  total examples: {n:,}")

        print("\n=== cross_repo_edges ===")
        n = conn.execute("SELECT count(*) FROM cross_repo_edges").fetchone()[0]
        print(f"  total edges: {n:,}")

        print("\n=== L4 statement annotations (docs with non-null) ===")
        try:
            n = conn.execute(
                "SELECT count(*) FROM generated_docs WHERE statement_annotations IS NOT NULL"
            ).fetchone()[0]
            print(f"  docs with annotations: {n:,}")
        except Exception as e:
            print(f"  (could not query: {e})")

        print("\n=== code_chunks (embedding targets) ===")
        total = conn.execute("SELECT count(*) FROM code_chunks").fetchone()[0]
        print(f"  total code_chunks: {total:,}")

        print("\n=== doc_chunks ===")
        try:
            total = conn.execute("SELECT count(*) FROM doc_chunks").fetchone()[0]
            print(f"  total doc_chunks: {total:,}")
        except Exception as e:
            print(f"  (no doc_chunks: {e})")

        print("\n=== recent pipeline_runs ===")
        for row in conn.execute(
            "SELECT run_type, status, started_at FROM pipeline_runs ORDER BY started_at DESC LIMIT 12"
        ).fetchall():
            print(f"  {str(row[2])[:19]}  {row[0]:<22} {row[1]}")


if __name__ == "__main__":
    main()
