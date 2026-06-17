"""Glossary gap detector.

Scans the generated documentation for recurring domain terms (multi-word
Title-Case phrases + ALL-CAPS table/feature names) and reports the ones that
appear often but have NO definition in the glossary — ranked by frequency.

This is the answer to "how do I know what's missing in the glossary": run this,
review the top of the list, add the real domain terms to data/glossary_rms.yaml.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn

# Leading words that usually start a sentence rather than a domain term.
_STOP_LEAD = {
    "The", "This", "That", "These", "Those", "It", "If", "When", "While", "For",
    "And", "But", "Or", "As", "At", "By", "To", "Of", "On", "An", "A", "Each",
    "Both", "After", "Before", "Once", "Then", "First", "Second", "Third",
    "Finally", "Here", "There", "So", "Note", "Output", "Input", "Phase", "Step",
    "Returns", "Throws", "Takes", "Uses", "Based", "Given", "During", "Since",
    "Private", "Public", "Java", "Angular", "SQL",
}
# Multi-word Title-Case phrase (2-4 words), e.g. "In Place Units", "Rent Roll Grid".
_PHRASE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,3})\b")


def normalize(p: str) -> str:
    return re.sub(r"\s+", " ", p.strip())


def main() -> None:
    with get_conn() as c:
        grows = c.execute("SELECT term, coalesce(canonical,'') FROM domain_glossary").fetchall()
        # Cover by both the short term AND its canonical/full form (RRG <-> Rent Roll Grid).
        gloss = [t.lower() for t, _ in grows] + [cn.lower() for _, cn in grows if cn]
        rows = c.execute("""
            SELECT coalesce(business,'') || ' ' || coalesce(behavioral,'') || ' ' || coalesce(structural,'')
              FROM generated_docs
        """).fetchall()

    counts: Counter[str] = Counter()
    for (text,) in rows:
        for m in _PHRASE.findall(text or ""):
            phrase = normalize(m)
            words = phrase.split()
            if words[0] in _STOP_LEAD:
                # keep domain phrases that merely start with a preposition-ish word
                # only if the phrase is >=3 words (e.g., "In Place Units")
                if len(words) < 3:
                    continue
            counts[phrase] += 1

    def covered(phrase: str) -> bool:
        pl = phrase.lower()
        return any(g in pl or pl in g for g in gloss)

    # Generic engineering vocabulary — not RMS business domain terms. Drop any
    # phrase containing one of these tokens.
    _TECH = {
        "jdbc", "http", "rxjs", "sql", "join", "joins", "null", "order", "group",
        "union", "where", "insert", "update", "delete", "select", "spring", "jdom",
        "struts", "rest", "api", "angular", "typescript", "exception", "sqlexception",
        "preparedstatement", "resultset", "connection", "observable", "subject",
        "subjects", "grid", "xml", "json", "jasperreports", "jrxml", "jasper",
        "servlet", "bean", "pojo", "dto", "dao", "mdc", "uuid", "html", "css",
        "please", "google", "maps", "context", "element", "document", "object",
    }

    def is_business(phrase: str) -> bool:
        words = {w.lower() for w in phrase.split()}
        return not (words & _TECH)

    gaps = [(p, n) for p, n in counts.most_common()
            if n >= 15 and not covered(p) and is_business(p)]

    print(f"Glossary has {len(gloss)} terms. Scanned {len(rows):,} generated docs.\n")
    print(f"Top undefined recurring terms (freq >= 15), {len(gaps)} candidates:\n")
    print(f"  {'freq':>6}  term")
    print(f"  {'-'*6}  {'-'*40}")
    for p, n in gaps[:60]:
        print(f"  {n:>6}  {p}")

    # Cross-check: terms appearing in failed/refused questions.
    print("\n--- terms seen in failed_questions (user actually asked these) ---")
    with get_conn() as c:
        fq = c.execute("SELECT question FROM failed_questions ORDER BY created_at DESC LIMIT 200").fetchall()
    fcounts: Counter[str] = Counter()
    for (q,) in fq:
        for m in _PHRASE.findall(q or ""):
            phrase = normalize(m)
            if not covered(phrase):
                fcounts[phrase] += 1
    for p, n in fcounts.most_common(20):
        print(f"  {n:>4}  {p}")


if __name__ == "__main__":
    main()
