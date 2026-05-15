"""Domain glossary loader and query.

Glossary YAML files live under data/glossary_<product>.yaml. On startup we
upsert each into the domain_glossary table (idempotent). Future products
ship their own YAML; this module is generic.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from uuid import UUID

import yaml

from lib.db import get_conn


GLOSSARY_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class GlossaryTerm:
    term: str
    canonical: str | None
    definition: str
    notes: str | None = None


def _yaml_files() -> list[Path]:
    return sorted(GLOSSARY_DIR.glob("glossary_*.yaml"))


def _product_id_for_name(name: str) -> UUID | None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT product_id FROM products WHERE name = %s", (name,))
        row = cur.fetchone()
        return row[0] if row else None


def bootstrap_glossaries() -> int:
    """Load every glossary_<product>.yaml under data/ and upsert into the DB.

    Idempotent — safe to call on every startup.
    Returns total terms upserted across all files.
    """
    total = 0
    for path in _yaml_files():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        product = data.get("product")
        terms = data.get("terms") or []
        product_id = _product_id_for_name(product) if product else None

        with get_conn() as conn, conn.cursor() as cur:
            for entry in terms:
                term = (entry.get("term") or "").strip()
                if not term:
                    continue
                cur.execute(
                    """
                    INSERT INTO domain_glossary (term, canonical, definition, product_id, notes)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (term, product_id, repo_id) DO UPDATE SET
                      canonical = EXCLUDED.canonical,
                      definition = EXCLUDED.definition,
                      notes = EXCLUDED.notes
                    """,
                    (
                        term,
                        entry.get("canonical"),
                        entry.get("definition") or "",
                        product_id,
                        entry.get("notes"),
                    ),
                )
                total += 1
            conn.commit()
    _glossary_for_product.cache_clear()
    return total


@lru_cache(maxsize=64)
def _glossary_for_product(product_id: UUID | None) -> tuple[GlossaryTerm, ...]:
    sql = """
        SELECT term, canonical, definition, notes
          FROM domain_glossary
         WHERE product_id = %s OR product_id IS NULL
         ORDER BY term
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (product_id,))
        return tuple(
            GlossaryTerm(term=t, canonical=c, definition=d, notes=n)
            for (t, c, d, n) in cur.fetchall()
        )


def glossary_block(product_id: UUID | None, focus_terms: list[str] | None = None) -> str:
    """Format glossary for inclusion in an LLM prompt.

    If focus_terms is given, only include those terms (plus any whose `term`
    appears as a substring of a focus term, e.g., 'RRG' in 'RRG screen').
    Else include everything for the product.
    """
    glossary = _glossary_for_product(product_id)
    if not glossary:
        return ""

    if focus_terms:
        focus_lower = {f.lower() for f in focus_terms}
        filtered = [
            g for g in glossary
            if g.term.lower() in focus_lower
            or any(g.term.lower() in f for f in focus_lower)
            or any(f in g.term.lower() for f in focus_lower)
        ]
        if filtered:
            glossary = tuple(filtered)

    lines = ["DOMAIN GLOSSARY (use these definitions; do not invent synonyms):"]
    for g in glossary:
        canonical = f" (a.k.a. {g.canonical})" if g.canonical else ""
        lines.append(f"  • {g.term}{canonical}: {g.definition}")
        if g.notes:
            lines.append(f"      note: {g.notes}")
    return "\n".join(lines)
