"""Curated business-doc index (NLW-style): human-written business prose — the
bible/scope/glossary HTML plus any business markdown — embedded into a small
standalone FAISS index. Business-worded questions match this directly, fixing
the 'business wording != code vocabulary' drift.

Self-contained and product-agnostic; retrieval treats it as one extra arm. If
the index hasn't been built, search returns [] (no-op) — retrieval still works.
"""
from __future__ import annotations

import html as _html
import json
import re
from functools import lru_cache
from pathlib import Path

import faiss
import numpy as np

from lib.embedder import embed_texts
from lib.config import get_settings

_DIR = Path("storage/faiss/business")
_IDX = _DIR / "business.index"
_CHUNKS = _DIR / "chunks.json"

# Built-in HTML sources
_HTML_SOURCES = [
    Path("docs/RealRAG-bible.html"),
    Path("docs/RealRAG-scope.html"),
    Path("docs/RealRAG-glossary.html"),
]


def _get_extra_dirs() -> tuple[list[Path], list[Path]]:
    """Read EXTRA_DOC_DIRS and EXTRA_SCHEMA_DIRS from .env at call time."""
    s = get_settings()
    doc_dirs = [Path(p.strip()) for p in s.get_extra_doc_dirs()]
    schema_dirs = [Path(p.strip()) for p in s.get_extra_schema_dirs()]
    return doc_dirs, schema_dirs


def _strip_html(h: str) -> str:
    h = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S | re.I)
    h = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|table|ul|ol|pre|blockquote)>", "\n", h)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t]+", " ", h)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", h).strip()


def _chunk(text: str, source: str, title: str, max_chars: int = 1400) -> list[dict]:
    out, buf = [], []
    size = 0
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if size + len(para) > max_chars and buf:
            out.append({"source": source, "title": title, "content": "\n\n".join(buf)})
            buf, size = [], 0
        buf.append(para)
        size += len(para)
    if buf:
        out.append({"source": source, "title": title, "content": "\n\n".join(buf)})
    return out


def _gather() -> list[dict]:
    chunks: list[dict] = []
    doc_dirs, schema_dirs = _get_extra_dirs()

    # Built-in HTML docs
    for p in _HTML_SOURCES:
        if p.exists():
            chunks += _chunk(_strip_html(p.read_text(encoding="utf-8", errors="replace")),
                             source=p.name, title=p.stem)

    # Extra markdown doc dirs (e.g. schema description .md files)
    for d in doc_dirs:
        if d.exists():
            for p in sorted(d.rglob("*.md")):
                chunks += _chunk(p.read_text(encoding="utf-8", errors="replace"),
                                 source=str(p.name), title=p.stem)
        else:
            print(f"[business_docs] WARNING: EXTRA_DOC_DIRS path not found: {d}")

    # Extra SQL schema dirs (DDL files — table/column definitions)
    for d in schema_dirs:
        if d.exists():
            for p in sorted(d.rglob("*.sql")):
                text = p.read_text(encoding="utf-8", errors="replace")
                # Use parent folder name as context (e.g. "ysmaster/public.sql")
                label = f"{p.parent.name}/{p.name}" if p.parent != d else p.name
                chunks += _chunk(text, source=label, title=f"Schema: {p.stem}")
        else:
            print(f"[business_docs] WARNING: EXTRA_SCHEMA_DIRS path not found: {d}")

    return [c for c in chunks if len(c["content"]) > 80]


def build_business_index() -> int:
    """(Re)build the business-doc index. Returns the number of chunks indexed."""
    chunks = _gather()
    if not chunks:
        return 0
    matrix, _model = embed_texts([c["content"] for c in chunks])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = (matrix / norms).astype(np.float32)
    index = faiss.IndexFlatIP(normed.shape[1])
    index.add(normed)
    _DIR.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(_IDX))
    _CHUNKS.write_text(json.dumps(chunks), encoding="utf-8")
    _load.cache_clear()
    return len(chunks)


@lru_cache(maxsize=1)
def _load():
    if not _IDX.exists() or not _CHUNKS.exists():
        return None
    index = faiss.read_index(str(_IDX))
    chunks = json.loads(_CHUNKS.read_text(encoding="utf-8"))
    return index, chunks


def search_business_docs(query_vec: np.ndarray, top_k: int = 5) -> list[dict]:
    loaded = _load()
    if loaded is None:
        return []
    index, chunks = loaded
    q = query_vec.astype(np.float32).reshape(1, -1)
    n = np.linalg.norm(q)
    if n > 0:
        q = q / n
    scores, idxs = index.search(q, top_k)
    out = []
    for i, score in zip(idxs[0], scores[0]):
        if 0 <= i < len(chunks):
            c = chunks[i]
            out.append({**c, "score": float(score)})
    return out
