"""OpenAI embeddings — batched, with on-disk cache keyed by sha1 of text."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Sequence

import numpy as np
import openai
import tiktoken

from lib.config import get_settings


CACHE_ROOT = Path("storage/embeddings_cache")
DEFAULT_BATCH = 100
# OpenAI's hard limit for text-embedding-3-* is 8192 tokens per input.
# Truncate to a margin below it so a single oversized chunk can't 400 the batch.
MAX_INPUT_TOKENS = 8000


@lru_cache
def _client() -> openai.OpenAI:
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not set in .env")
    # Bump retries: embedding runs are long and the network here is flaky.
    return openai.OpenAI(api_key=settings.openai_api_key, max_retries=5)


def _model_name() -> str:
    settings = get_settings()
    full = settings.embedding_model
    if full.startswith("openai:"):
        return full.split(":", 1)[1]
    return full


@lru_cache
def _encoding(model: str) -> "tiktoken.Encoding":
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def _truncate(model: str, text: str) -> str:
    """Clip text to MAX_INPUT_TOKENS so the embeddings API never 400s on length."""
    if not text:
        return ""
    enc = _encoding(model)
    toks = enc.encode(text)
    if len(toks) <= MAX_INPUT_TOKENS:
        return text
    return enc.decode(toks[:MAX_INPUT_TOKENS])


def _cache_path(model: str, text: str) -> Path:
    h = hashlib.sha1(f"{model}\n{text}".encode("utf-8", errors="replace")).hexdigest()
    return CACHE_ROOT / model.replace(":", "_") / f"{h}.npy"


def _load_cached(model: str, text: str) -> np.ndarray | None:
    p = _cache_path(model, text)
    if p.exists():
        try:
            return np.load(p)
        except Exception:
            return None
    return None


def _save_cached(model: str, text: str, vec: np.ndarray) -> None:
    p = _cache_path(model, text)
    p.parent.mkdir(parents=True, exist_ok=True)
    np.save(p, vec)


def embed_texts(
    texts: Sequence[str],
    *,
    batch_size: int = DEFAULT_BATCH,
) -> tuple[np.ndarray, str]:
    """Return (matrix [n × d], model_name). Order matches input."""
    if not texts:
        return np.empty((0, 0), dtype=np.float32), _model_name()

    model = _model_name()
    # Clip every input up front: keeps cache keys stable for normal-length texts
    # (only oversized ones change) and guarantees no length-based 400s.
    texts = [_truncate(model, t) for t in texts]
    n = len(texts)
    vectors: list[np.ndarray | None] = [None] * n

    miss_idx: list[int] = []
    for i, t in enumerate(texts):
        cached = _load_cached(model, t)
        if cached is not None:
            vectors[i] = cached
        else:
            miss_idx.append(i)

    client = _client()
    for start in range(0, len(miss_idx), batch_size):
        idx_chunk = miss_idx[start:start + batch_size]
        text_chunk = [texts[i] for i in idx_chunk]
        resp = client.embeddings.create(model=model, input=text_chunk)
        for j, item in enumerate(resp.data):
            vec = np.asarray(item.embedding, dtype=np.float32)
            vectors[idx_chunk[j]] = vec
            _save_cached(model, text_chunk[j], vec)

    matrix = np.stack(vectors).astype(np.float32)
    return matrix, model


def normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms
