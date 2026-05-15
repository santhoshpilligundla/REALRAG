"""Chat orchestrator — hybrid retrieval → Claude with citation-strict prompt → verifier.

Bible §7.2 query pipeline. 4-tier router decides Naive / Self-RAG / Agentic / Live source-walk.
For hackathon, T1 (naive) and T2 (self-RAG) are wired. T3/T4 are stubs that fall through to T2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID

from psycopg.rows import dict_row

from lib.coverage import log_failed_question
from lib.db import get_conn
from lib.glossary import glossary_block
from lib.intent import classify
from lib.kg import reads_from_table, trace_chain_from, writes_to_table
from lib.llm import call_json
from lib.retrieval import Hit, retrieve


@dataclass
class ChatAnswer:
    answer: str
    citations: list[dict] = field(default_factory=list)
    intent: str = "behavioral"
    tier: str = "T1"
    refusal: bool = False
    refusal_reason: str | None = None
    is_faithful: bool | None = None
    used_hits: int = 0
    retrieved_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


_CHAT_SYSTEM = """You are a senior software engineer answering a question about a RealPage codebase. You have been given retrieved context — code chunks and pre-generated docs — drawn from the actual repos.

You MUST:
  - Cite file paths and line ranges for every concrete claim. Format: [file.java:LINE_START-LINE_END]
  - Refuse if the retrieved context does not support an answer. Say "I don't have evidence in the retrieved context to answer this." and explain what's missing. Do NOT guess or invent.
  - Use the domain glossary verbatim. Never substitute synonyms (e.g., RRG ≠ "Revenue Risk Group").

Output a JSON object:
{
  "answer": "...",                 /* prose answer with [file:lines] citations inline */
  "citations": [                    /* every cited (file, range) repeated here for the UI */
    {"file": "rms-core/.../X.java", "start_line": 100, "end_line": 120, "qname": "..."}
  ],
  "is_grounded": true|false,        /* false if you had to refuse */
  "refusal_reason": "..." | null    /* set when is_grounded=false */
}
"""


_CHAT_FEW_SHOT = """
EXEMPLAR
Q: How is the Revenue Trend Widget data populated?
RETRIEVED:
  [generated_doc] RevenueTrendComponent (rm-web/src/app/dashboard/revenue-trend.component.ts:198-220)
    behavioral: dispatches getQueries({ query: 'getRevenueTrendData' }) ...
  [generated_doc] sql_query getRevenueTrendData (ys/ys-core/.../yscore_queries.xml:5234-5267)
    behavioral: SELECT ... FROM revenuetrend rt JOIN propunit pu ...
  [generated_doc] xml_service RevenueTrend (po/.../etl2posql.xml:710-740)
    behavioral: INSERT INTO revenuetrend SELECT ... FROM ysmaster.rentchanges JOIN ...

OUTPUT:
{
  "answer": "The Revenue Trend Widget renders a chart on the property dashboard, fed by a chain of three steps. (1) The Angular component RevenueTrendComponent dispatches the named query 'getRevenueTrendData' [revenue-trend.component.ts:198-220]. (2) ys looks up that name in yscore_queries.xml and runs SELECT against the revenuetrend table joined with propunit [yscore_queries.xml:5234-5267]. (3) The revenuetrend table is populated nightly by the po ETL service named RevenueTrend, which INSERTs into revenuetrend by selecting from ysmaster.rentchanges [etl2posql.xml:710-740].",
  "citations": [
    {"file": "rm-web/src/app/dashboard/revenue-trend.component.ts", "start_line": 198, "end_line": 220, "qname": "RevenueTrendComponent"},
    {"file": "ys/ys-core/src/main/resources/queries/yscore_queries.xml", "start_line": 5234, "end_line": 5267, "qname": "getRevenueTrendData"},
    {"file": "po/.../etl2posql.xml", "start_line": 710, "end_line": 740, "qname": "RevenueTrend"}
  ],
  "is_grounded": true,
  "refusal_reason": null
}
"""


def _system_blocks() -> list[dict]:
    text = _CHAT_SYSTEM + "\n\n" + _CHAT_FEW_SHOT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _format_hit_for_prompt(h: Hit, idx: int) -> str:
    cite = h.citations[0] if h.citations else {}
    file = cite.get("file") or "?"
    start = cite.get("start_line") or "?"
    end = cite.get("end_line") or "?"
    qname = h.qualified_name or "?"
    body = (h.content or "")[:3000]
    return (
        f"\n[{idx}] [{h.pass_level}] {qname}  "
        f"[{file}:{start}-{end}]  (source={h.source}, score={h.score:.2f})\n{body}\n"
    )


def _build_prompt(question: str, hits: list[Hit], glossary: str) -> str:
    if not hits:
        retrieved_block = "(no retrieved context)"
    else:
        retrieved_block = "RETRIEVED CONTEXT:\n" + "\n".join(
            _format_hit_for_prompt(h, i + 1) for i, h in enumerate(hits[:8])
        )

    return f"""USER QUESTION:
{question}

{glossary}

{retrieved_block}

