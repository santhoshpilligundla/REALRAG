"""Per-product, per-repo FAISS index + chunk-id mapping.

Layout (bible §11):
    storage/faiss/<product>/<kind>.<repo_display>.faiss
    storage/faiss/<product>/<kind>.<repo_display>.idmap
    storage/faiss/<product>/<kind>.<repo_display>.meta.json

Where:
  <product>      — product name slug ('rms', 'onesite', 'ilm', ...)
  <kind>         — 'code' | 'generated' | 'examples' | 'docs'
  <repo_display> — repo display_name (human-readable)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence
from uuid import UUID

import faiss
import numpy as np

from lib.db import get_conn


FAISS_ROOT = Path("storage/faiss")


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", (s or "").strip().lower()) or "_"


def _resolve_product_repo(repo_id: UUID) -> tuple[str, str]:
    """Look up product slug + repo display slug for the given repo_id."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.name, r.display_name
              FROM repos r
              LEFT JOIN products p ON p.product_id = r.product_id
             WHERE r.repo_id = %s
            """,
            (repo_id,),
        )
        row = cur.fetchone()
        if not row:
            return ("unknown", str(repo_id))
        product_name, display = row
        return (_slug(product_name or "unknown"), _slug(display or str(repo_id)))


def _paths(repo_id: UUID, kind: str) -> tuple[Path, Path, Path]:
    product, display = _resolve_product_repo(repo_id)
    base = FAISS_ROOT / product / f"{kind}.{display}"
    return (
        base.with_suffix(base.suffix + ".index") if base.suffix else Path(str(base) + ".index"),
        Path(str(base) + ".idmap"),
        Path(str(base) + ".meta.json"),
    )


def write_index(
    repo_id: UUID,
    kind: str,
    matrix: np.ndarray,
    ids: Sequence[UUID],
    *,
    model: str,
) -> None:
    """Write a fresh per-repo FAISS index. matrix is (n, d) float32; ids align row-wise."""
    if matrix.size == 0:
        return
    idx_path, map_path, meta_path = _paths(repo_id, kind)
    idx_path.parent.mkdir(parents=True, exist_ok=True)

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normed = matrix / norms

    dim = normed.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(normed.astype(np.float32))
    faiss.write_index(index, str(idx_path))

    map_path.write_text(
        "\n".join(str(i) for i in ids), encoding="utf-8"
    )
    meta_path.write_text(
        json.dumps({"model": model, "dim": dim, "n": len(ids), "kind": kind}),
        encoding="utf-8",
    )


def load_index(repo_id: UUID, kind: str) -> tuple[faiss.Index, list[UUID], dict] | None:
    idx_path, map_path, meta_path = _paths(repo_id, kind)
    if not idx_path.exists():
        return None
    index = faiss.read_index(str(idx_path))
    ids = [UUID(line.strip()) for line in map_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return index, ids, meta


def search(
    repo_id: UUID,
    kind: str,
    query_vec: np.ndarray,
    top_k: int = 10,
) -> list[tuple[UUID, float]]:
    loaded = load_index(repo_id, kind)
    if loaded is None:
        return []
    index, ids, _meta = loaded

    q = query_vec.astype(np.float32).reshape(1, -1)
    norm = np.linalg.norm(q)
    if norm > 0:
        q = q / norm

    scores, idxs = index.search(q, top_k)
    out: list[tuple[UUID, float]] = []
    for i, score in zip(idxs[0], scores[0]):
        if 0 <= i < len(ids):
            out.append((ids[i], float(score)))
    return out
