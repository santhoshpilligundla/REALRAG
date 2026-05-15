"""Pass 3 — cross-module / cross-repo narrative doc-gen.

Walks the cross_repo_edges graph from each UI entry point (e.g., an rm-web
component that dispatches queries) along all reachable edges, gathers Pass 1
and Pass 2 docs at each step, and asks Opus to compose the workflow narrative.

Stored with pass_level='narrative', narrative_subject=<workflow name>.

Per bible §6 strategy 5: heavy LLM (Opus) for the deepest layer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID

from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from lib.db import get_conn
from lib.glossary import glossary_block
from lib.llm import acall_json
from lib.runs_repo import finish_run, start_run


_MAX_CHAIN_DEPTH = 6
_MAX_CHAIN_NODES = 20
_DIGEST_CHAR_CAP = 24_000


class NarrativeDoc(BaseModel):
    structural: str = Field(..., min_length=20)
    behavioral: str = Field(..., min_length=20)
    business: str = Field(..., min_length=20)
    cross_references: str = Field("", description="External systems / databases / repos involved.")
    chain_steps: list[str] = Field(default_factory=list, description="Ordered walkthrough of the workflow chain.")


@dataclass
class Pass3Result:
    success: bool
    doc_id: UUID | None
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


@dataclass
class BulkPass3Result:
    success: bool
    attempted: int
    succeeded: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


_SYSTEM_PROMPT = """You are a senior software engineer documenting a CROSS-REPO WORKFLOW — a chain of code entities from a UI element down to the database (or vice versa).

You receive:
  - The workflow's starting entity (an rm-web component, a ys controller, etc.)
  - The chain of subsequent entities reached via cross_repo_edges
  - Each step's Pass 1 entity doc and (where available) Pass 2 module doc
  - The repo overviews + domain glossary

Output a JSON narrative explaining the end-to-end flow.

JSON SCHEMA:
{
  "structural":  "...",        /* the chain of entities at a structural level — what each one IS */
  "behavioral":  "...",        /* what happens at runtime when this workflow executes, end to end */
  "business":    "...",        /* the user-facing outcome / business rule the workflow implements */
  "cross_references": "...",   /* tables read/written, external systems touched */
  "chain_steps": ["step 1: ...", "step 2: ..."]    /* ordered walkthrough, one bullet per node */
}

RULES:
  - Ground every claim in the per-step docs you receive. Do not invent.
  - Use the glossary verbatim — no synonyms.
  - Output ONLY the JSON object.
"""


_NARRATIVE_FEW_SHOT = """
EXEMPLAR — narrative for a 4-step workflow
INPUT (excerpt):
  WORKFLOW: RevenueTrendComponent
  CHAIN:
    1. [ts_component] RevenueTrendComponent (rm-web, src/app/dashboard/revenue-trend.component.ts)
       behavioral: dispatches getQueries({ query: 'getRevenueTrendData' }) on widget render
    2. [sql_query] getRevenueTrendData (ys, ys-core/.../yscore_queries.xml)
       behavioral: SELECT ... FROM revenuetrend rt JOIN propunit pu ON rt.propcode=pu.propcode
    3. [db_table] revenuetrend (ys-admin)
       structural: table with columns propcode, week, base_rent, adj_rent, ...
    4. [xml_service] RevenueTrend (po, etl2posql.xml)
       behavioral: INSERT INTO revenuetrend SELECT ... FROM ysmaster.rentchanges JOIN ...

