"""Pass 2 — module-level (per-file) doc-gen. Rolls up Pass 1 entity docs.

Per bible §6 strategy 5: Pass 2 uses a mid-tier LLM (Sonnet) and produces a
narrative that describes how the file's entities collaborate.

For each parseable file with at least one Pass 1 doc, we:
  1. Gather all Pass 1 entity docs in the file
  2. Compose a digest of those docs (entity name + structural + behavioral)
  3. Send to Sonnet with a module-level prompt
  4. Validate the JSON response with ModuleDoc schema (with retry-on-missing)
  5. Persist as pass_level='module', file_id=<the file>
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from lib.db import get_conn
from lib.glossary import glossary_block
from lib.llm import acall_json
from lib.runs_repo import finish_run, start_run


_DIGEST_CHAR_CAP = 28_000


class ModuleDoc(BaseModel):
    structural: str = Field(..., min_length=20)
    behavioral: str = Field(..., min_length=20)
    business: str = Field(..., min_length=10)
    cross_references: str = Field("", description="External touchpoints / collaborators outside this file.")
    key_flows: list[str] = Field(default_factory=list, description="2-5 representative flows through the file.")


@dataclass
class Pass2Result:
    success: bool
    doc_id: UUID | None
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


@dataclass
class BulkPass2Result:
    success: bool
    attempted: int
    succeeded: int
    failed: int
    skipped: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


_SYSTEM_PROMPT = """You are a senior software engineer documenting a single FILE as a coherent module.

You will be given:
  - The file path and repo role
  - A digest of every entity in the file with its Pass 1 doc (structural + behavioral)
  - The repo's domain glossary

Output a JSON object describing the FILE AS A WHOLE — what it contains, how its entities work together, and the role it plays in its repo.

JSON SCHEMA:
{
  "structural":   "...",            /* overall shape: what classes/functions/queries the file contains */
  "behavioral":   "...",            /* how the entities collaborate at runtime */
  "business":     "...",            /* what this file accomplishes in the domain */
  "cross_references": "...",        /* external systems / tables / queries / files this file touches */
  "key_flows": ["...", "..."]       /* 2-5 representative flows through the file */
}

RULES:
  - Ground claims in the entity digests. If a claim isn't supported, omit it.
  - Use glossary terms verbatim — no synonyms.
  - Output ONLY the JSON object. No fences, no preamble.
"""


_MODULE_FEW_SHOT = """
EXEMPLAR — module doc at L4 depth
INPUT (excerpt):
  FILE: rms-core/src/main/java/com/example/rates/PropertyRateCalculator.java
  REPO ROLE: ETL
  ENTITY DIGEST:
    [class] PropertyRateCalculator
      structural: public class with two final DAOs injected via constructor.
      behavioral: orchestrates rent rate retrieval and applies a market factor.
    [method] computeBaseRate
      structural: BigDecimal computeBaseRate(int propcode, String fpcode)
      behavioral: looks up Property by propcode; multiplies current rate by market factor; throws on missing propcode.

