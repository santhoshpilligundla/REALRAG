"""Opt-in agentic RAG (bible Tier-3): a tool-use loop where the model plans a
multi-step investigation — search, resolve symbols, walk the knowledge graph,
grep source — then answers. Used ONLY when the user enables Agent/Trace mode;
the default fast/streaming path is untouched.

Reuses existing retrieval/KG/code-search functions as tools — no new indexing.
"""
from __future__ import annotations

from uuid import UUID

from psycopg.rows import dict_row

from lib.code_search import search_code
from lib.db import get_conn
from lib.kg import (
    find_entity_by_name,
    reads_from_table,
    trace_chain_from,
    writes_to_table,
)
from lib.llm import _client, _model_for_tier
from lib.retrieval import retrieve

MAX_STEPS = 6

_AGENT_SYS = """You are a senior analyst investigating how a RealPage product (RMS) works, for a BUSINESS audience.

You have tools to search documentation/code, resolve named symbols, walk the cross-repo knowledge graph, and grep the real source. Most calculation logic lives in the 'po' repo (ETL/SQL). Plan a short investigation:
  - Start by searching; if the result is in the wrong area or lacks the exact detail (e.g., a formula), use grep_source or find_symbol to drill in, or trace_cross_repo to follow UI -> API -> SQL -> ETL -> table chains.
  - Stop as soon as you have enough to answer; don't over-call tools.

Then give a FINAL answer in plain, business-understandable English:
  - Lead with a direct answer; keep it concise (~60-150 words).
  - NO code references in the prose (no class/file/method/SQL/table/line names) — translate to business terms.
  - If, after investigating, the evidence doesn't support an answer, say so plainly."""

_TOOLS = [
    {"name": "search_knowledge_base",
     "description": "Hybrid search over generated docs + code chunks. Use for most questions.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "find_symbol",
     "description": "Resolve an exact class/method/query name to its entity, repo, and file.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "what_writes_to_table",
     "description": "Knowledge-graph: entities that WRITE to a database table (deterministic).",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "what_reads_table",
     "description": "Knowledge-graph: entities that READ from a database table (deterministic).",
     "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "trace_cross_repo",
     "description": "Follow the cross-repo chain (UI -> API -> SQL -> ETL -> table) starting from a named entity.",
     "input_schema": {"type": "object", "properties": {"entity_name": {"type": "string"}}, "required": ["entity_name"]}},
    {"name": "grep_source",
     "description": "Keyword-scan the real cloned source files. Use to find an exact formula/column/identifier.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "get_documentation",
     "description": "Get the generated documentation (what it does, business meaning) for a named entity.",
     "input_schema": {"type": "object", "properties": {"entity_name": {"type": "string"}}, "required": ["entity_name"]}},
]


def _repo_ids(product_id: UUID | None) -> list[UUID]:
    sql = "SELECT repo_id FROM repos" + (" WHERE product_id = %s" if product_id else "")
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (product_id,) if product_id else ())
        return [r[0] for r in cur.fetchall()]


def _repos_with_paths(product_id: UUID | None) -> list[tuple[str, str]]:
    sql = "SELECT display_name, clone_path FROM repos WHERE clone_path IS NOT NULL"
    params: tuple = ()
    if product_id:
        sql += " AND product_id = %s"
        params = (product_id,)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def _entity_doc(name: str, product_id: UUID | None) -> str:
    rows = find_entity_by_name(name, product_id=product_id, limit=1)
    if not rows:
        return f"No entity named '{name}' found."
    eid = rows[0]["entity_id"]
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT structural, behavioral, business FROM generated_docs "
            "WHERE entity_id = %s AND pass_level = 'entity' LIMIT 1", (eid,))
        d = cur.fetchone()
    if not d:
        return f"{rows[0]['qualified_name']} exists in {rows[0]['repo_name']} but has no generated doc."
    return (f"{rows[0]['qualified_name']} (in {rows[0].get('file_path')}):\n"
            f"STRUCTURAL: {d.get('structural')}\nBEHAVIORAL: {d.get('behavioral')}\n"
            f"BUSINESS: {d.get('business')}")


