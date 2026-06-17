"""Show what documentation was actually generated, with quality signals."""
from __future__ import annotations
import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn


def show(title, rows):
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)
    for r in rows:
        print(r)


with get_conn() as c:
    print("=== verified flag distribution ===")
    for r in c.execute("SELECT pass_level, verified, count(*) FROM generated_docs GROUP BY pass_level, verified ORDER BY pass_level, verified").fetchall():
        print(f"  {r[0]:<10} verified={r[1]}  {r[2]:,}")

    print("\n=== empty-field check (entity docs) ===")
    r = c.execute("""
        SELECT
          count(*) AS total,
          count(*) FILTER (WHERE coalesce(structural,'')      = '') AS no_structural,
          count(*) FILTER (WHERE coalesce(behavioral,'')      = '') AS no_behavioral,
          count(*) FILTER (WHERE coalesce(business,'')        = '') AS no_business,
          count(*) FILTER (WHERE worked_example IS NULL OR worked_example::text IN ('null','""','[]','{}')) AS no_example,
          count(*) FILTER (WHERE coalesce(edge_cases,'')      = '') AS no_edge,
          count(*) FILTER (WHERE coalesce(cross_references,'')= '') AS no_xref
        FROM generated_docs WHERE pass_level='entity'
    """).fetchone()
    cols = ["total","no_structural","no_behavioral","no_business","no_example","no_edge","no_xref"]
    for k, v in zip(cols, r):
        print(f"  {k:<16} {v:,}")

    print("\n=== avg content length (entity docs, chars) ===")
    r = c.execute("""
        SELECT round(avg(length(coalesce(structural,'')))) ,
               round(avg(length(coalesce(behavioral,'')))) ,
               round(avg(length(coalesce(business,'')))) ,
               round(avg(length(coalesce(worked_example::text,''))))
        FROM generated_docs WHERE pass_level='entity'
    """).fetchone()
    print(f"  structural~{r[0]}  behavioral~{r[1]}  business~{r[2]}  worked_example~{r[3]}")

    # One concrete entity doc, full content
    print("\n\n##### SAMPLE ENTITY DOC (RecommendationArchiveTask.computeRecommendedRent) #####")
    row = c.execute("""
        SELECT e.qualified_name, d.depth_tier, d.verified, d.model_used,
               d.structural, d.behavioral, d.business, d.edge_cases, d.worked_example, d.cross_references
          FROM generated_docs d JOIN entities e ON e.entity_id=d.entity_id
         WHERE d.pass_level='entity' AND e.qualified_name ILIKE '%computeRecommendedRent%'
         LIMIT 1
    """).fetchone()
    if row:
        labels = ["qualified_name","depth_tier","verified","model_used","STRUCTURAL","BEHAVIORAL","BUSINESS","EDGE_CASES","WORKED_EXAMPLE","CROSS_REFERENCES"]
        for lab, val in zip(labels, row):
            print(f"\n--- {lab} ---\n{val}")
    else:
        print("  (no matching entity doc found)")

    print("\n\n##### SAMPLE MODULE DOC #####")
    row = c.execute("SELECT narrative_subject, left(structural,1500), left(behavioral,1500) FROM generated_docs WHERE pass_level='module' LIMIT 1").fetchone()
    print(row)

    print("\n\n##### SAMPLE NARRATIVE DOC #####")
    row = c.execute("SELECT narrative_subject, left(structural,2000), left(behavioral,2000) FROM generated_docs WHERE pass_level='narrative' LIMIT 1").fetchone()
    print(row)
