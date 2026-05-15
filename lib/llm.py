"""Anthropic client wrapper. Tier-routed by question/task complexity."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import anthropic

from lib.config import get_settings


_log = logging.getLogger("realrag.llm")


_RETRYABLE = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.APIStatusError,        # 5xx and similar
    anthropic.APITimeoutError,
)


def _is_retryable_status(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIStatusError):
        try:
            code = int(getattr(exc, "status_code", 0) or 0)
            return code >= 500 or code == 429 or code == 408
        except (TypeError, ValueError):
            return False
    return isinstance(exc, _RETRYABLE)


def _backoff_seconds(attempt: int) -> float:
    """Exponential backoff with jitter: 4, 8, 16, 32 seconds (+/- 25%).

    Tuned for Anthropic per-minute token ceilings — gives a full minute window
    on the largest backoff to let the bucket refill.
    """
    base = 2 ** (attempt + 1)
    return base * (0.75 + 0.5 * random.random())


@dataclass
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int


@lru_cache
def _client() -> anthropic.Anthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


@lru_cache
def _async_client() -> anthropic.AsyncAnthropic:
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)


def _model_for_tier(tier: str) -> str:
    settings = get_settings()
    if tier == "reasoning":
        return settings.reasoning_anthropic_model
    if tier == "mid":
        return settings.mid_anthropic_model
    return settings.default_anthropic_model


def _system_blocks(system: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wrap a plain system string into a single text-block; pass-through if list.

    Callers can pass a list with cache_control entries to enable Anthropic prompt
    caching on the static system content. Cached blocks lower per-call input
    cost dramatically when the same system prompt is reused (5-min TTL).
    """
    if isinstance(system, list):
        return system
    return [{"type": "text", "text": system}]


def call(
    system: str | list[dict[str, Any]],
    user: str,
    *,
    tier: str = "default",
    max_tokens: int = 2048,
    max_retries: int = 4,
) -> LLMResult:
    model = _model_for_tier(tier)
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = _client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_system_blocks(system),
                messages=[{"role": "user", "content": user}],
            )
            break
        except Exception as e:
            last_exc = e
            if not _is_retryable_status(e) or attempt >= max_retries:
                raise
            sleep_for = _backoff_seconds(attempt)
            _log.warning(
                "call retryable error (attempt %d/%d): %s — sleeping %.1fs",
                attempt + 1, max_retries + 1, type(e).__name__, sleep_for,
            )
            time.sleep(sleep_for)
    else:  # pragma: no cover
        raise last_exc or RuntimeError("call: exhausted retries")

    text_parts = [
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ]
    return LLMResult(
        text="".join(text_parts),
        model=model,
        prompt_tokens=resp.usage.input_tokens,
        completion_tokens=resp.usage.output_tokens,
    )


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_block(text: str) -> str:
    m = _JSON_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def call_json(
    system: str,
    user: str,
    *,
    tier: str = "default",
    max_tokens: int = 2048,
) -> tuple[dict[str, Any], LLMResult]:
    """Call LLM, parse JSON from response. Raises ValueError if no valid JSON found."""
    result = call(system, user, tier=tier, max_tokens=max_tokens)
    raw = _extract_json_block(result.text)
    try:
        return json.loads(raw), result
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM output not valid JSON: {e}\n--- raw ---\n{result.text[:1000]}") from e


async def acall_json(
    system: str | list[dict[str, Any]],
    user: str,
    *,
    tier: str = "default",
    max_tokens: int = 2048,
    max_retries: int = 4,
) -> tuple[dict[str, Any], LLMResult]:
    """Async version of call_json — retries on transient API errors with exponential backoff.

    Retries on: RateLimitError (429), APIConnectionError, 5xx APIStatusError, APITimeoutError.
    Does NOT retry on: 4xx other than 429, JSON decode errors (those are content-level).
    """
    model = _model_for_tier(tier)
    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = await _async_client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=_system_blocks(system),
                messages=[{"role": "user", "content": user}],
            )
            break
        except Exception as e:
            last_exc = e
            if not _is_retryable_status(e) or attempt >= max_retries:
                raise
            sleep_for = _backoff_seconds(attempt)
            _log.warning(
                "acall_json retryable error (attempt %d/%d): %s — sleeping %.1fs",
                attempt + 1, max_retries + 1, type(e).__name__, sleep_for,
            )
            await asyncio.sleep(sleep_for)
    else:  # pragma: no cover - loop always breaks or raises
        raise last_exc or RuntimeError("acall_json: exhausted retries")

    text_parts = [
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    ]
    text = "".join(text_parts)
    raw = _extract_json_block(text)
    result = LLMResult(
        text=text,
        model=model,
        prompt_tokens=resp.usage.input_tokens,
        completion_tokens=resp.usage.output_tokens,
    )
    try:
        return json.loads(raw), result
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM output not valid JSON: {e}\n--- raw ---\n{text[:1000]}") from e