def _dispatch(name: str, args: dict, product_id: UUID | None) -> tuple[str, list[dict]]:
    """Run a tool. Returns (text_for_model, citations)."""
    cites: list[dict] = []
    try:
        if name == "search_knowledge_base":
            hits = retrieve(query=args["query"], rewrite=args["query"], hyde=None,
                            structural_targets=[], repo_ids=_repo_ids(product_id),
                            product_id=product_id, top_k=6, rerank=False,
                            prefer_calc=True)
            lines = []
            for h in hits:
                cites.append({"file": (h.file_path or ""), "qname": h.qualified_name or "",
                              "repo": h.repo_name or ""})
                lines.append(f"[{h.repo_name}] {h.qualified_name} ({h.file_path}):\n"
                             f"{(h.content or '')[:500]}")
            return ("\n\n".join(lines) or "No results.", cites)

        if name == "find_symbol":
            rows = find_entity_by_name(args["name"], product_id=product_id, limit=5)
            for r in rows:
                cites.append({"file": r.get("file_path") or "", "qname": r.get("qualified_name") or "",
                              "repo": r.get("repo_name") or ""})
            return ("\n".join(f"{r['qualified_name']} [{r['kind']}] in {r['repo_name']}/{r.get('file_path')}"
                              for r in rows) or "No matching symbol.", cites)

        if name in ("what_writes_to_table", "what_reads_table"):
            fn = writes_to_table if name == "what_writes_to_table" else reads_from_table
            rows = fn(args["table"], product_id=product_id)
            for r in rows[:15]:
                cites.append({"file": r.get("file_path") or "", "qname": r.get("qualified_name") or "",
                              "repo": r.get("repo_name") or ""})
            verb = "write to" if name == "what_writes_to_table" else "read from"
            return ("\n".join(f"{r['qualified_name']} ({r['repo_name']})" for r in rows[:15])
                    or f"Nothing found that {verb}s '{args['table']}'.", cites)

        if name == "trace_cross_repo":
            ents = find_entity_by_name(args["entity_name"], product_id=product_id, limit=1)
            if not ents:
                return (f"No entity named '{args['entity_name']}'.", cites)
            chain = trace_chain_from(ents[0]["entity_id"])
            for r in chain:
                cites.append({"file": r.get("file_path") or "", "qname": r.get("qualified_name") or "",
                              "repo": r.get("repo_name") or ""})
            return ("\n".join(f"depth {r['depth']} [{r.get('edge_kind') or 'start'}] "
                              f"{r['qualified_name']} ({r['repo_name']})" for r in chain)
                    or "No cross-repo chain from that entity.", cites)

        if name == "grep_source":
            ex, _ = search_code(args["pattern"], [args["pattern"]], _repos_with_paths(product_id))
            for e in ex:
                cites.append({"file": e.rel_path, "qname": "", "repo": e.repo})
            return ("\n\n".join(f"{e.repo}/{e.rel_path}:\n{e.snippet[:600]}" for e in ex[:5])
                    or "No source files matched.", cites)

        if name == "get_documentation":
            return (_entity_doc(args["entity_name"], product_id), cites)

        return (f"Unknown tool {name}.", cites)
    except Exception as e:  # never let a tool error kill the loop
        return (f"Tool {name} errored: {type(e).__name__}: {e}", cites)


def run_agent(question: str, product_id: UUID | None = None, history: list[dict] | None = None,
              on_step=None) -> dict:
    """Multi-step tool-use investigation -> business answer. Returns
    {answer, citations, steps, trace}."""
    client = _client()
    model = _model_for_tier("mid")  # Sonnet — good multi-step reasoning at moderate cost

    ctx = ""
    for h in (history or [])[-4:]:
        if h.get("content"):
            ctx += f"{h.get('role')}: {h.get('content','')[:400]}\n"
    user = (f"CONVERSATION SO FAR:\n{ctx}\n\nQUESTION: {question}" if ctx else question)

    messages = [{"role": "user", "content": user}]
    citations: list[dict] = []
    trace: list[str] = []

    for step in range(MAX_STEPS):
        try:
            resp = client.messages.create(model=model, max_tokens=1500, system=_AGENT_SYS,
                                          tools=_TOOLS, messages=messages)
        except Exception as e:
            return {"answer": f"Agent error: {type(e).__name__}: {e}", "citations": citations,
                    "steps": step, "trace": trace}

        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            results = []
            for b in resp.content:
                if getattr(b, "type", None) == "tool_use":
                    label = f"{b.name}({list(b.input.values())[0] if b.input else ''})"
                    trace.append(label)
                    if on_step:
                        on_step(label)
                    out, cites = _dispatch(b.name, b.input, product_id)
                    citations.extend(cites)
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": out[:6000]})
            messages.append({"role": "user", "content": results})
            continue

        # Final answer (no more tool calls).
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return {"answer": text.strip(), "citations": _dedup(citations), "steps": step + 1, "trace": trace}

    # Hit the step cap — force a final answer with no tools.
    messages.append({"role": "user",
                     "content": "Give your final answer now in plain business English, no code references."})
    try:
        resp = client.messages.create(model=model, max_tokens=1200, system=_AGENT_SYS, messages=messages)
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    except Exception as e:
        text = f"Agent could not finalize: {type(e).__name__}: {e}"
    return {"answer": text.strip(), "citations": _dedup(citations), "steps": MAX_STEPS, "trace": trace}


def _dedup(cites: list[dict]) -> list[dict]:
    seen, out = set(), []
    for c in cites:
        k = (c.get("file"), c.get("qname"))
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out[:10]
