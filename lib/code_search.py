"""Tier-4 code-search fallback (bible §7.2).

When indexed retrieval (code chunks + generated docs) is insufficient, scan the
cloned source files on disk directly, keyword-rank them, and return the best
excerpts so the answerer can respond from the real code. This is a SAFETY NET,
fired only when the index misses — it is keyword-based and slower than the
indexed path, so it must not be the primary retrieval mechanism.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Real source only — .js is excluded because these repos vendor huge minified
# JS libraries (Angular Material, jQuery UI) that swamp a keyword scan.
_EXTS = {".java", ".ts", ".tsx", ".xml", ".sql", ".yaml", ".yml",
         ".properties", ".jrxml", ".py"}
_SKIP_DIRS = {
    ".git", "node_modules", "target", "dist", "build", ".idea", "__pycache__",
    ".angular", "coverage", "test-output", "vendor", "static", "test", "tests",
    "__tests__", "e2e", "mock", "mocks", "fixtures", "spec", "specs", "assets",
    "images", "img", "fonts", "scss", "css", "public", "wwwroot", "samples",
    "examples", "generated", "gen",
}
_SKIP_FILE_SUBSTR = (".min.", ".bundle.", ".spec.", ".test.")
_MAX_FILE_BYTES = 200_000

# Question/glue words that carry no retrieval signal.
_STOP = {
    "the", "and", "for", "are", "was", "were", "how", "what", "where", "why",
    "who", "when", "which", "does", "did", "done", "doing", "use", "used",
    "uses", "using", "via", "with", "from", "into", "that", "this", "these",
    "those", "have", "has", "had", "you", "your", "our", "their", "its", "it",
    "is", "in", "on", "of", "to", "by", "as", "at", "or", "an", "a", "be",
    "calculate", "calculated", "calculation", "compute", "computed", "process",
    "work", "works", "working", "explain", "show", "tell", "about", "get",
    "set", "value", "values", "data", "system", "code", "between", "versus",
    "vs", "they", "them", "we", "do", "can", "will", "would", "should",
}


@dataclass
class CodeExcerpt:
    repo: str
    path: str
    rel_path: str
    score: int
    snippet: str


def _keywords(question: str, extra: list[str], cap: int = 25) -> list[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", question)
    for e in extra:
        raw += re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", e or "")
    seen: dict[str, None] = {}
    for w in raw:
        wl = w.lower()
        if wl in _STOP or len(wl) < 3:
            continue
        seen.setdefault(wl, None)
    kws = list(seen.keys())
    # Prefer longer / rarer-looking terms (more discriminating) when capping.
    kws.sort(key=len, reverse=True)
    return kws[:cap]


def _best_window(text: str, pattern: re.Pattern, window: int = 2800) -> str:
    """Window around the densest cluster of matches (not just the first match)."""
    positions = [m.start() for m in pattern.finditer(text)]
    if not positions:
        return text[:window]
    # Pick the match position with the most other matches within `window` after it.
    best_pos, best_count = positions[0], 0
    for p in positions:
        cnt = sum(1 for q in positions if p <= q < p + window)
        if cnt > best_count:
            best_count, best_pos = cnt, p
    start = max(0, best_pos - 200)
    return text[start:start + window]


def search_code(
    question: str,
    extra_terms: list[str],
    repos: list[tuple[str, str]],   # (display_name, clone_path)
    *,
    read_cap: int = 600,
    top_files: int = 8,
) -> tuple[list[CodeExcerpt], int]:
    """Return (best excerpts, files_read). Two-stage to stay fast on a monorepo:

    1. Walk the tree (no file reads) and rank every candidate by how many distinct
       query terms appear in its PATH — entity/feature names usually live in the
       filename, so this cheaply focuses the search.
    2. Read content for only the top `read_cap` path-ranked files, score by
       distinct-term coverage in the body, and return the best excerpts.
    """
    kws = _keywords(question, extra_terms)
    if not kws:
        return [], 0
    kws_l = [k.lower() for k in kws]
    pattern = re.compile(r"\b(" + "|".join(re.escape(k) for k in kws) + r")\b", re.I)

    # Stage 1 — collect candidate paths, scored by keyword hits in the path.
    candidates: list[tuple[int, str, str, str]] = []  # (path_score, abs, rel, repo)
    for name, root in repos:
        rootp = Path(root)
        if not rootp.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(rootp):
            dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_DIRS]
            for fn in filenames:
                fnl = fn.lower()
                if os.path.splitext(fnl)[1] not in _EXTS:
                    continue
                if any(s in fnl for s in _SKIP_FILE_SUBSTR):
                    continue
                abs = os.path.join(dirpath, fn)
                rel = os.path.relpath(abs, root)
                rl = rel.lower()
                pscore = sum(1 for k in kws_l if k in rl)
                candidates.append((pscore, abs, rel, name))

    # Read the most path-relevant files first; bound the number of reads.
    candidates.sort(key=lambda x: -x[0])
    pool = candidates[:read_cap]

    scored: list[tuple[int, str, str, str, str]] = []  # (score, abs, rel, text, repo)
    read = 0
    for pscore, abs, rel, name in pool:
        try:
            if os.path.getsize(abs) > _MAX_FILE_BYTES:
                continue
            with open(abs, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:
            continue
        read += 1
        matches = pattern.findall(text)
        if matches:
            distinct = len({m.lower() for m in matches})
            # path hits weighted highest, then body breadth, then occurrences
            score = pscore * 100_000 + distinct * 1000 + len(matches)
            scored.append((score, abs, rel, text, name))

    scored.sort(key=lambda x: -x[0])
    out: list[CodeExcerpt] = []
    for score, abs, rel, text, name in scored[:top_files]:
        out.append(CodeExcerpt(repo=name, path=abs, rel_path=rel, score=score,
                               snippet=_best_window(text, pattern)))
    return out, read