OUTPUT:
{
  "structural": "Workflow RevenueTrendComponent → getRevenueTrendData → revenuetrend → RevenueTrend has four nodes spanning rm-web (UI), ys (named-query), client DB (table), and po (ETL service).",
  "behavioral": "When the dashboard renders, RevenueTrendComponent dispatches a named-query call ('getRevenueTrendData') to ys. ys looks up the SQL in yscore_queries.xml and runs it against the client DB; the SQL reads from the revenuetrend table joined with propunit. The revenuetrend table is populated nightly by the po ETL service named 'RevenueTrend' (etl2posql.xml), which selects from ysmaster.rentchanges and inserts into revenuetrend.",
  "business": "Powers the Revenue Trend widget on the property dashboard. The widget shows weekly trend of recommended vs actual rents per unit, computed nightly and queried on demand by the UI.",
  "cross_references": "Reads: revenuetrend, propunit (client DB). Writes: revenuetrend (po ETL). Source: ysmaster.rentchanges. Repos: rm-web (UI), ys (query catalog), po (ETL), ys-admin (schema source).",
  "chain_steps": [
    "1. UI renders RevenueTrendComponent in src/app/dashboard/; widget calls getQueries with query='getRevenueTrendData'.",
    "2. ys receives the call, looks up <entry key='getRevenueTrendData'> in yscore_queries.xml, executes the SELECT against the client DB.",
    "3. The SELECT reads the revenuetrend table — populated by the po ETL service 'RevenueTrend'.",
    "4. po's RevenueTrend service runs nightly: it SELECTs from ysmaster.rentchanges and INSERTs into revenuetrend in the client DB."
  ]
}
"""


def _system_blocks() -> list[dict]:
    text = _SYSTEM_PROMPT + "\n\n" + _NARRATIVE_FEW_SHOT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------------------
# Workflow discovery
# ---------------------------------------------------------------------------


def list_workflow_starting_entities() -> list[dict]:
    """Find candidate workflow starts: entities with outgoing cross_repo_edges
    that originate from a UI repo or a component-like kind.

    Limit one per starting entity to avoid explosion.
    """
    sql = """
        SELECT DISTINCT e.entity_id, e.repo_id, e.qualified_name, e.kind, e.name,
                        r.display_name AS repo_name
          FROM cross_repo_edges cre
          JOIN entities e ON e.entity_id = cre.from_entity_id
          JOIN repos r    ON r.repo_id = e.repo_id
         WHERE r.repo_role = 'UI'
            OR e.kind IN ('ts_component', 'ts_service', 'ts_class')
         ORDER BY e.qualified_name
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        return list(cur.fetchall())


def list_pending_pass3() -> list[dict]:
    """Workflows whose narrative isn't yet generated."""
    candidates = list_workflow_starting_entities()
    if not candidates:
        return []

    starts = {c["qualified_name"] for c in candidates}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT narrative_subject FROM generated_docs WHERE pass_level = 'narrative'"
        )
        already = {r[0] for r in cur.fetchall() if r[0]}

    return [c for c in candidates if c["qualified_name"] not in already]


def count_pending_pass3() -> int:
    return len(list_pending_pass3())


# ---------------------------------------------------------------------------
# Chain walker (recursive CTE)
# ---------------------------------------------------------------------------


def walk_chain(start_entity_id: UUID, max_depth: int = _MAX_CHAIN_DEPTH) -> list[dict]:
    """Walk cross_repo_edges from a starting entity. Returns ordered list of nodes."""
    sql = """
        WITH RECURSIVE chain (entity_id, depth, path) AS (
            SELECT %s::uuid, 0, ARRAY[%s::uuid]
            UNION ALL
            SELECT cre.to_entity_id, c.depth + 1, c.path || cre.to_entity_id
              FROM chain c
              JOIN cross_repo_edges cre ON cre.from_entity_id = c.entity_id
             WHERE c.depth < %s
               AND NOT (cre.to_entity_id = ANY(c.path))   -- prevent cycles
        )
        SELECT DISTINCT ON (chain.entity_id)
               chain.entity_id, chain.depth,
               e.kind, e.name, e.qualified_name,
               r.display_name AS repo_name,
               f.path         AS file_path,
               d_e.structural AS entity_structural,
               d_e.behavioral AS entity_behavioral,
               d_e.business   AS entity_business,
               d_m.structural AS module_structural,
               d_m.behavioral AS module_behavioral
          FROM chain
          JOIN entities e   ON e.entity_id = chain.entity_id
          JOIN repos r      ON r.repo_id = e.repo_id
          LEFT JOIN repo_files f ON f.file_id = e.file_id
          LEFT JOIN generated_docs d_e
                 ON d_e.entity_id = e.entity_id AND d_e.pass_level = 'entity'
          LEFT JOIN generated_docs d_m
                 ON d_m.file_id = e.file_id AND d_m.pass_level = 'module'
         ORDER BY chain.entity_id, chain.depth
    """
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, (start_entity_id, start_entity_id, max_depth))
        rows = list(cur.fetchall())
    rows.sort(key=lambda r: r["depth"])
    return rows[:_MAX_CHAIN_NODES]


