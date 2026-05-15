"""L4 enrichment for heart entities. Per bible §6 + scope §11:

  - Multiple worked examples per business rule (3 total: 1 from Pass 1 + 2 new)
  - Statement-level annotation (line-by-line walkthrough of a method body)
  - Git-history context (last 5 meaningful commits affecting the entity range)

Triggered from the UI per repo via 'Enrich L4'. Only entities whose Pass 1 doc
has depth_tier='L4' are eligible.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from lib.db import get_conn
from lib.git_history import commits_to_jsonable, fetch_history_for_range
from lib.glossary import glossary_block
from lib.llm import acall_json
from lib.runs_repo import finish_run, start_run


@dataclass
class EnrichResult:
    success: bool
    examples_added: int
    annotated: bool
    git_added: bool
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


@dataclass
class BulkEnrichResult:
    success: bool
    attempted: int
    succeeded: int
    failed: int
    examples_added: int
    annotated: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class WorkedExampleExtra(BaseModel):
    scenario: str = Field(..., min_length=20)
    inputs: Any = None
    expected_output: Any = None
    calculation_steps: list[str] = Field(default_factory=list)
    is_synthetic: bool = True


class ExtraExamples(BaseModel):
    examples: list[WorkedExampleExtra] = Field(..., min_length=2, max_length=4)


class StatementAnnotation(BaseModel):
    line: int
    code: str
    explanation: str


class StatementAnnotations(BaseModel):
    annotations: list[StatementAnnotation] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompts (cached system blocks)
# ---------------------------------------------------------------------------


_EXAMPLES_SYSTEM = """You are extending a Java method's documentation with TWO ADDITIONAL distinct worked examples that complement the one already produced.

You receive:
  - The entity's source body
  - The original Pass 1 worked example (so you know what NOT to repeat)

Your two new examples should cover DIFFERENT scenarios than the original:
  - One should exercise an edge case (null input, empty list, boundary value, error path)
  - One should exercise a different "happy path" with different input values

Output ONLY a JSON object:
{
  "examples": [
    {"scenario": "...", "inputs": <any>, "expected_output": <any>, "calculation_steps": ["..."], "is_synthetic": true|false},
    {"scenario": "...", "inputs": <any>, "expected_output": <any>, "calculation_steps": ["..."], "is_synthetic": true|false}
  ]
}

Ground each example in the source. Set is_synthetic=true if you invented values; false if values appear in the source.
"""


_ANNOTATION_SYSTEM = """You are annotating a Java method body line-by-line.

You receive:
  - The method's source text with line numbers attached
  - The Pass 1 doc (for context only)

For each NON-TRIVIAL line (skip braces, blank lines, simple variable assignments without semantic weight), produce one annotation:
  {"line": <int>, "code": "<the literal line>", "explanation": "<one-sentence explanation>"}

Output ONLY:
{"annotations": [...]}

Skip trivial lines. Aim for 5-30 annotations depending on method size.
"""


def _system_blocks(prompt: str) -> list[dict]:
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------------------
# Eligibility / data fetch
# ---------------------------------------------------------------------------


def list_pending_l4_entities(repo_id: UUID) -> list[UUID]:
    """L4 entities in this repo that have a Pass 1 doc but no enrichment yet
    (defined as: no statement_annotations AND no extra examples in `examples`)."""
    sql = """
        SELECT DISTINCT e.entity_id
          FROM entities e
          JOIN generated_docs d
                 ON d.entity_id = e.entity_id AND d.pass_level = 'entity'
          LEFT JOIN examples ex
                 ON ex.entity_id = e.entity_id
         WHERE e.repo_id = %s
           AND d.depth_tier = 'L4'
           AND e.kind IN ('class','interface','enum','record','method','constructor')
           AND (d.statement_annotations IS NULL OR d.statement_annotations = '')
         ORDER BY e.entity_id
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (repo_id,))
        return [row[0] for row in cur.fetchall()]


def count_pending_l4_enrich(repo_id: UUID) -> int:
    return len(list_pending_l4_entities(repo_id))


def _fetch_entity_for_enrich(entity_id: UUID) -> dict | None:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT e.entity_id, e.repo_id, e.kind, e.name, e.qualified_name,
                   e.signature, e.start_line, e.end_line,
                   f.path AS file_path,
                   r.display_name AS repo_name,
                   r.product_id   AS product_id,
                   r.clone_path   AS clone_path,
                   r.key_business_concepts AS concepts,
                   d.doc_id, d.worked_example, d.structural, d.behavioral,
                   d.business, d.statement_annotations
              FROM entities e
              JOIN repo_files f ON f.file_id = e.file_id
              JOIN repos r       ON r.repo_id = e.repo_id
              JOIN generated_docs d
                     ON d.entity_id = e.entity_id AND d.pass_level = 'entity'
             WHERE e.entity_id = %s
            """,
            (entity_id,),
        )
        return cur.fetchone()


def _read_body(clone_path: str, file_path: str, start: int, end: int) -> str:
    try:
        full = Path(clone_path) / file_path
        text = full.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        return "\n".join(lines[max(0, start - 1):end])
    except OSError:
        return ""


def _numbered(body: str, start_line: int) -> str:
    out: list[str] = []
    for i, line in enumerate(body.splitlines(), start=start_line):
        out.append(f"{i:5d} | {line}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Enrichment per entity
# ---------------------------------------------------------------------------


async def enrich_entity(entity_id: UUID) -> EnrichResult:
    ent = await asyncio.to_thread(_fetch_entity_for_enrich, entity_id)
    if not ent:
        return EnrichResult(False, 0, False, False, 0, 0, error="entity not found")

    body = await asyncio.to_thread(
        _read_body, ent["clone_path"] or "", ent["file_path"],
        ent["start_line"], ent["end_line"],
    )
    if not body:
        return EnrichResult(False, 0, False, False, 0, 0, error="body unavailable")

    glossary = glossary_block(ent["product_id"], focus_terms=list(ent["concepts"] or [])) or "(no glossary)"
    existing_example = ""
    if ent["worked_example"]:
        try:
            existing_example = json.dumps(ent["worked_example"], indent=2)[:1500]
        except Exception:
            existing_example = "(unreadable)"

    total_in = 0
    total_out = 0

    # ---- Generate two additional examples ----
    examples_added = 0
    examples_user = f"""ENTITY: {ent['qualified_name']} ({ent['kind']})
FILE: {ent['file_path']}

{glossary}

EXISTING WORKED EXAMPLE (do not duplicate):
{existing_example or '(none)'}

SOURCE:
```
{body[:24_000]}
```

Output the JSON object with exactly two new examples.
"""
    try:
        raw, llm = await acall_json(
            _system_blocks(_EXAMPLES_SYSTEM), examples_user,
            tier="default", max_tokens=2048,
        )
        total_in += llm.prompt_tokens
        total_out += llm.completion_tokens
        ex_doc = ExtraExamples(**raw)

        def _persist_examples() -> int:
            with get_conn() as conn, conn.cursor() as cur:
                count = 0
                for ex in ex_doc.examples:
                    cur.execute(
                        """
                        INSERT INTO examples
                          (product_id, repo_id, entity_id, concept, narrative,
                           inputs, outputs, calculation_steps, confidence, verified)
                        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, false)
                        """,
                        (
                            ent["product_id"], ent["repo_id"], entity_id,
                            ent["qualified_name"], ex.scenario,
                            json.dumps(ex.inputs) if ex.inputs is not None else None,
                            json.dumps(ex.expected_output) if ex.expected_output is not None else None,
                            "\n".join(ex.calculation_steps) if ex.calculation_steps else None,
                            0.9 if not ex.is_synthetic else 0.7,
                        ),
                    )
                    count += 1
                conn.commit()
                return count

        examples_added = await asyncio.to_thread(_persist_examples)
    except Exception as e:
        return EnrichResult(False, 0, False, False, total_in, total_out,
                            error=f"examples: {type(e).__name__}: {e}")

    # ---- Statement annotations (only for methods/constructors) ----
    annotated = False
    annotations_text = ""
    if ent["kind"] in ("method", "constructor"):
        ann_user = f"""ENTITY: {ent['qualified_name']} ({ent['kind']})

PASS 1 BEHAVIORAL DOC: {ent['behavioral'] or '(empty)'}

NUMBERED SOURCE (line | code):
{_numbered(body, ent['start_line'])[:24_000]}

