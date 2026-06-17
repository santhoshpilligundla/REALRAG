"""Chat orchestrator — hybrid retrieval → Claude with citation-strict prompt → verifier.

Bible §7.2 query pipeline. 4-tier router decides Naive / Self-RAG / Agentic / Live source-walk.
For hackathon, T1 (naive) and T2 (self-RAG) are wired. T3/T4 are stubs that fall through to T2.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from uuid import UUID

from psycopg.rows import dict_row

from lib.code_search import search_code
from lib.coverage import log_failed_question
from lib.db import get_conn
from lib.embedder import embed_texts
from lib.glossary import glossary_block, glossary_expansion
from lib.intent import IntentResult, classify
from lib.kg import reads_from_table, trace_chain_from, writes_to_table
from lib.llm import call_json, stream_text
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


_CHAT_SYSTEM = """You are explaining how a RealPage product works to a BUSINESS audience — product managers, operations, and business owners, NOT engineers. You are given retrieved context (source code and pre-generated documentation) from the actual product repositories.

Write the answer in plain, business-understandable English, and KEEP IT SHORT:
  - Be concise. Lead with a direct 1-3 sentence answer to the exact question. Add at most a few short supporting sentences, or a short bulleted list (max ~5 bullets) only if it genuinely aids clarity.
  - Target roughly 60-130 words. Do NOT restate the question, do not pad, and do not enumerate every step or detail unless the user explicitly asks for a full breakdown.
  - Explain in terms of business workflows, concepts, data, and outcomes — what the product does, when it happens, and why it matters to the business.
  - Do NOT put any technical/code references in the answer text: no class names, file names, method names, SQL, table or column names, code symbols, or line numbers. Translate the technical detail into business language (e.g., say "the renewal rate calculation runs each night" — never name a class or file).
  - Use plain terms a property manager would recognize (rents, leases, renewals, forecasts, dashboards, nightly runs).
  - Use the domain glossary terms verbatim (e.g., RRG = "Rent Roll Grid"); never invent synonyms.
  - If the context does not support an answer, say so plainly and state what information is missing. Do not guess or invent.

Record the technical sources you relied on in the citations array — these are stored as evidence and shown separately, and must NOT appear in the answer text.

Output a JSON object:
{
  "answer": "...",                 /* plain business-language prose; NO code/file/method/SQL/table/line references */
  "citations": [                    /* evidence for the UI only, never referenced in the answer text */
    {"file": "...", "start_line": 100, "end_line": 120, "qname": "..."}
  ],
  "is_grounded": true|false,        /* false if you had to refuse */
  "refusal_reason": "..." | null    /* set when is_grounded=false */
}
"""


_CHAT_FEW_SHOT = """
EXEMPLAR
Q: How is the Revenue Trend Widget data populated?
RETRIEVED:
  [generated_doc] RevenueTrendComponent (rm-web/.../revenue-trend.component.ts:198-220)
    behavioral: dispatches getQueries({ query: 'getRevenueTrendData' }) ...
  [generated_doc] sql_query getRevenueTrendData (ys/.../yscore_queries.xml:5234-5267)
    behavioral: SELECT ... FROM revenuetrend rt JOIN propunit pu ...
  [generated_doc] xml_service RevenueTrend (po/.../etl2posql.xml:710-740)
    behavioral: INSERT INTO revenuetrend SELECT ... FROM ysmaster.rentchanges JOIN ...

