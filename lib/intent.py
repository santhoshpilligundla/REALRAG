"""Query understanding — intent classifier + query rewrite + HyDE.

Bible §7.2: cheap LLM classifies the question shape into one of:
  - structural   — "where is X defined?", "what writes to table Y?", "what calls Z?"
                   Answer deterministically from the knowledge graph.
  - factual      — "what is X?"
  - behavioral   — "how does X work?"
  - cross_repo   — "trace X from UI to database"
  - example      — "show me an example of X"
  - speculative  — "why was X designed this way?"

This drives the tier router (§7.2 "Tier router T1→T4").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from lib.llm import call_json


Intent = Literal[
    "structural", "factual", "behavioral",
    "cross_repo", "example", "speculative",
]


_TIER_FOR_INTENT: dict[Intent, str] = {
    "structural": "T1",
    "factual":    "T1",
    "behavioral": "T2",
    "example":    "T1",
    "cross_repo": "T3",
    "speculative":"T2",
}


@dataclass
class IntentResult:
    intent: Intent
    suggested_tier: str
    rewrite: str
    hyde: str | None  # hypothetical answer for HyDE retrieval (vague queries)
    structural_targets: list[str]  # named symbols / tables the query references


_SYSTEM = """You classify a user question about a code base into one of six shapes and prepare it for retrieval.

shapes:
  - structural   — wants a fact answerable from a graph (where defined? what writes to table X? what calls Y?)
  - factual      — wants a definitional answer (what is X?)
  - behavioral   — wants a runtime walkthrough (how does X work?)
  - cross_repo   — wants an end-to-end trace across UI/API/ETL/DB
  - example      — wants a concrete worked example
  - speculative  — wants design/why reasoning, may not be groundable

Output JSON:
{
  "intent": "structural|factual|behavioral|cross_repo|example|speculative",
  "rewrite": "a clean, retrieval-friendly rephrasing of the user's question (preserve domain terms verbatim)",
  "hyde": "a SHORT hypothetical answer that a perfect doc would give to this question — used only if intent in (factual,behavioral). Else null.",
  "structural_targets": ["list of named symbols/tables/files referenced in the question"]
}
"""


def classify(question: str) -> IntentResult:
    user = f"QUESTION:\n{question}\n\nOutput JSON only."
    try:
        raw, _ = call_json(_SYSTEM, user, tier="default", max_tokens=512)
    except Exception:
        # Fallback: dumb rule
        return IntentResult(
            intent="behavioral",
            suggested_tier="T2",
            rewrite=question,
            hyde=None,
            structural_targets=[],
        )

    intent = raw.get("intent", "behavioral")
    if intent not in _TIER_FOR_INTENT:
        intent = "behavioral"
    rewrite = (raw.get("rewrite") or "").strip() or question
    hyde = raw.get("hyde")
    if hyde and not isinstance(hyde, str):
        hyde = None
    targets = raw.get("structural_targets") or []
    if not isinstance(targets, list):
        targets = []
    targets = [str(t) for t in targets if t]

    return IntentResult(
        intent=intent,  # type: ignore[arg-type]
        suggested_tier=_TIER_FOR_INTENT[intent],  # type: ignore[index]
        rewrite=rewrite,
        hyde=hyde,
        structural_targets=targets,
    )