Output the JSON object with annotations[] only.
"""
        try:
            raw, llm = await acall_json(
                _system_blocks(_ANNOTATION_SYSTEM), ann_user,
                tier="default", max_tokens=4096,
            )
            total_in += llm.prompt_tokens
            total_out += llm.completion_tokens
            ann_doc = StatementAnnotations(**raw)
            if ann_doc.annotations:
                annotations_text = "\n".join(
                    f"{a.line:5d} | {a.code}\n      ↳ {a.explanation}"
                    for a in ann_doc.annotations
                )
                annotated = True
        except Exception:
            # Statement annotation is optional; fall through.
            pass

    # ---- Git history ----
    git_added = False
    git_payload: list[dict] | None = None
    if ent["clone_path"]:
        commits = await asyncio.to_thread(
            fetch_history_for_range,
            Path(ent["clone_path"]), ent["file_path"],
            ent["start_line"], ent["end_line"], 5,
        )
        if commits:
            git_payload = commits_to_jsonable(commits)
            git_added = True

    # ---- Persist enrichment fields onto the doc row ----
    def _persist_enrich() -> None:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE generated_docs
                   SET statement_annotations = COALESCE(%s, statement_annotations),
                       git_history = COALESCE(%s::jsonb, git_history),
                       generated_at = now()
                 WHERE doc_id = %s
                """,
                (
                    annotations_text or None,
                    json.dumps(git_payload) if git_payload else None,
                    ent["doc_id"],
                ),
            )
            conn.commit()

    await asyncio.to_thread(_persist_enrich)

    return EnrichResult(
        success=True,
        examples_added=examples_added,
        annotated=annotated,
        git_added=git_added,
        prompt_tokens=total_in,
        completion_tokens=total_out,
    )


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------


async def _aenrich_repo(
    entity_ids: list[UUID],
    cb: Callable[[str], None],
    max_concurrency: int = 6,
) -> tuple[int, int, int, int, int, int]:
    sem = asyncio.Semaphore(max_concurrency)
    state = {"i": 0, "ok": 0, "fail": 0, "ex": 0, "ann": 0, "in": 0, "out": 0}
    total = len(entity_ids)

    async def _one(eid: UUID) -> None:
        async with sem:
            try:
                r = await enrich_entity(eid)
            except Exception as e:
                r = EnrichResult(False, 0, False, False, 0, 0, error=f"{type(e).__name__}: {e}")
        state["i"] += 1
        if r.success:
            state["ok"] += 1
            state["ex"] += r.examples_added
            state["ann"] += 1 if r.annotated else 0
            state["in"] += r.prompt_tokens
            state["out"] += r.completion_tokens
        else:
            state["fail"] += 1
        if state["i"] % 20 == 0 or state["i"] == total:
            cb(f"l4-enrich {state['i']}/{total} · ok={state['ok']} fail={state['fail']} · "
               f"examples_added={state['ex']} annotated={state['ann']} · "
               f"tokens(in/out)={state['in']}/{state['out']}")

    await asyncio.gather(*(_one(eid) for eid in entity_ids))
    return state["ok"], state["fail"], state["ex"], state["ann"], state["in"], state["out"]


def enrich_l4_repo(
    repo_id: UUID,
    on_progress: Callable[[str], None] | None = None,
) -> BulkEnrichResult:
    cb = on_progress or (lambda _msg: None)
    entity_ids = list_pending_l4_entities(repo_id)
    if not entity_ids:
        cb("no L4 entities to enrich (run Pass 1 first; this is PO-only currently)")
        return BulkEnrichResult(True, 0, 0, 0, 0, 0, 0, 0)

    cb(f"starting L4 enrichment — entities={len(entity_ids)} (Haiku, conc=6)")
    run_id = start_run(repo_id, "doc_gen_l4_enrich", notes=f"entities={len(entity_ids)}")

    try:
        ok, fail, ex_added, ann, total_in, total_out = asyncio.run(
            _aenrich_repo(entity_ids, cb)
        )
    except Exception as e:
        finish_run(run_id, "error", error_message=f"{type(e).__name__}: {e}")
        return BulkEnrichResult(False, 0, 0, 0, 0, 0, 0, 0, str(e))

    counts = {
        "attempted": len(entity_ids),
        "succeeded": ok,
        "failed": fail,
        "examples_added": ex_added,
        "annotated": ann,
        "prompt_tokens": total_in,
        "completion_tokens": total_out,
    }
    finish_run(run_id, "success" if fail == 0 else "error", counts=counts)
    cb(f"done — ok={ok} fail={fail} examples_added={ex_added} annotated={ann} "
       f"tokens(in/out)={total_in}/{total_out}")
    return BulkEnrichResult(
        success=fail == 0,
        attempted=len(entity_ids),
        succeeded=ok,
        failed=fail,
        examples_added=ex_added,
        annotated=ann,
        prompt_tokens=total_in,
        completion_tokens=total_out,
    )