Output the JSON answer object only. Cite file:lines inline for every concrete claim. Refuse cleanly if the context does not support an answer.
"""


# ---------------------------------------------------------------------------
# Tier router
# ---------------------------------------------------------------------------


def _kg_answer_for(intent: str, targets: list[str], product_id: UUID | None) -> str | None:
    """Tier 0 / structural arm — produce a deterministic answer from the KG when possible."""
    if intent != "structural" or not targets:
        return None

    parts: list[str] = []
    for t in targets:
        # Look like "writes to <table>" / "reads from <table>"?
        writers = writes_to_table(t, product_id=product_id)
        readers = reads_from_table(t, product_id=product_id)
        if writers or readers:
            if writers:
                parts.append(f"**Writes to `{t}`** ({len(writers)}):")
                for w in writers[:10]:
                    parts.append(f"  - [{w['kind']}] {w['qualified_name']} in {w['repo_name']}/{w.get('file_path') or '?'}")
            if readers:
                parts.append(f"**Reads from `{t}`** ({len(readers)}):")
                for r in readers[:10]:
                    parts.append(f"  - [{r['kind']}] {r['qualified_name']} in {r['repo_name']}/{r.get('file_path') or '?'}")
    return "\n".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Verifier (chat-side)
# ---------------------------------------------------------------------------


_VERIFIER_SYSTEM = """You are a citation-grounding auditor. Given:
  - A user question
  - The retrieved context provided to the answerer
  - The answer that was produced

Check whether each concrete claim in the answer cites the retrieved context with [file:lines]. Output JSON:
{
  "is_faithful": true|false,
  "issues": ["claim 1 ...", "claim 2 ..."]
}
"""


def _verify(question: str, hits: list[Hit], answer_text: str) -> bool:
    digest = "\n\n".join(_format_hit_for_prompt(h, i + 1) for i, h in enumerate(hits[:6]))
    user = (
        f"QUESTION: {question}\n\n"
        f"RETRIEVED:\n{digest[:8000]}\n\n"
        f"ANSWER:\n{answer_text[:6000]}\n\n"
        "Output the JSON only."
    )
    try:
        raw, _ = call_json(_VERIFIER_SYSTEM, user, tier="default", max_tokens=512)
        return bool(raw.get("is_faithful"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public chat entry
# ---------------------------------------------------------------------------


def _repo_ids_for_product(product_id: UUID | None) -> list[UUID]:
    if product_id is None:
        sql = "SELECT repo_id FROM repos"
        params: tuple = ()
    else:
        sql = "SELECT repo_id FROM repos WHERE product_id = %s"
        params = (product_id,)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [row[0] for row in cur.fetchall()]


def chat(
    question: str,
    *,
    product_id: UUID | None = None,
    session_id: UUID | None = None,
    user_id: str | None = None,
) -> ChatAnswer:
    intent = classify(question)
    repo_ids = _repo_ids_for_product(product_id)
    glossary = glossary_block(product_id) or "(no glossary)"

    # Try the KG / structural shortcut first when applicable.
    kg_answer = _kg_answer_for(intent.intent, intent.structural_targets, product_id)

    hits = retrieve(
        query=question,
        rewrite=intent.rewrite,
        hyde=intent.hyde,
        structural_targets=intent.structural_targets,
        repo_ids=repo_ids,
        product_id=product_id,
        top_k=8,
    )

    if not hits and not kg_answer:
        log_failed_question(
            question=question, product_id=product_id, session_id=session_id, user_id=user_id,
            refusal_reason="no_retrieved_context", retrieved_count=0, used_count=0,
        )
        return ChatAnswer(
            answer="I don't have any indexed context for this question. Try running doc-gen + embed for the relevant repo first.",
            refusal=True, refusal_reason="no_retrieved_context",
            intent=intent.intent, tier=intent.suggested_tier,
        )

    prompt = _build_prompt(question, hits, glossary)
    if kg_answer:
        prompt = (
            "STRUCTURAL FACTS FROM KNOWLEDGE GRAPH (deterministic):\n"
            + kg_answer + "\n\n"
            + prompt
        )

    try:
        raw, llm = call_json(_system_blocks(), prompt, tier="default", max_tokens=2048)
    except Exception as e:
        log_failed_question(
            question=question, product_id=product_id, session_id=session_id, user_id=user_id,
            refusal_reason=f"llm_error:{type(e).__name__}",
            retrieved_count=len(hits), used_count=0,
            suspected_entities=[h.entity_id for h in hits if h.entity_id],
        )
        return ChatAnswer(
            answer=f"LLM call failed: {e}", refusal=True, refusal_reason="llm_error",
            intent=intent.intent, tier=intent.suggested_tier,
            retrieved_hits=len(hits),
        )

    answer_text = raw.get("answer") or ""
    citations = raw.get("citations") or []
    is_grounded = bool(raw.get("is_grounded"))
    refusal_reason = raw.get("refusal_reason")
    is_faithful = _verify(question, hits, answer_text) if is_grounded else False

    if not is_grounded or not is_faithful:
        log_failed_question(
            question=question, product_id=product_id, session_id=session_id, user_id=user_id,
            refusal_reason=refusal_reason or ("not_faithful" if not is_faithful else "ungrounded"),
            retrieved_count=len(hits),
            used_count=len(citations),
            suspected_entities=[h.entity_id for h in hits if h.entity_id][:8],
        )

    return ChatAnswer(
        answer=answer_text,
        citations=citations,
        intent=intent.intent,
        tier=intent.suggested_tier,
        refusal=not is_grounded,
        refusal_reason=refusal_reason,
        is_faithful=is_faithful,
        used_hits=len(citations),
        retrieved_hits=len(hits),
        prompt_tokens=llm.prompt_tokens,
        completion_tokens=llm.completion_tokens,
    )