OUTPUT:
{
  "structural": "PropertyRateCalculator.java is a single-class file in the rates package. It declares one public class (PropertyRateCalculator) with one public method (computeBaseRate) and a constructor. Two final dependencies — PropertyDAO and RateMatrixDAO — are injected via constructor.",
  "behavioral": "At runtime, computeBaseRate(propcode, fpcode) is the sole entry point: PropertyDAO.find loads the Property, RateMatrixDAO.currentRate loads the unit's current rate, and the result is the product of those two values via Property.getMarketFactor.",
  "business": "Implements the simplest base-rate calculation in the rates pipeline: take the current rate for a unit's floor plan and scale it by the property's market factor. Output is consumed by downstream code that adjusts for seasonality and renewal.",
  "cross_references": "Reads from prop and UNITRATEMATRIX tables (via DAOs). Does NOT touch ysmaster, polog, or seasonalforecast.",
  "key_flows": [
    "Caller invokes computeBaseRate(propcode, fpcode); throws IllegalArgumentException if propcode unknown; otherwise returns BigDecimal product of currentRate and marketFactor.",
    "Constructor wires in PropertyDAO and RateMatrixDAO — class is immutable once built."
  ]
}
"""


def _system_blocks() -> list[dict]:
    text = _SYSTEM_PROMPT + "\n\n" + _MODULE_FEW_SHOT
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def _build_digest(rows: list[dict]) -> str:
    """Compose a compact digest of entity docs for a file."""
    lines: list[str] = []
    for r in rows:
        lines.append(f"[{r['kind']}] {r['name']}")
        struct = (r["structural"] or "").strip()
        if struct:
            lines.append(f"  structural: {struct[:600]}")
        beh = (r["behavioral"] or "").strip()
        if beh:
            lines.append(f"  behavioral: {beh[:600]}")
        biz = (r["business"] or "").strip()
        if biz:
            lines.append(f"  business: {biz[:300]}")
        lines.append("")
    digest = "\n".join(lines)
    if len(digest) > _DIGEST_CHAR_CAP:
        digest = digest[:_DIGEST_CHAR_CAP] + "\n... [truncated]"
    return digest


def list_pending_pass2_files(repo_id: UUID) -> list[UUID]:
    """Files that have Pass 1 docs for >= 2 entities but no Pass 2 doc yet."""
    sql = """
        SELECT f.file_id
          FROM repo_files f
          JOIN entities e ON e.file_id = f.file_id
          JOIN generated_docs d
                 ON d.entity_id = e.entity_id AND d.pass_level = 'entity'
          LEFT JOIN generated_docs m
                 ON m.file_id = f.file_id AND m.pass_level = 'module'
         WHERE f.repo_id = %s
           AND m.doc_id IS NULL
         GROUP BY f.file_id
        HAVING COUNT(DISTINCT e.entity_id) >= 2
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (repo_id,))
        return [row[0] for row in cur.fetchall()]


def count_pending_pass2(repo_id: UUID) -> int:
    return len(list_pending_pass2_files(repo_id))


def _depth_tier_for_module(repo_display_name: str, repo_priority: str | None) -> str:
    """Map repo display_name + priority → depth tier (L1..L4) — same logic as Pass 1."""
    if repo_display_name == "po":
        return "L4"
    if repo_priority == "P0":
        return "L3"
    if repo_priority == "P1":
        return "L2"
    return "L1"


def _fetch_file_context(file_id: UUID) -> tuple[dict, list[dict]] | None:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT f.file_id, f.path, f.language, r.repo_id, r.display_name AS repo_name,
                   r.product_id, r.repo_role, r.priority, r.key_business_concepts AS concepts
              FROM repo_files f
              JOIN repos r ON r.repo_id = f.repo_id
             WHERE f.file_id = %s
            """,
            (file_id,),
        )
        f = cur.fetchone()
        if not f:
            return None

        cur.execute(
            """
            SELECT e.entity_id, e.kind, e.name, e.qualified_name, e.signature,
                   d.structural, d.behavioral, d.business, d.edge_cases, d.cross_references
              FROM entities e
              JOIN generated_docs d
                ON d.entity_id = e.entity_id AND d.pass_level = 'entity'
             WHERE e.file_id = %s
             ORDER BY e.start_line
            """,
            (file_id,),
        )
        rows = list(cur.fetchall())
        return f, rows


async def agenerate_module_doc(file_id: UUID) -> Pass2Result:
    ctx = await asyncio.to_thread(_fetch_file_context, file_id)
    if ctx is None:
        return Pass2Result(False, None, 0, 0, error="file not found")
    f, rows = ctx
    if not rows:
        return Pass2Result(False, None, 0, 0, error="no Pass 1 docs in file")

    digest = _build_digest(rows)
    glossary = glossary_block(f["product_id"], focus_terms=list(f["concepts"] or [])) or "(no glossary)"

    user_prompt = f"""FILE: {f['path']}
LANGUAGE: {f['language']}
REPO: {f['repo_name']}
REPO ROLE: {f['repo_role']}
NUM ENTITIES (with Pass 1 docs): {len(rows)}

{glossary}

ENTITY DIGEST:
{digest}