def _build_chain_digest(chain: list[dict]) -> str:
    lines: list[str] = []
    for i, node in enumerate(chain, 1):
        lines.append(f"{i}. [{node['kind']}] {node['qualified_name']} ({node['repo_name']}, {node['file_path'] or '?'})")
        es = (node["entity_structural"] or "").strip()
        eb = (node["entity_behavioral"] or "").strip()
        ebs = (node["entity_business"] or "").strip()
        if es:
            lines.append(f"   structural: {es[:400]}")
        if eb:
            lines.append(f"   behavioral: {eb[:400]}")
        if ebs:
            lines.append(f"   business: {ebs[:300]}")
        lines.append("")
    digest = "\n".join(lines)
    if len(digest) > _DIGEST_CHAR_CAP:
        digest = digest[:_DIGEST_CHAR_CAP] + "\n... [truncated]"
    return digest


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


async def agenerate_narrative(start_entity: dict) -> Pass3Result:
    chain = await asyncio.to_thread(walk_chain, start_entity["entity_id"])
    if len(chain) < 2:
        return Pass3Result(False, None, 0, 0, error="chain too short (no cross-repo edges)")

    digest = _build_chain_digest(chain)

    # Glossary based on first node's product (good enough — chains rarely span products)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT product_id FROM repos WHERE repo_id = %s",
            (start_entity["repo_id"],),
        )
        row = cur.fetchone()
        product_id = row[0] if row else None
    glossary = glossary_block(product_id) or "(no glossary)"

    user_prompt = f"""WORKFLOW SUBJECT: {start_entity['qualified_name']}
STARTING REPO: {start_entity['repo_name']}
CHAIN LENGTH: {len(chain)}

{glossary}

CHAIN:
{digest}

Output the JSON narrative only.
"""

    try:
        raw, llm = await acall_json(
            _system_blocks(), user_prompt, tier="reasoning", max_tokens=4096
        )
        for _attempt in range(2):
            try:
                doc = NarrativeDoc(**raw)
                if (doc.structural.strip() and doc.behavioral.strip() and doc.business.strip()):
                    break
                raise ValueError("required fields empty")
            except Exception as e:
                retry = (
                    user_prompt
                    + f"\n\nYour previous output was incomplete: {e}. Fill all required fields. JSON only."
                )
                raw, llm2 = await acall_json(
                    _system_blocks(), retry, tier="reasoning", max_tokens=4096
                )
                llm = llm2
        else:
            return Pass3Result(False, None, llm.prompt_tokens, llm.completion_tokens,
                               error="retry exhausted")
    except Exception as e:
        return Pass3Result(False, None, 0, 0, error=f"{type(e).__name__}: {e}")

    source_entity_ids = [c["entity_id"] for c in chain]

    def _persist() -> UUID:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_docs
                  (repo_id, pass_level, depth_tier,
                   structural, behavioral, business, edge_cases,
                   worked_example, cross_references,
                   model_used, prompt_tokens, completion_tokens, verified,
                   narrative_subject, narrative_subject_kind,
                   source_entity_ids)
                VALUES (%s, 'narrative', 'L4',
                        %s, %s, %s, %s,
                        NULL, %s,
                        %s, %s, %s, false,
                        %s, 'cross_repo_chain',
                        %s)
                ON CONFLICT (narrative_subject, pass_level)
                  WHERE pass_level = 'narrative'
                  DO UPDATE SET
                    structural = EXCLUDED.structural,
                    behavioral = EXCLUDED.behavioral,
                    business = EXCLUDED.business,
                    edge_cases = EXCLUDED.edge_cases,
                    cross_references = EXCLUDED.cross_references,
                    model_used = EXCLUDED.model_used,
                    prompt_tokens = EXCLUDED.prompt_tokens,
                    completion_tokens = EXCLUDED.completion_tokens,
                    source_entity_ids = EXCLUDED.source_entity_ids,
                    generated_at = now()
                RETURNING doc_id
                """,
                (
                    start_entity["repo_id"],
                    doc.structural, doc.behavioral, doc.business,
                    "\n".join(f"- {step}" for step in doc.chain_steps),
                    doc.cross_references,
                    llm.model, llm.prompt_tokens, llm.completion_tokens,
                    start_entity["qualified_name"],
                    source_entity_ids,
                ),
            )
            doc_id = cur.fetchone()[0]
            conn.commit()
            return doc_id

    doc_id = await asyncio.to_thread(_persist)
    return Pass3Result(True, doc_id, llm.prompt_tokens, llm.completion_tokens)


async def _adoc_gen_pass3_all(
    starts: list[dict],
    cb: Callable[[str], None],
    max_concurrency: int = 3,    # Opus is heavy — small concurrency
) -> tuple[int, int, int, int]:
    sem = asyncio.Semaphore(max_concurrency)
    state = {"i": 0, "ok": 0, "fail": 0, "in": 0, "out": 0}
    total = len(starts)

    async def _one(start: dict) -> None:
        async with sem:
            try:
                r = await agenerate_narrative(start)
            except Exception as e:
                r = Pass3Result(False, None, 0, 0, error=f"{type(e).__name__}: {e}")
        state["i"] += 1
        if r.success:
            state["ok"] += 1
            state["in"] += r.prompt_tokens
            state["out"] += r.completion_tokens
        else:
            state["fail"] += 1
        if state["i"] % 5 == 0 or state["i"] == total:
            cb(f"pass-3 {state['i']}/{total} · ok={state['ok']} fail={state['fail']} · "
               f"tokens(in/out)={state['in']}/{state['out']}")

    await asyncio.gather(*(_one(s) for s in starts))
    return state["ok"], state["fail"], state["in"], state["out"]


def doc_gen_pass3(on_progress: Callable[[str], None] | None = None) -> BulkPass3Result:
    cb = on_progress or (lambda _msg: None)
    starts = list_pending_pass3()
    if not starts:
        cb("no pending workflows for Pass 3 (run cross_repo edge-build first?)")
        return BulkPass3Result(True, 0, 0, 0, 0, 0)

    cb(f"starting Pass 3 — workflows={len(starts)} (Opus, conc=3)")
    run_id = start_run(None, "doc_gen_pass3", notes=f"workflows={len(starts)}")

    try:
        ok, fail, total_in, total_out = asyncio.run(_adoc_gen_pass3_all(starts, cb))
    except Exception as e:
        finish_run(run_id, "error", error_message=f"{type(e).__name__}: {e}")
        return BulkPass3Result(False, 0, 0, 0, 0, 0, str(e))

    counts = {
        "attempted": len(starts), "succeeded": ok, "failed": fail,
        "prompt_tokens": total_in, "completion_tokens": total_out,
    }
    finish_run(run_id, "success" if fail == 0 else "error", counts=counts)
    cb(f"done — ok={ok} fail={fail} tokens(in/out)={total_in}/{total_out}")
    return BulkPass3Result(
        success=fail == 0, attempted=len(starts), succeeded=ok, failed=fail,
        prompt_tokens=total_in, completion_tokens=total_out,
    )