OUTPUT:
{
  "answer": "The Revenue Trend Widget is a chart on the property dashboard that shows how revenue performance changes over time. The numbers it displays are prepared in three stages. When you open the dashboard, the widget asks for revenue-trend information for the properties you've selected. That request is served by the pricing system, which returns the stored revenue and occupancy figures for those properties. Those figures are refreshed every night by a back-office process that gathers recent rent and lease activity and rolls it up into the revenue-trend numbers. So the widget always reflects the previous night's consolidated revenue and occupancy data.",
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


# Plain-text variant for streaming (no JSON envelope, so tokens render live).
_CHAT_SYSTEM_STREAM = """You are explaining how a RealPage product works to a BUSINESS audience — product managers, operations, business owners, NOT engineers. You are given retrieved context (source code + generated documentation) from the actual product repositories.

Answer in plain, business-understandable English, and KEEP IT SHORT:
  - Lead with a direct 1-3 sentence answer. Add at most a few short supporting sentences or a brief bulleted list only if it truly helps. Target ~60-130 words.
  - Do NOT include code references: no class names, file names, method names, SQL, table/column names, or line numbers. Translate technical detail into business language.
  - Use plain terms a property manager would recognize. Use domain glossary terms verbatim.
  - Answer from the relevant context even if it doesn't match the question's exact wording or names a slightly different scope — explain what the system actually does and how the value is derived. Synthesize across the provided pieces. Only say you don't have enough information if the context is genuinely unrelated to the question; do NOT refuse just because a single "step-by-step formula" or an exact-named artifact isn't present.

Output ONLY the answer prose. No JSON, no headings, no preamble."""


def _system_blocks_stream() -> list[dict]:
    return [{"type": "text", "text": _CHAT_SYSTEM_STREAM, "cache_control": {"type": "ephemeral"}}]


# When the user explicitly asks for the exact formula/table/column, allow naming
# the specific artifact (overrides the no-code-references rule for that answer).
_CHAT_SYSTEM_STREAM_EXACT = _CHAT_SYSTEM_STREAM + """

OVERRIDE: The user explicitly asked for the precise calculation/formula/table/columns. You MAY name the exact table(s), column(s), and give the literal formula/expression from the retrieved sources — be precise and specific. Still open with a one-line plain-English summary, and only state specifics that appear in the retrieved sources (do not invent names)."""


def _system_blocks_stream_exact() -> list[dict]:
    return [{"type": "text", "text": _CHAT_SYSTEM_STREAM_EXACT, "cache_control": {"type": "ephemeral"}}]


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

Output the JSON answer object only. Write the answer in plain business English — do NOT mention class names, file names, methods, SQL, table/column names, or line numbers in the answer text (put technical sources only in the citations array). Refuse cleanly if the context does not support an answer.
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


_VERIFIER_SYSTEM = """You are a faithfulness auditor. Given:
  - A user question
  - The retrieved context provided to the answerer (source code + generated documentation)
  - The answer that was produced (written in plain business language)

Judge whether the answer's substantive claims are SUPPORTED by the retrieved context — i.e., the answer is a fair, business-language summary of what the context says. The answer intentionally contains NO code/file/line references; do NOT penalize the absence of citations. Only mark it unfaithful if the answer states something that the context contradicts or that has no basis in the context at all.

Be lenient: reasonable paraphrase, generalization, and business framing are faithful. Default to faithful unless there is a clear, specific contradiction or fabrication.

Output JSON:
{
  "is_faithful": true|false,
  "issues": ["specific unsupported or contradicted claim, if any"]
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
        raw, _ = call_json(_VERIFIER_SYSTEM, user, tier="mid", max_tokens=512)
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


# Map the intent classifier's suggested tier to an LLM tier (see lib/llm.py:
# default=Haiku, mid=Sonnet, reasoning=Opus).
_TIER_TO_MODEL = {"T1": "default", "T2": "mid", "T3": "reasoning", "T4": "reasoning"}

# Identifier-like tokens (CamelCase / snake_case / dotted) for cheap structural targets.
_IDENT_RE = re.compile(r"\b[A-Za-z]+(?:[A-Z][a-z0-9]+)+\b|\b\w+_\w+\b|\b\w+\.\w+\b")
# Capitalized multi-word phrases ("Leases Needed", "Model Threshold") — a business
# term often maps to a single code symbol with the spaces removed (LeasesNeeded).
_CAP_PHRASE_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\b")

# "How is X calculated" questions — bias retrieval toward the po ETL/SQL engine.
_CALC_RE = re.compile(r"calculat|comput|formula|derived|determined|how .*\b(work|set|generat|populat)", re.I)


def _is_calc_question(question: str) -> bool:
    return bool(_CALC_RE.search(question or ""))


def _fast_intent(question: str) -> IntentResult:
    """Heuristic intent — no LLM call. Used on the fast path to cut ~3s of latency.

    The full LLM classifier (HyDE, careful rewrite) runs only under Deep search.
    """
    ql = question.lower()
    targets = list(_IDENT_RE.findall(question))
    for ph in _CAP_PHRASE_RE.findall(question):
        targets.append(ph)                    # "Leases Needed"
        targets.append(ph.replace(" ", ""))   # "LeasesNeeded" — matches the code symbol
    targets = list(dict.fromkeys(targets))[:8]
    if any(w in ql for w in ("cross", "trace", "end to end", "end-to-end", "across repos")):
        intent, tier = "cross_repo", "T3"
    elif ql.startswith(("what is", "what are", "what's")) or "define" in ql or "definition" in ql:
        intent, tier = "factual", "T1"
    elif any(w in ql for w in ("what writes", "what reads", "where is", "what calls", "which table")):
        intent, tier = "structural", "T1"
    else:
        intent, tier = "behavioral", "T2"
    return IntentResult(intent=intent, suggested_tier=tier, rewrite=question,
                        hyde=None, structural_targets=targets)


def _critical_paths(product_id: UUID | None) -> set[str]:
    """Curated key files per repo (critical_entry_points) → lowercased fragments to
    boost in retrieval. This is NLW-style curation applied to the general index."""
    sql = "SELECT critical_entry_points FROM repos WHERE critical_entry_points IS NOT NULL"
    params: tuple = ()
    if product_id is not None:
        sql += " AND product_id = %s"
        params = (product_id,)
    out: set[str] = set()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for (eps,) in cur.fetchall():
            for ep in (eps or []):
                ep = (ep or "").strip().lower()
                if len(ep) >= 4:
                    out.add(ep)
    return out


def _repos_with_paths(product_id: UUID | None) -> list[tuple[str, str]]:
    sql = "SELECT display_name, clone_path FROM repos WHERE clone_path IS NOT NULL"
    params: tuple = ()
    if product_id is not None:
        sql += " AND product_id = %s"
        params = (product_id,)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def _code_fallback_answer(question, intent, product_id, glossary, answer_tier,
                          session_id, user_id):
    """Tier-4 fallback: indexed retrieval missed — scan the cloned source files
    directly and answer from real code. Returns a ChatAnswer, or None to let the
    caller emit its standard refusal.
    """
    repos = _repos_with_paths(product_id)
    if not repos:
        return None
    excerpts, scanned = search_code(
        question, list(intent.structural_targets) + [intent.rewrite], repos
    )
    if not excerpts:
        return None
    ctx = "\n\n".join(f"FILE: {e.rel_path} (repo {e.repo})\n{e.snippet}" for e in excerpts)
    prompt = (
        f"USER QUESTION:\n{question}\n\n{glossary}\n\n"
        f"SOURCE CODE EXCERPTS (scanned live from the repository because the index had no good match):\n"
        f"{ctx[:16000]}\n\n"
        "Answer in plain, concise business English, based only on these excerpts. "
        "If they still do not answer the question, refuse plainly. Output the JSON answer object only."
    )
    try:
        raw, llm = call_json(_system_blocks(), prompt, tier=answer_tier, max_tokens=1024)
    except Exception:
        return None
    answer_text = raw.get("answer") or ""
    if not bool(raw.get("is_grounded")):
        return None
    citations = [{"file": e.rel_path, "repo": e.repo, "qname": ""} for e in excerpts[:6]]
    return ChatAnswer(
        answer=answer_text, citations=citations, intent=intent.intent,
        tier="T4/code-search", refusal=False, refusal_reason=None,
        is_faithful=True, used_hits=len(citations), retrieved_hits=len(excerpts),
        prompt_tokens=llm.prompt_tokens, completion_tokens=llm.completion_tokens,
    )


def chat(
    question: str,
    *,
    product_id: UUID | None = None,
    session_id: UUID | None = None,
    user_id: str | None = None,
    history: list[dict] | None = None,
    deep_search: bool = False,
) -> ChatAnswer:
    intent = classify(question)
    repo_ids = _repo_ids_for_product(product_id)
    glossary = glossary_block(product_id) or "(no glossary)"

    # --- conversation context (multi-turn follow-ups) ---
    hist_turns = [h for h in (history or []) if h.get("content")][-4:]
    hist_block = ""
    if hist_turns:
        hist_block = (
            "CONVERSATION SO FAR (context; the new question may refer back to this):\n"
            + "\n".join(f"{h.get('role')}: {h.get('content','')[:500]}" for h in hist_turns)
            + "\n\n"
        )
    # So a follow-up like "how is it calculated?" retrieves on-topic, fold the
    # most recent prior user turn into the retrieval query.
    prev_user = next((h.get("content", "") for h in reversed(hist_turns)
                      if h.get("role") == "user"), "")
    retr_rewrite = f"{prev_user} {intent.rewrite}".strip() if prev_user else intent.rewrite

    # Glossary-aware retrieval: fold the definitions of any glossary terms in the
    # question into the embedded query, so the search reaches the implementation
    # (which often uses different vocabulary than the business/UI term).
    expansion = glossary_expansion(product_id, question + " " + intent.rewrite)
    hyde = intent.hyde
    if expansion:
        hyde = (hyde + "\n\n" + expansion) if hyde else expansion

    # Tier routing (bible §7.2): cheap model for trivial questions, Sonnet for
    # synthesis, Opus for cross-repo traces. Previously every answer used Haiku.
    answer_tier = _TIER_TO_MODEL.get(intent.suggested_tier, "mid")

    # Try the KG / structural shortcut first when applicable.
    kg_answer = _kg_answer_for(intent.intent, intent.structural_targets, product_id)

    hits = retrieve(
        query=question,
        rewrite=retr_rewrite,
        hyde=hyde,
        structural_targets=intent.structural_targets,
        repo_ids=repo_ids,
        product_id=product_id,
        top_k=8,
        rerank=deep_search,  # re-rank is an extra LLM round-trip; opt-in for speed
    )

    if not hits and not kg_answer:
        # Index had nothing — fall back to scanning the real source files.
        fb = _code_fallback_answer(question, intent, product_id, glossary,
                                   answer_tier, session_id, user_id)
        if fb is not None:
            return fb
        log_failed_question(
            question=question, product_id=product_id, session_id=session_id, user_id=user_id,
            refusal_reason="no_retrieved_context", retrieved_count=0, used_count=0,
        )
        return ChatAnswer(
            answer="I don't have any indexed context for this question, and a direct source scan found nothing relevant.",
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
    if hist_block:
        prompt = hist_block + prompt

    try:
        raw, llm = call_json(_system_blocks(), prompt, tier=answer_tier, max_tokens=1024)
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

    # Tier-4 fallback: the indexed answer refused — read the real source files
    # before giving up. Only fires on a refusal, so it stays a safety net.
    if not is_grounded:
        fb = _code_fallback_answer(question, intent, product_id, glossary,
                                   answer_tier, session_id, user_id)
        if fb is not None:
            return fb

    # The separate verifier pass was removed for speed: it added a full extra LLM
    # round-trip per question but only set the badge (it never changed the answer).
    # We trust the answer model's own is_grounded self-report.
    is_faithful = is_grounded

    if not is_grounded:
        log_failed_question(
            question=question, product_id=product_id, session_id=session_id, user_id=user_id,
            refusal_reason=refusal_reason or "ungrounded",
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


# ---------------------------------------------------------------------------
# Streaming path (responsive chat) — retrieval up front, answer streamed live
# ---------------------------------------------------------------------------


def prepare_answer(question, *, product_id=None, history=None, deep_search=False) -> dict:
    """Do everything up to (not including) answer generation, then return either:
      - {"mode": "final", "answer", "citations", "tier", ...}  — refusal/no-context
      - {"mode": "stream", "system", "prompt", "tier", "citations", ...} — stream it
    """
    # Build conversation context first (instant, no network).
    hist_turns = [h for h in (history or []) if h.get("content")][-4:]
    hist_block = ""
    if hist_turns:
        hist_block = (
            "CONVERSATION SO FAR (context; the new question may refer back to this):\n"
            + "\n".join(f"{h.get('role')}: {h.get('content','')[:500]}" for h in hist_turns)
            + "\n\n"
        )
    prev_user = next((h.get("content", "") for h in reversed(hist_turns)
                      if h.get("role") == "user"), "")
    embed_query = f"{prev_user} {question}".strip()

    # Fast path uses a local heuristic intent (no LLM call → ~3s faster to first
    # token). Deep search uses the full LLM classifier (HyDE + careful rewrite).
    intent = classify(question) if deep_search else _fast_intent(question)
    matrix, _ = embed_texts([embed_query])  # embedding is ~0.5s, not the bottleneck
    qvec = matrix[0] if getattr(matrix, "size", 0) else None

    repo_ids = _repo_ids_for_product(product_id)
    glossary = glossary_block(product_id) or "(no glossary)"
    retr_rewrite = f"{prev_user} {intent.rewrite}".strip() if prev_user else intent.rewrite

    kg_answer = _kg_answer_for(intent.intent, intent.structural_targets, product_id)
    hits = retrieve(
        query=question, rewrite=retr_rewrite, hyde=intent.hyde,
        structural_targets=intent.structural_targets, repo_ids=repo_ids,
        product_id=product_id, top_k=8, rerank=deep_search, query_vec=qvec,
        prefer_calc=_is_calc_question(question),
        critical_paths=_critical_paths(product_id),
    )

    llm_tier = _TIER_TO_MODEL.get(intent.suggested_tier, "mid")
    display_tier = intent.suggested_tier

    if not hits and not kg_answer:
        # No index hits — scan the cloned source (Tier-4) and stream from that.
        repos = _repos_with_paths(product_id)
        excerpts, _ = (search_code(question, list(intent.structural_targets) + [intent.rewrite], repos)
                       if repos else ([], 0))
        if not excerpts:
            return {"mode": "final", "intent": intent.intent, "tier": display_tier,
                    "citations": [], "refusal": True,
                    "answer": "I don't have any indexed context for this question, and a direct source scan found nothing relevant."}
        context_block = "SOURCE CODE EXCERPTS (scanned live from the repository):\n" + \
            "\n\n".join(f"FILE: {e.rel_path} (repo {e.repo})\n{e.snippet}" for e in excerpts)
        citations = [{"file": e.rel_path, "repo": e.repo, "qname": ""} for e in excerpts[:6]]
        display_tier, llm_tier = "T4/code-search", "mid"
    else:
        context_block = "RETRIEVED CONTEXT:\n" + "\n".join(
            _format_hit_for_prompt(h, i + 1) for i, h in enumerate(hits[:8])
        )
        citations = [c for h in hits[:8] for c in (h.citations[:1] or [])]
        # Enumeration ("what all / list all") — expand context with EVERY entry of
        # the dominant file among the top hits, so the answer can be complete.
        if _is_enumeration(question):
            from collections import Counter
            files = Counter(h.file_path for h in hits[:5] if h.file_path)
            if files:
                top_file = files.most_common(1)[0][0]
                listing = _file_full_listing(top_file)
                if listing and listing.count("\n") >= 8:  # only worth it for many-entry files
                    context_block = (f"COMPLETE CONTENTS of {top_file} "
                                     f"(every entry — use this to give a FULL list):\n{listing}\n\n"
                                     + context_block)

    prompt = hist_block
    if kg_answer:
        prompt += "STRUCTURAL FACTS FROM KNOWLEDGE GRAPH (deterministic):\n" + kg_answer + "\n\n"
    prompt += (
        f"USER QUESTION:\n{question}\n\n{glossary}\n\n{context_block}\n\n"
        "Answer now in plain, concise business English (no code references)."
    )
    system = _system_blocks_stream_exact() if wants_exact(question) else _system_blocks_stream()
    return {"mode": "stream", "system": system, "prompt": prompt,
            "tier": llm_tier, "display_tier": display_tier, "citations": citations,
            "intent": intent.intent, "context_text": context_block}


_UNSURE_MARKERS = (
    "don't have", "do not have", "does not contain", "doesn't contain",
    "not available", "not enough information", "no information", "not contain",
    "unable to", "i don't have evidence", "cannot determine", "can't determine",
    "not present in", "insufficient context", "no relevant", "not found in",
    "isn't enough", "is not enough", "not in the retrieved", "made-up term",
    "i don't have the", "do not contain", "not specified in",
)


# Code-specific tokens that, if present in an answer but NOT in the retrieved
# sources, indicate a fabricated artifact (e.g. a made-up table/class name).
_FAB_RES = (
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\.(?:java|xml|ts|sql|py)\b"),   # file refs
    re.compile(r"\b[A-Z][A-Z0-9_]{5,}\b"),                                # ALLCAPS table
    re.compile(r"\b[A-Z][a-z]+(?:[A-Z][a-z0-9]+){2,}\b"),                 # long CamelCase class
)


def answer_has_fabrication(answer: str, context_text: str) -> bool:
    """True if the answer names a specific code artifact that does NOT appear in
    the retrieved sources — a strong fabrication signal. Cheap (no LLM)."""
    if not answer:
        return False
    ctx = (context_text or "").lower()
    for rx in _FAB_RES:
        for tok in rx.findall(answer):
            if tok.lower() not in ctx:
                return True
    return False


# Named subjects in a question (specific things the user asked about): identifier-like
# tokens (≥2 capitals / snake / dotted, len≥6) + Title-Case multiword phrases.
_SUBJ_TOKEN = re.compile(r"\b\w*[A-Za-z]\w*\b")
_SUBJ_ACRONYM_STOP = {"realpage", "should", "explain", "describe", "between"}


def _question_subjects(question: str) -> list[str]:
    """Code-identifier-like subjects only (e.g. TOPriceOptimizer, EtlToPOExecutor).

    These SHOULD appear in retrieved code if retrieval is on-target, so their
    absence is a reliable drift signal. Title-Case business phrases ("In Place
    Units") are deliberately excluded — they rarely appear verbatim in code, so
    grounding on them would cause false escalations.
    """
    subs: list[str] = []
    for t in _SUBJ_TOKEN.findall(question or ""):
        caps = sum(1 for ch in t if ch.isupper())
        if (len(t) >= 6 and (caps >= 2 or "_" in t)) or "." in t:
            if t.lower() not in _SUBJ_ACRONYM_STOP:
                subs.append(t)
    return list(dict.fromkeys(subs))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def context_covers_subjects(question: str, context_text: str) -> bool:
    """True if the question names no specific subject, OR at least one named subject
    appears in the retrieved context. False ⇒ retrieval likely drifted off the subject
    (the 'confident-wrong neighborhood' failure) ⇒ caller should escalate."""
    subs = _question_subjects(question)
    if not subs:
        return True  # nothing specific to ground — don't force escalation
    ctx = _norm(context_text)
    return any(_norm(s) and _norm(s) in ctx for s in subs)


# Does the user explicitly want the precise artifact (formula/table/column/code)?
_EXACT_RE = re.compile(r"\b(formula|exact|table|column|sql|query|code|field|equation|expression|which table|name of)\b", re.I)


def wants_exact(question: str) -> bool:
    return bool(_EXACT_RE.search(question or ""))


# "List all / what all X" — enumeration questions need EVERY sibling entity of a
# file (e.g. all 91 services of an ETL config), not just the top-8 retrieved.
_ENUM_RE = re.compile(
    r"\b(what all|list all|all the|all of the|everything|all data|all the data|"
    r"all tables|what types of|which (?:data|tables|services|steps)|how many)\b", re.I)


def _is_enumeration(question: str) -> bool:
    return bool(_ENUM_RE.search(question or ""))


def _file_full_listing(file_path: str, limit: int = 150) -> str:
    """Compact listing of EVERY entity in a file (name + short description) — so an
    enumeration answer can be complete instead of limited to the top-8 hits."""
    sql = """
        SELECT e.qualified_name, e.kind,
               left(coalesce(d.business, d.behavioral, e.signature, ''), 130)
          FROM entities e
          JOIN repo_files f ON f.file_id = e.file_id
          LEFT JOIN generated_docs d ON d.entity_id = e.entity_id AND d.pass_level = 'entity'
         WHERE f.path = %s
         ORDER BY e.start_line
         LIMIT %s
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (file_path, limit))
        rows = cur.fetchall()
    if not rows:
        return ""
    return "\n".join(f"- {qn} [{kind}]: {(desc or '').strip()}" for qn, kind, desc in rows)


def answer_looks_unsure(text: str) -> bool:
    """True if an answer reads like a refusal / low-confidence hedge — used to
    decide whether to auto-escalate to Deep search."""
    t = (text or "").lower()
    # Only treat as unsure if it's short-ish OR clearly leads with a hedge; a long
    # answer that mentions "not specified" in passing is still a real answer.
    if not t:
        return True
    head = t[:400]
    return any(m in head for m in _UNSURE_MARKERS)


def stream_answer(prep: dict):
    """Yield answer text deltas for a prepared 'stream' result."""
    try:
        yield from stream_text(prep["system"], prep["prompt"], tier=prep["tier"], max_tokens=1024)
    except Exception as e:
        yield f"\n\n_(streaming error: {type(e).__name__}; try again)_"
