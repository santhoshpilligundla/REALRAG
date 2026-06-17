"""Export a representative sample of generated documentation to a readable HTML file."""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn

OUT = Path("docs/generated_docs_sample.html")
ENTITY_PER_REPO = 4
MODULE_SAMPLE = 8
NARRATIVE_SAMPLE = 12


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
    if d.get("inputs"):
        parts.append(f"<details><summary>inputs</summary><pre>{esc(json.dumps(d['inputs'], indent=2))[:2500]}</pre></details>")
    steps = d.get("calculation_steps") or []
    if steps:
        parts.append("<b>Calculation steps:</b><ol>" + "".join(f"<li>{esc(s)}</li>" for s in steps) + "</ol>")
    if d.get("expected_output"):
        parts.append(f"<p><b>Expected output:</b> <code>{esc(json.dumps(d['expected_output']))[:1200]}</code></p>")
    return "".join(parts) or f"<pre>{esc(json.dumps(d, indent=2))[:2000]}</pre>"


def field(label, val):
    if val in (None, ""):
        return ""
    return f"<div class='f'><span class='lbl'>{label}</span><div class='v'>{esc(val)}</div></div>"


def main() -> None:
    blocks = []
    with get_conn() as c:
        # corpus stats
        stats = c.execute("SELECT pass_level, count(*) FROM generated_docs GROUP BY pass_level ORDER BY pass_level").fetchall()
        repos = c.execute("SELECT repo_id, display_name FROM repos ORDER BY display_name").fetchall()

        stat_html = "".join(f"<li>{esc(p)}: <b>{n:,}</b></li>" for p, n in stats)

        # entity docs per repo
        for repo_id, name in repos:
            rows = c.execute("""
                SELECT e.qualified_name, e.kind, d.depth_tier, d.model_used, f.path,
                       d.structural, d.behavioral, d.business, d.edge_cases, d.worked_example, d.cross_references
                  FROM generated_docs d
                  JOIN entities e ON e.entity_id = d.entity_id
                  LEFT JOIN repo_files f ON f.file_id = e.file_id
                 WHERE d.pass_level='entity' AND d.repo_id=%s
                   AND coalesce(d.business,'')<>'' AND d.worked_example IS NOT NULL
                 ORDER BY (d.depth_tier='L4') DESC, random()
                 LIMIT %s
            """, (repo_id, ENTITY_PER_REPO)).fetchall()
            items = []
            for r in rows:
                qn, kind, tier, model, path, st, beh, bus, edge, ex, xref = r
                items.append(f"""
                <div class='doc'>
                  <h4>{esc(qn)} <span class='tag'>{esc(kind)}</span> <span class='tag'>{esc(tier)}</span></h4>
                  <div class='path'>{esc(path)} · {esc(model)}</div>
                  {field('STRUCTURAL', st)}
                  {field('BEHAVIORAL', beh)}
                  {field('BUSINESS', bus)}
                  {field('EDGE CASES', edge)}
                  <div class='f'><span class='lbl'>WORKED EXAMPLE</span><div class='v'>{render_example(ex)}</div></div>
                  {field('CROSS REFERENCES', xref)}
                </div>""")
            blocks.append(f"<h2>{esc(name)} — entity docs ({len(rows)})</h2>" + ("".join(items) or "<em>none</em>"))

        # module docs
        mod = c.execute("""
            SELECT d.narrative_subject, r.display_name, d.structural, d.behavioral
              FROM generated_docs d JOIN repos r ON r.repo_id=d.repo_id
             WHERE d.pass_level='module' ORDER BY random() LIMIT %s
        """, (MODULE_SAMPLE,)).fetchall()
        mblocks = "".join(f"""
            <div class='doc'><h4>{esc(s or '(module)')} <span class='tag'>{esc(rn)}</span></h4>
            {field('STRUCTURAL', st)}{field('BEHAVIORAL', beh)}</div>""" for s, rn, st, beh in mod)
        blocks.append(f"<h2>Module narratives (sample {len(mod)})</h2>{mblocks}")

        # narrative docs
        nar = c.execute("""
            SELECT d.narrative_subject, r.display_name, d.structural, d.behavioral
              FROM generated_docs d JOIN repos r ON r.repo_id=d.repo_id
             WHERE d.pass_level='narrative' ORDER BY random() LIMIT %s
        """, (NARRATIVE_SAMPLE,)).fetchall()
        nblocks = "".join(f"""
            <div class='doc'><h4>{esc(s or '(narrative)')} <span class='tag'>{esc(rn)}</span></h4>
            {field('OVERVIEW', st)}{field('WALKTHROUGH', beh)}</div>""" for s, rn, st, beh in nar)
        blocks.append(f"<h2>Cross-module narratives (sample {len(nar)})</h2>{nblocks}")

    css = """
    body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#1b1b1b}
    h1{border-bottom:3px solid #2b6cb0}h2{margin-top:34px;color:#2b6cb0;border-bottom:1px solid #ddd}
    .doc{border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin:14px 0;background:#fafafa}
    .doc h4{margin:0 0 4px}.tag{font-size:11px;background:#2b6cb0;color:#fff;padding:1px 7px;border-radius:10px;margin-left:6px}
    .path{color:#777;font-size:12px;font-family:monospace;margin-bottom:8px}
    .f{margin:8px 0}.lbl{display:inline-block;font-size:11px;font-weight:700;color:#2b6cb0;letter-spacing:.5px}
    .v{margin-top:2px}pre{background:#f0f0f0;padding:8px;border-radius:6px;overflow:auto;font-size:12px}
    ol{margin:4px 0}code{background:#eef;padding:1px 4px;border-radius:4px}
    """
    htmlout = f"""<!DOCTYPE html><html><head><meta charset='utf-8'><title>RealRAG — Generated Docs Sample</title>
    <style>{css}</style></head><body>
    <h1>RealRAG — Generated Documentation Sample</h1>
    <p>Corpus totals: <ul>{stat_html}</ul></p>
    <p><em>This is a random sample to judge quality. Entity docs prefer L4 (deepest) tier.</em></p>
    {''.join(blocks)}
    </body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(htmlout, encoding="utf-8")
    print(f"Wrote {OUT.resolve()}  ({len(htmlout):,} bytes)")


if __name__ == "__main__":
    main()