Output the JSON module doc only.
"""

    try:
        raw, llm = await acall_json(_system_blocks(), user_prompt, tier="mid", max_tokens=2048)
        # Retry-on-missing-field
        for _attempt in range(2):
            try:
                doc = ModuleDoc(**raw)
                if doc.structural.strip() and doc.behavioral.strip() and doc.business.strip():
                    break
                raise ValueError("required fields empty")
            except Exception as e:
                retry_user = (
                    user_prompt
                    + f"\n\nYour previous output was incomplete: {e}. "
                    "Fill all required fields. JSON only."
                )
                raw, llm2 = await acall_json(
                    _system_blocks(), retry_user, tier="mid", max_tokens=2048
                )
                llm = llm2
        else:
            return Pass2Result(False, None, llm.prompt_tokens, llm.completion_tokens,
                               error="retry exhausted")
    except Exception as e:
        return Pass2Result(False, None, 0, 0, error=f"{type(e).__name__}: {e}")

    source_entity_ids = [r["entity_id"] for r in rows]

    def _persist() -> UUID:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO generated_docs
                  (repo_id, file_id, pass_level, depth_tier,
                   structural, behavioral, business, edge_cases,
                   worked_example, cross_references,
                   model_used, prompt_tokens, completion_tokens, verified,
                   source_entity_ids)
                VALUES (%s, %s, 'module', %s,
                        %s, %s, %s, %s,
                        NULL, %s,
                        %s, %s, %s, false, %s)
                ON CONFLICT (file_id, pass_level)
                  WHERE pass_level = 'module'
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
                    f["repo_id"], file_id,
                    _depth_tier_for_module(f["repo_name"], f["priority"]),
                    doc.structural, doc.behavioral, doc.business,
                    "\n".join(f"- {flow}" for flow in doc.key_flows),
                    doc.cross_references,
                    llm.model, llm.prompt_tokens, llm.completion_tokens,
                    source_entity_ids,
                ),
            )
            doc_id = cur.fetchone()[0]
            conn.commit()
            return doc_id

    doc_id = await asyncio.to_thread(_persist)
    return Pass2Result(True, doc_id, llm.prompt_tokens, llm.completion_tokens)


async def _adoc_gen_pass2_repo(
    file_ids: list[UUID],
    cb: Callable[[str], None],
    max_concurrency: int = 6,
) -> tuple[int, int, int, int]:
    """Pass 2 uses Sonnet, smaller concurrency than Pass 1."""
    sem = asyncio.Semaphore(max_concurrency)
    state = {"i": 0, "ok": 0, "fail": 0, "in": 0, "out": 0}
    total = len(file_ids)

    async def _one(fid: UUID) -> None:
        async with sem:
            try:
                r = await agenerate_module_doc(fid)
            except Exception as e:
                r = Pass2Result(False, None, 0, 0, error=f"{type(e).__name__}: {e}")
        state["i"] += 1
        if r.success:
            state["ok"] += 1
            state["in"] += r.prompt_tokens
            state["out"] += r.completion_tokens
        else:
            state["fail"] += 1
        if state["i"] % 10 == 0 or state["i"] == total:
            cb(f"pass-2 {state['i']}/{total} · ok={state['ok']} fail={state['fail']} · "
               f"tokens(in/out)={state['in']}/{state['out']}")

    await asyncio.gather(*(_one(fid) for fid in file_ids))
    return state["ok"], state["fail"], state["in"], state["out"]


def doc_gen_pass2_repo(
    repo_id: UUID,
    on_progress: Callable[[str], None] | None = None,
) -> BulkPass2Result:
    cb = on_progress or (lambda _msg: None)

    file_ids = list_pending_pass2_files(repo_id)
    if not file_ids:
        cb("nothing to doc-gen at module level (need >=2 Pass-1 docs per file)")
        return BulkPass2Result(True, 0, 0, 0, 0, 0, 0)

    cb(f"starting Pass 2 — files={len(file_ids)} (Sonnet, conc=6)")
    run_id = start_run(repo_id, "doc_gen_pass2", notes=f"files={len(file_ids)}")

    try:
        ok, fail, total_in, total_out = asyncio.run(_adoc_gen_pass2_repo(file_ids, cb))
    except Exception as e:
        finish_run(run_id, "error", error_message=f"{type(e).__name__}: {e}")
        return BulkPass2Result(False, 0, 0, 0, 0, 0, 0, str(e))

    counts = {
        "attempted": len(file_ids),
        "succeeded": ok,
        "failed": fail,
        "prompt_tokens": total_in,
        "completion_tokens": total_out,
    }
    finish_run(run_id, "success" if fail == 0 else "error", counts=counts)
    cb(f"done — ok={ok} fail={fail} tokens(in/out)={total_in}/{total_out}")
    return BulkPass2Result(
        success=fail == 0,
        attempted=len(file_ids),
        succeeded=ok,
        failed=fail,
        skipped=0,
        prompt_tokens=total_in,
        completion_tokens=total_out,
    )
