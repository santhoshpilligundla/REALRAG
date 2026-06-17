"""Export generated documentation for a topic/workflow to a readable HTML file.

Usage:  python scripts/export_topic.py "renewal" "Renewal Rates"
  argv[1] = search keyword matched against qualified_name + doc content
  argv[2] = (optional) human title for the report
"""
from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn

TERM = sys.argv[1] if len(sys.argv) > 1 else "renewal"
TITLE = sys.argv[2] if len(sys.argv) > 2 else TERM.title()
LIKE = f"%{TERM}%"
ENTITY_LIMIT = 30
OUT = Path(f"docs/topic_{re.sub(r'[^a-z0-9]+', '_', TERM.lower())}.html")


def esc(x) -> str:
    return html.escape(str(x)) if x is not None else ""


def render_example(raw) -> str:
    if raw in (None, "", "null"):
        return "<em>(none)</em>"
    try:
        d = raw if isinstance(raw, dict) else json.loads(raw)
    except Exception:
        return f"<pre>{esc(raw)[:2000]}</pre>"
    parts = []
    if d.get("scenario"):
        parts.append(f"<p><b>Scenario:</b> {esc(d['scenario'])}</p>")
    steps = d.get("calculation_steps") or []
    if steps:
        parts.append("<b>Calculation steps:</b><ol>" + "".join(f"<li>{esc(s)}</li>" for s in steps) + "</ol>")
    if d.get("expected_output"):
        parts.append(f"<p><b>Expected output:</b> <code>{esc(json.dumps(d['expected_output']))[:1400]}</code></p>")
    return "".join(parts) or f"<pre>{esc(json.dumps(d, indent=2))[:2000]}</pre>"


def field(label, val):
    if val in (None, ""):
        return ""
    return f"<div class='f'><span class='lbl'>{label}</span><div class='v'>{esc(val)}</div></div>"


def main() -> None:
    with get_conn() as c:
        ent = c.execute("""
            SELECT e.qualified_name, e.kind, d.depth_tier, r.display_name, f.path,
                   d.structural, d.behavioral, d.business, d.edge_cases, d.worked_example, d.cross_references,
                   (e.qualified_name ILIKE %s) AS name_match
              FROM generated_docs d
              JOIN entities e ON e.entity_id = d.entity_id
              JOIN repos r ON r.repo_id = d.repo_id
              LEFT JOIN repo_files f ON f.file_id = e.file_id
             WHERE d.pass_level='entity'
               AND (e.qualified_name ILIKE %s OR d.business ILIKE %s OR d.behavioral ILIKE %s)
             ORDER BY (e.qualified_name ILIKE %s) DESC, (d.depth_tier='L4') DESC, e.qualified_name
             LIMIT %s
        """, (LIKE, LIKE, LIKE, LIKE, LIKE, ENTITY_LIMIT)).fetchall()

        mod = c.execute("""
            SELECT d.narrative_subject, r.display_name, d.structural, d.behavioral
              FROM generated_docs d JOIN repos r ON r.repo_id=d.repo_id
             WHERE d.pass_level='module'
               AND (coalesce(d.narrative_subject,'') ILIKE %s OR d.structural ILIKE %s OR d.behavioral ILIKE %s)
             LIMIT 12
        """, (LIKE, LIKE, LIKE)).fetchall()

        nar = c.execute("""
            SELECT d.narrative_subject, r.display_name, d.structural, d.behavioral
              FROM generated_docs d JOIN repos r ON r.repo_id=d.repo_id
             WHERE d.pass_level='narrative'
               AND (coalesce(d.narrative_subject,'') ILIKE %s OR d.structural ILIKE %s OR d.behavioral ILIKE %s)
             LIMIT 15
        """, (LIKE, LIKE, LIKE)).fetchall()

    ent_html = ""
    for r in ent:
        qn, kind, tier, repo, path, st, beh, bus, edge, ex, xref, _ = r
        ent_html += f"""
        <div class='doc'>
          <h4>{esc(qn)} <span class='tag'>{esc(repo)}</span> <span class='tag'>{esc(kind)}</span> <span class='tag'>{esc(tier)}</span></h4>
          <div class='path'>{esc(path)}</div>
          {field('STRUCTURAL', st)}{field('BEHAVIORAL', beh)}{field('BUSINESS', bus)}{field('EDGE CASES', edge)}
          <div class='f'><span class='lbl'>WORKED EXAMPLE</span><div class='v'>{render_example(ex)}</div></div>
          {field('CROSS REFERENCES', xref)}
        </div>"""

    mod_html = "".join(f"<div class='doc'><h4>{esc(s or '(module)')} <span class='tag'>{esc(rn)}</span></h4>{field('STRUCTURAL', st)}{field('BEHAVIORAL', beh)}</div>" for s, rn, st, beh in mod)
    nar_html = "".join(f"<div class='doc'><h4>{esc(s or '(narrative)')} <span class='tag'>{esc(rn)}</span></h4>{field('OVERVIEW', st)}{field('WALKTHROUGH', beh)}</div>" for s, rn, st, beh in nar)

    css = """body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px}
    h1{border-bottom:3px solid #2b6cb0}h2{margin-top:30px;color:#2b6cb0;border-bottom:1px solid #ddd}
    .doc{border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin:14px 0;background:#fafafa}
    .doc h4{margin:0 0 4px}.tag{font-size:11px;background:#2b6cb0;color:#fff;padding:1px 7px;border-radius:10px;margin-left:6px}
    .path{color:#777;font-size:12px;font-family:monospace;margin-bottom:8px}
    .f{margin:8px 0}.lbl{font-size:11px;font-weight:700;color:#2b6cb0;letter-spacing:.5px}
    .v{margin-top:2px}ol{margin:4px 0}code{background:#eef;padding:1px 4px;border-radius:4px}pre{background:#f0f0f0;padding:8px;border-radius:6px;font-size:12px;overflow:auto}"""

    out = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>RealRAG — {esc(TITLE)} docs</title><style>{css}</style></head><body>
    <h1>RealRAG Documentation — {esc(TITLE)}</h1>
    <p>Matched on keyword <code>{esc(TERM)}</code>: {len(ent)} entity docs, {len(mod)} module narratives, {len(nar)} cross-module narratives.</p>
    <h2>Entity documentation ({len(ent)})</h2>{ent_html or '<em>none</em>'}
    <h2>Module narratives ({len(mod)})</h2>{mod_html or '<em>none</em>'}
    <h2>Cross-module narratives ({len(nar)})</h2>{nar_html or '<em>none</em>'}
    </body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(out, encoding="utf-8")
    print(f"Wrote {OUT.resolve()}  ({len(out):,} bytes)")
    print(f"entity={len(ent)} module={len(mod)} narrative={len(nar)}")
    print("Top entity matches:")
    for r in ent[:15]:
        print(f"  [{r[3]}] {r[0]}  ({r[2]})")


if __name__ == "__main__":
    main()
