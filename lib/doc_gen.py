"""Pass-1 entity-level doc generation with six-perspective schema.

Implements bible §6 strategies:
  - schema-constrained generation (Pydantic + retry-on-missing-field)
  - parser-first extraction (signatures/types/lines come from tree-sitter)
  - six perspectives (structural/behavioral/business/edge_cases/worked_example/cross_references)
  - few-shot prompting (2 reference exemplars in cached system prompt)
  - domain glossary expansion (definitions, not just terms)
  - verifier pass (cheap LLM faithfulness check after gen)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from lib.db import get_conn
from lib.glossary import glossary_block
from lib.llm import acall_json, call_json
from lib.runs_repo import finish_run, start_run


_BODY_CHAR_CAP = 32_000


def _read_entity_body(clone_path: str, file_path: str, start_line: int, end_line: int) -> str:
    """Read the actual entity body from disk by line range. Falls back to '' on failure."""
    try:
        full = Path(clone_path) / file_path
        text = full.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        slice_text = "\n".join(lines[max(0, start_line - 1):end_line])
        if len(slice_text) > _BODY_CHAR_CAP:
            return slice_text[:_BODY_CHAR_CAP] + "\n... [truncated]"
        return slice_text
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Schemas (bible §6 strategy 2: schema-constrained generation)
# ---------------------------------------------------------------------------


class WorkedExample(BaseModel):
    scenario: str = Field(..., min_length=20)
    inputs: Any = None
    expected_output: Any = None
    calculation_steps: list[str] = Field(default_factory=list)
    is_synthetic: bool = Field(
        default=False,
        description="True when the LLM had to invent values (no real example in source).",
    )


class EntityDoc(BaseModel):
    structural: str = Field(..., min_length=20)
    behavioral: str = Field(..., min_length=20)
    business: str = Field("", description="Business rule / why this exists.")
    edge_cases: str = Field("", description="Boundary conditions, error modes.")
    worked_example: WorkedExample | None = None
    cross_references: str = Field("", description="What calls / what's called / data refs.")


class VerifierResult(BaseModel):
    is_faithful: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)


@dataclass
class DocGenResult:
    success: bool
    doc_id: UUID | None
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Tier policy (bible scope §11 indexing priority)
# ---------------------------------------------------------------------------


def _depth_tier_for(repo_display_name: str, repo_priority: str) -> str:
    if repo_display_name == "po":
        return "L4"
    if repo_priority == "P0":
        return "L3"
    if repo_priority == "P1":
        return "L2"
    return "L1"


def _required_fields_for_tier(tier: str) -> set[str]:
    base = {"structural", "behavioral"}
    if tier in ("L2", "L3", "L4"):
        base.add("business")
    if tier in ("L3", "L4"):
        base |= {"edge_cases", "worked_example", "cross_references"}
    return base


def _max_tokens_for_tier(tier: str) -> int:
    return {"L1": 768, "L2": 1536, "L3": 3072, "L4": 6144}.get(tier, 2048)


# ---------------------------------------------------------------------------
# System prompt — STATIC across calls so Anthropic prompt-caching applies.
# Few-shot exemplars (bible §6 strategy 4 + bible glossary "Few-shot prompting")
# ---------------------------------------------------------------------------


_SYSTEM_INSTRUCTIONS = """You are a senior software engineer writing internal documentation for a code entity (a class, interface, enum, method, constructor, SQL query, XML config entry, or DB column).

You are given:
  - The entity's KIND (class/method/etc.)
  - Its qualified name + file path
  - Its source body (the actual code/text)
  - The repo's role (UI/API/ETL/Reports/Config) and a domain glossary
  - A DEPTH TIER (L1..L4) and REQUIRED FIELDS to fill

You produce a single JSON object with up to six perspectives:

  1. structural        — what it IS in the type system (signature, fields, parameters, return type, hierarchy if a type)
  2. behavioral        — what it DOES at runtime (steps, side effects, what it returns under what conditions)
  3. business          — WHY it exists; the domain rule it implements; in plain language a non-engineer can read
  4. edge_cases        — boundary conditions, error modes, special values, null handling, timing concerns
  5. worked_example    — a concrete scenario with sample inputs, expected outputs, and the step-by-step calculation
  6. cross_references  — what THIS calls (callees), what likely calls THIS (callers), tables/queries/external systems it touches

CRITICAL RULES:
  - Ground every claim in the SOURCE shown to you. Do NOT invent signatures, return types, table names, or fields not in the source.
  - If you don't know, write "unknown" — never fabricate.
  - For worked_example.is_synthetic: set FALSE only if the example uses values literally appearing in the source (e.g., a constant, a default, a value seen in a SQL query). Set TRUE if you had to invent values.
  - Use the DOMAIN GLOSSARY definitions verbatim — do not invent synonyms (e.g., RRG is "Rent Roll Grid"; never "Revenue Risk Group").
  - Output ONLY the JSON object. No markdown fences, no preamble, no commentary outside the JSON.

JSON SCHEMA:
{
  "structural": "...",                    /* 1-3 short paragraphs */
  "behavioral": "...",                    /* 1-3 short paragraphs */
  "business":  "...",                     /* required for L2+; plain English */
  "edge_cases": "...",                    /* required for L3+; bullets ok */
  "worked_example": {                     /* required for L3+; null only for L1/L2 */
    "scenario": "...",
    "inputs": <dict | list | scalar>,
    "expected_output": <dict | list | scalar>,
    "calculation_steps": ["...", "..."],
    "is_synthetic": true|false
  } OR null,
  "cross_references": "..."               /* required for L3+; what calls/is called/touched */
}
"""


_FEW_SHOT_HEADER = """
=========================================
EXEMPLARS — match this voice and depth.
=========================================
"""


# Hand-crafted reference example #1: a substantive Java service class (L4).
_FEW_SHOT_EXAMPLE_CLASS = """\
EXEMPLAR 1 — class entity at L4 depth
INPUT (excerpt):
  KIND: class
  QUALIFIED NAME: com.example.rates.PropertyRateCalculator
  FILE: rms-core/src/main/java/com/example/rates/PropertyRateCalculator.java
  SOURCE:
  ```java
  public class PropertyRateCalculator {
      private final PropertyDAO propertyDao;
      private final RateMatrixDAO rateDao;
      public PropertyRateCalculator(PropertyDAO p, RateMatrixDAO r) { this.propertyDao = p; this.rateDao = r; }
      public BigDecimal computeBaseRate(int propcode, String fpcode) {
          Property prop = propertyDao.find(propcode);
          if (prop == null) throw new IllegalArgumentException("unknown propcode");
          BigDecimal current = rateDao.currentRate(propcode, fpcode);
          return current.multiply(prop.getMarketFactor());
      }
  }
  ```

OUTPUT:
{
  "structural": "PropertyRateCalculator is a public class with two final dependencies — PropertyDAO and RateMatrixDAO — injected via its constructor. It exposes one public method computeBaseRate(int propcode, String fpcode) returning BigDecimal. No fields are mutable; the class is effectively immutable once constructed.",
  "behavioral": "computeBaseRate(propcode, fpcode) looks up the Property by propcode via PropertyDAO; if no Property exists it throws IllegalArgumentException. Otherwise it queries the current rate from RateMatrixDAO and multiplies it by the property's market factor (Property.getMarketFactor()), returning the product. No persistence; the call is read-only and side-effect-free apart from the DAO reads.",
  "business": "The class implements the simplest case of rent calculation: take the current rate for a unit's floor plan and scale it by the property's market factor. This produces a 'base' rate, which downstream code may then adjust for seasonality, renewal, etc. The market factor is usually a number near 1.0 — values >1 push rents up for hot markets, <1 for soft markets.",
  "edge_cases": "Throws IllegalArgumentException for unknown propcode (PropertyDAO.find returns null). Does NOT validate fpcode — passing a nonexistent fpcode results in whatever RateMatrixDAO.currentRate returns (likely null → NullPointerException on multiply). Market factor of zero zeros out the rate; negative values would invert it. No nullability guard on Property.getMarketFactor.",
  "worked_example": {
    "scenario": "Compute the base rate for a 2BR floor plan at property 5001 with current rate $1500 and market factor 1.05.",
    "inputs": { "propcode": 5001, "fpcode": "2BR-STD", "current_rate": 1500.00, "market_factor": 1.05 },
    "expected_output": { "base_rate": 1575.00 },
    "calculation_steps": [
      "PropertyDAO.find(5001) returns Property(propcode=5001, marketFactor=1.05)",
      "RateMatrixDAO.currentRate(5001, '2BR-STD') returns 1500.00",
      "1500.00 * 1.05 = 1575.00",
      "computeBaseRate returns 1575.00"
    ],
    "is_synthetic": true
  },
  "cross_references": "Callees: PropertyDAO.find (DB read on prop), RateMatrixDAO.currentRate (DB read on UNITRATEMATRIX likely), Property.getMarketFactor (POJO accessor), BigDecimal.multiply. Callers (inferred): rate orchestration code that needs an unadjusted rate. External systems: prop and UNITRATEMATRIX tables (read-only). Cross-cutting: this class does NOT touch ysmaster, polog, or seasonal forecast tables."
}
"""


_FEW_SHOT_EXAMPLE_METHOD = """\
EXEMPLAR 2 — method entity at L3 depth
INPUT (excerpt):
  KIND: method
  QUALIFIED NAME: com.example.surrogate.SurrogateFinder.pickPeers
  FILE: rms-yoda/src/main/java/com/example/surrogate/SurrogateFinder.java
  SOURCE:
  ```java
  public List<Integer> pickPeers(int propcode, int desired) {
      List<Integer> candidates = peerDao.candidates(propcode);
      if (candidates.size() <= desired) return candidates;
      return candidates.stream()
                       .sorted(Comparator.comparingDouble(this::distance))
                       .limit(desired)
                       .collect(Collectors.toList());
  }
  ```

OUTPUT:
{
  "structural": "Public instance method on SurrogateFinder. Signature: List<Integer> pickPeers(int propcode, int desired). Returns a list of peer propcodes sized at most `desired`.",
  "behavioral": "Loads all candidate peer propcodes for `propcode` via peerDao.candidates(). If the candidate list size is at-or-below `desired`, returns it as-is (no sorting). Otherwise sorts candidates by ascending distance (this::distance) and returns the first `desired`. The sort key is whatever distance() produces — typically a similarity or geographic distance metric.",
  "business": "Used during property onboarding (Yoda) to choose surrogate (peer) properties when a new property has insufficient direct rental history. The closer the peer (by distance metric) the more representative its history is for forecasting and rates on the new property.",
  "edge_cases": "Returns an empty list when peerDao returns empty (no candidates). When fewer candidates than `desired` exist, returns all of them (no padding). Sort is stable but ties broken by stream insertion order. NOT null-safe on peerDao.candidates() — NPE if it returns null. desired<=0 returns an empty list (limit(0)).",
  "worked_example": {
    "scenario": "Pick the 3 nearest peers for a new property 9999 from 5 candidates.",
    "inputs": { "propcode": 9999, "desired": 3, "candidates": [101, 202, 303, 404, 505], "distances": {"101": 12.0, "202": 5.0, "303": 8.0, "404": 25.0, "505": 3.0} },
    "expected_output": { "peers": [505, 202, 303] },
    "calculation_steps": [
      "peerDao.candidates(9999) returns [101, 202, 303, 404, 505]",
      "candidates.size() == 5 > desired (3), so we sort",
      "sort by distance: [505 (3.0), 202 (5.0), 303 (8.0), 101 (12.0), 404 (25.0)]",
      "limit(3) keeps [505, 202, 303]",
      "return [505, 202, 303]"
    ],
    "is_synthetic": true
  },
  "cross_references": "Callees: peerDao.candidates (likely DB read), distance() (private method on SurrogateFinder, sort key). Callers (inferred): the surrogate-finder pipeline in rms-yoda when onboarding a new propcode."
}
"""


def _system_blocks() -> list[dict]:
    """Return the system message as Anthropic blocks with prompt-caching enabled.

    The whole block is identical across all doc-gen calls, so Anthropic caches it
    after the first call (5-min TTL). With 10x concurrency on Pass 1 we hit the
    cache on ~99% of calls — saves substantial input-token cost.
    """
    text = "\n".join([
        _SYSTEM_INSTRUCTIONS,
        _FEW_SHOT_HEADER,
        _FEW_SHOT_EXAMPLE_CLASS,
        _FEW_SHOT_EXAMPLE_METHOD,
    ])
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# ---------------------------------------------------------------------------
# Per-call user prompt (bible §6 domain-glossary expansion)
# ---------------------------------------------------------------------------


def _build_user_prompt(
    *,
    entity_kind: str,
    qualified_name: str,
    signature: str,
    body: str,
    file_path: str,
    repo_role: str,
    repo_product_id: UUID | None,
    business_concepts: list[str],
    depth_tier: str,
    required_fields: set[str],
) -> str:
    required_list = ", ".join(sorted(required_fields))
    glossary = glossary_block(repo_product_id, focus_terms=business_concepts) or "(no glossary)"
    return f"""DEPTH TIER: {depth_tier}
REQUIRED FIELDS (must be filled, not empty): {required_list}

REPO ROLE: {repo_role}

{glossary}

ENTITY KIND: {entity_kind}
QUALIFIED NAME: {qualified_name}
FILE: {file_path}
SIGNATURE: {signature}

SOURCE:
```
{body}
```

Output the JSON object only.
"""


def _retry_user_prompt(original: str, missing: list[str]) -> str:
    return (
        original
        + "\n\nYour previous output was incomplete. The following REQUIRED fields "
          f"were empty or missing: {missing}. Output the JSON object again with all "
          "required fields filled. No commentary, no markdown, JSON only."
    )


# ---------------------------------------------------------------------------
# Validation (with retry-on-missing-field)
# ---------------------------------------------------------------------------


def _validate(raw: dict, required: set[str]) -> EntityDoc:
    """Coerce + validate the LLM JSON output. Raises ValueError if required fields empty."""
    if "worked_example" in raw and raw["worked_example"] in ("", None, {}):
        raw["worked_example"] = None
    doc = EntityDoc(**raw)

    missing = []
    for field in required:
        if field == "worked_example":
            if doc.worked_example is None:
                missing.append(field)
        else:
            val = getattr(doc, field, "") or ""
            if not val.strip():
                missing.append(field)
    if missing:
        raise ValueError(f"required fields empty: {missing}")
    return doc


def _missing_required(raw: dict, required: set[str]) -> list[str]:
    """Return list of required fields missing/empty in raw, without raising."""
    missing = []
    for field in required:
        if field == "worked_example":
            we = raw.get("worked_example")
            if we in (None, "", {}, []):
                missing.append(field)
        else:
            val = (raw.get(field) or "").strip() if isinstance(raw.get(field), str) else ""
            if not val:
                missing.append(field)
    return missing


# ---------------------------------------------------------------------------
# Verifier pass (cheap LLM faithfulness check; bible §11 "verifier")
# ---------------------------------------------------------------------------


_VERIFIER_SYSTEM = """You are a documentation auditor. You will be given:
  1. The original SOURCE code of an entity.
  2. A generated DOC describing that entity (six perspectives, JSON).

Your job: identify any claim in the DOC that is NOT supported by the SOURCE.

A "claim" is a specific assertion: a method name, a field name, a return type, a table reference, a behavior step, an edge case, a fact in the worked example.

Output ONLY a JSON object:
{
  "is_faithful": true|false,
  "unsupported_claims": ["claim 1 ...", "claim 2 ..."],
  "confidence": 0.0-1.0
}

is_faithful = true ONLY if every concrete claim in the doc is grounded in the source. Synthetic worked examples are OK as long as the LLM marked them is_synthetic=true and the calculation steps follow the source's logic.
"""


async def averify_doc(body: str, doc: EntityDoc) -> tuple[VerifierResult | None, int, int]:
    """Run the verifier on a generated doc. Returns (result, in_tokens, out_tokens)."""
    user = (
        f"SOURCE:\n```\n{body[:_BODY_CHAR_CAP]}\n```\n\n"
        f"GENERATED DOC:\n```json\n{doc.model_dump_json(indent=2)}\n```\n\n"
        "Output the JSON object only."
    )
    try:
        raw, llm = await acall_json(_VERIFIER_SYSTEM, user, tier="default", max_tokens=1024)
        return VerifierResult(**raw), llm.prompt_tokens, llm.completion_tokens
    except Exception:
        return None, 0, 0


# ---------------------------------------------------------------------------
# Entity-level doc generation (sync + async)
# ---------------------------------------------------------------------------


def _fetch_entity_data(entity_id: UUID) -> dict | None:
    with get_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT
              e.entity_id, e.repo_id, e.kind, e.name, e.qualified_name,
              e.signature, e.start_line, e.end_line,
              f.path AS file_path,
              r.display_name AS repo_display_name,
              r.product_id   AS repo_product_id,
              r.priority      AS repo_priority,
              r.repo_role     AS repo_role,
              r.clone_path    AS clone_path,
              r.key_business_concepts AS concepts
            FROM entities e
            JOIN repo_files f ON f.file_id = e.file_id
            JOIN repos r ON r.repo_id = e.repo_id
            WHERE e.entity_id = %s
            """,
            (entity_id,),
        )
        return cur.fetchone()


def _persist_doc(
    *,
    repo_id: UUID,
    entity_id: UUID,
    depth: str,
    doc: EntityDoc,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    verified: bool | None = None,
    verifier_issues: list[str] | None = None,
    source_body_hash: str | None = None,
) -> UUID:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO generated_docs
              (repo_id, entity_id, pass_level, depth_tier,
               structural, behavioral, business, edge_cases,
               worked_example, cross_references,
               model_used, prompt_tokens, completion_tokens, verified,
               source_body_hash)
            VALUES (%s, %s, 'entity', %s,
                    %s, %s, %s, %s,
                    %s::jsonb, %s,
                    %s, %s, %s, %s, %s)
            ON CONFLICT (entity_id, pass_level) WHERE pass_level = 'entity'
              DO UPDATE SET
              depth_tier        = EXCLUDED.depth_tier,
              structural        = EXCLUDED.structural,
              behavioral        = EXCLUDED.behavioral,
              business          = EXCLUDED.business,
              edge_cases        = EXCLUDED.edge_cases,
              worked_example    = EXCLUDED.worked_example,
              cross_references  = EXCLUDED.cross_references,
              model_used        = EXCLUDED.model_used,
              prompt_tokens     = EXCLUDED.prompt_tokens,
              completion_tokens = EXCLUDED.completion_tokens,
              verified          = EXCLUDED.verified,
              source_body_hash  = EXCLUDED.source_body_hash,
              generated_at      = now()
            RETURNING doc_id
            """,
            (
                repo_id, entity_id, depth,
                doc.structural, doc.behavioral, doc.business, doc.edge_cases,
                json.dumps(doc.worked_example.model_dump()) if doc.worked_example else None,
                doc.cross_references,
                model, prompt_tokens, completion_tokens,
                bool(verified) if verified is not None else False,
                source_body_hash,
            ),
        )
        doc_id = cur.fetchone()[0]

        # Multi-vector: write a 'summary' chunk pointing to this entity, with
        # the Pass-1 doc text as content. Body chunks are already in code_chunks
        # from the chunker pass. The summary chunk is what the retriever uses
        # for "doc-vec" hits (bible §7.2 parallel retrieval).
        summary_text = _summary_text(doc)
        if summary_text:
            cur.execute(
                """
                INSERT INTO code_chunks
                  (repo_id, file_id, entity_id, content, language,
                   start_line, end_line, sha, vector_kind)
                SELECT
                    %s, e.file_id, e.entity_id, %s, 'doc-summary',
                    e.start_line, e.end_line, %s, 'summary'
                  FROM entities e
                 WHERE e.entity_id = %s
                ON CONFLICT DO NOTHING
                """,
                (repo_id, summary_text, hashlib.sha1(summary_text.encode("utf-8")).hexdigest(), entity_id),
            )
        conn.commit()
        return doc_id


def _summary_text(doc: EntityDoc) -> str:
    parts = [doc.structural.strip(), doc.behavioral.strip()]
    if doc.business and doc.business.strip():
        parts.append(f"Business: {doc.business.strip()}")
    return "\n\n".join(p for p in parts if p)


def generate_entity_doc(entity_id: UUID) -> DocGenResult:
    """Synchronous Pass-1 doc-gen for one entity. Persists to generated_docs.

    Includes retry-on-missing-field but NOT verifier pass — the bulk async path
    handles verifier. This sync path is for ad-hoc single-entity calls.
    """
    ent = _fetch_entity_data(entity_id)
    if not ent:
        return DocGenResult(False, None, 0, 0, error="entity not found")

    body = _read_entity_body(
        ent["clone_path"] or "",
        ent["file_path"],
        ent["start_line"],
        ent["end_line"],
    )
    if not body:
        body = ent["signature"] or ent["name"]

    depth = _depth_tier_for(ent["repo_display_name"], ent["repo_priority"])
    required = _required_fields_for_tier(depth)

    user_prompt = _build_user_prompt(
        entity_kind=ent["kind"],
        qualified_name=ent["qualified_name"] or ent["name"],
        signature=ent["signature"] or "",
        body=body,
        file_path=ent["file_path"],
        repo_role=ent["repo_role"],
        repo_product_id=ent["repo_product_id"],
        business_concepts=list(ent["concepts"] or []),
        depth_tier=depth,
        required_fields=required,
    )
    max_tok = _max_tokens_for_tier(depth)
    system = _system_blocks()

    total_in = 0
    total_out = 0
    try:
        raw, llm = call_json(system, user_prompt, tier="default", max_tokens=max_tok)
        total_in += llm.prompt_tokens
        total_out += llm.completion_tokens
        missing = _missing_required(raw, required)
        if missing:
            retry = _retry_user_prompt(user_prompt, missing)
            raw, llm2 = call_json(system, retry, tier="default", max_tokens=max_tok)
            total_in += llm2.prompt_tokens
            total_out += llm2.completion_tokens
            llm = llm2
        doc = _validate(raw, required)
    except ValueError as e:
        return DocGenResult(False, None, total_in, total_out, error=f"{type(e).__name__}: {e}")

    body_hash = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()
    doc_id = _persist_doc(
        repo_id=ent["repo_id"],
        entity_id=entity_id,
        depth=depth,
        doc=doc,
        model=llm.model,
        prompt_tokens=total_in,
        completion_tokens=total_out,
        verified=False,
        source_body_hash=body_hash,
    )
    return DocGenResult(True, doc_id, total_in, total_out)


async def agenerate_entity_doc(entity_id: UUID, *, run_verifier: bool = True) -> DocGenResult:
    """Async Pass-1 doc-gen + retry + (optional) verifier pass. Persists to DB."""
    ent = await asyncio.to_thread(_fetch_entity_data, entity_id)
    if not ent:
        return DocGenResult(False, None, 0, 0, error="entity not found")

    body = await asyncio.to_thread(
        _read_entity_body,
        ent["clone_path"] or "",
        ent["file_path"],
        ent["start_line"],
        ent["end_line"],
    )
    if not body:
        body = ent["signature"] or ent["name"]

    depth = _depth_tier_for(ent["repo_display_name"], ent["repo_priority"])
    required = _required_fields_for_tier(depth)

    user_prompt = _build_user_prompt(
        entity_kind=ent["kind"],
        qualified_name=ent["qualified_name"] or ent["name"],
        signature=ent["signature"] or "",
        body=body,
        file_path=ent["file_path"],
        repo_role=ent["repo_role"],
        repo_product_id=ent["repo_product_id"],
        business_concepts=list(ent["concepts"] or []),
        depth_tier=depth,
        required_fields=required,
    )

    max_tok = _max_tokens_for_tier(depth)
    system = _system_blocks()

    total_in = 0
    total_out = 0
    try:
        raw, llm = await acall_json(system, user_prompt, tier="default", max_tokens=max_tok)
        total_in += llm.prompt_tokens
        total_out += llm.completion_tokens
        missing = _missing_required(raw, required)
        if missing:
            retry = _retry_user_prompt(user_prompt, missing)
            raw, llm2 = await acall_json(system, retry, tier="default", max_tokens=max_tok)
            total_in += llm2.prompt_tokens
            total_out += llm2.completion_tokens
            llm = llm2
        doc = _validate(raw, required)
    except ValueError as e:
        return DocGenResult(False, None, total_in, total_out, error=f"{type(e).__name__}: {e}")

    verified_flag: bool | None = None
    if run_verifier:
        v_result, v_in, v_out = await averify_doc(body, doc)
        total_in += v_in
        total_out += v_out
        if v_result is not None:
            verified_flag = bool(v_result.is_faithful)

    body_hash = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()
    doc_id = await asyncio.to_thread(
        _persist_doc,
        repo_id=ent["repo_id"],
        entity_id=entity_id,
        depth=depth,
        doc=doc,
        model=llm.model,
        prompt_tokens=total_in,
        completion_tokens=total_out,
        verified=verified_flag,
        source_body_hash=body_hash,
    )
    return DocGenResult(True, doc_id, total_in, total_out)


# ---------------------------------------------------------------------------
# Bulk doc-gen for a repo (async-driven, bounded concurrency)
# ---------------------------------------------------------------------------


DocGenScope = str  # 'critical' | 'types' | 'all' | 'meaningful'
DEFAULT_DOC_GEN_CONCURRENCY = 5


@dataclass
class BulkDocGenResult:
    success: bool
    attempted: int
    succeeded: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    error: str | None = None


_POJO_TYPE_FILTER = """
    (e.kind IN ('class','interface','enum','record')
     AND (e.name LIKE '%%Bean'
          OR e.name LIKE '%%DTO' OR e.name LIKE '%%Dto'
          OR e.name LIKE '%%VO' OR e.name LIKE '%%Vo'
          OR e.name LIKE '%%Pojo'
          OR e.name LIKE '%%Request' OR e.name LIKE '%%Response'
          OR e.name LIKE '%%Entity'
          OR f.path ILIKE '%%/dto/%%' OR f.path ILIKE '%%/dtos/%%'
          OR f.path ILIKE '%%/beans/%%' OR f.path ILIKE '%%/vo/%%'
          OR f.path ILIKE '%%/model/%%'))
"""

_GETTER_SETTER_FILTER = """
    (e.kind IN ('method','constructor','ts_method')
     AND (e.name LIKE 'get%%' OR e.name LIKE 'set%%' OR e.name LIKE 'is%%'
          OR e.name IN ('equals','hashCode','toString','compareTo','clone')))
"""


# All atomic-unit kinds that doc-gen Pass 1 should target. Bible §6: every
# function, class, SQL query, XML entry, JRXML report, table gets its own doc.
# db_column is intentionally excluded (body is too short — covered in db_table doc).
_MEANINGFUL_KINDS = (
    # Java
    "class", "interface", "enum", "record", "method", "constructor",
    # TypeScript / Angular
    "ts_class", "ts_method", "ts_function", "ts_interface", "ts_type_alias",
    "ts_component", "ts_service", "ts_pipe", "ts_directive", "ts_module",
    # XML query catalogs and ETL service catalogs
    "sql_query", "xml_service",
    # Standalone SQL functions / stored procedures (core calc logic, e.g. Func_*)
    "sql_function", "sql_procedure", "sql_view", "sql_script",
    # JRXML JASPER reports
    "jrxml_report",
    # DDL (db_table; db_column omitted as too short for full doc)
    "db_table",
)


def _list_entities_for_scope(
    repo_id: UUID,
    scope: DocGenScope,
    repo_critical_entry_points: list[str],
) -> list[UUID]:
    if scope == "critical":
        return list_critical_entry_point_entities(repo_id, repo_critical_entry_points)

    if scope == "types":
        kinds = ("class", "interface", "enum", "record",
                 "ts_class", "ts_interface", "ts_type_alias",
                 "ts_component", "ts_service", "ts_pipe", "ts_directive", "ts_module",
                 "db_table", "jrxml_report")
        extra_exclude = ""
    elif scope == "all":
        kinds = _MEANINGFUL_KINDS
        extra_exclude = ""
    elif scope == "meaningful":
        kinds = _MEANINGFUL_KINDS
        extra_exclude = f"AND NOT ({_POJO_TYPE_FILTER} OR {_GETTER_SETTER_FILTER})"
    else:
        return []

    sql = f"""
        SELECT e.entity_id
          FROM entities e
          JOIN repo_files f ON f.file_id = e.file_id
          LEFT JOIN generated_docs d
                 ON d.entity_id = e.entity_id AND d.pass_level = 'entity'
         WHERE e.repo_id = %s
           AND e.kind = ANY(%s)
           AND f.path NOT ILIKE '%%/test/%%'
           AND f.path NOT ILIKE '%%/tests/%%'
           AND e.name NOT ILIKE '%%Tests'
           AND e.name NOT ILIKE '%%Test'
           AND d.doc_id IS NULL
           {extra_exclude}
         ORDER BY
            CASE WHEN e.kind IN ('class','interface','enum','record') THEN 0 ELSE 1 END,
            e.qualified_name
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (repo_id, list(kinds)))
        return [row[0] for row in cur.fetchall()]


def count_pending_docs(repo_id: UUID, scope: DocGenScope, repo_critical_entry_points: list[str]) -> int:
    return len(_list_entities_for_scope(repo_id, scope, repo_critical_entry_points))


async def _adoc_gen_repo(
    entity_ids: list[UUID],
    cb: Callable[[str], None],
    max_concurrency: int = DEFAULT_DOC_GEN_CONCURRENCY,
    *,
    run_verifier: bool = True,
) -> tuple[int, int, int, int]:
    sem = asyncio.Semaphore(max_concurrency)
    state = {"i": 0, "ok": 0, "fail": 0, "in": 0, "out": 0, "verified": 0}
    total = len(entity_ids)

    async def _one(eid: UUID) -> None:
        async with sem:
            try:
                r = await agenerate_entity_doc(eid, run_verifier=run_verifier)
            except Exception as e:
                r = DocGenResult(False, None, 0, 0, error=f"{type(e).__name__}: {e}")
        state["i"] += 1
        if r.success:
            state["ok"] += 1
            state["in"] += r.prompt_tokens
            state["out"] += r.completion_tokens
        else:
            state["fail"] += 1
        if state["i"] % 25 == 0 or state["i"] == total:
            cb(
                f"doc-gen {state['i']}/{total} · "
                f"ok={state['ok']} fail={state['fail']} · "
                f"tokens(in/out)={state['in']}/{state['out']}"
            )

    await asyncio.gather(*(_one(eid) for eid in entity_ids))
    return state["ok"], state["fail"], state["in"], state["out"]


def doc_gen_repo(
    repo_id: UUID,
    scope: DocGenScope,
    repo_critical_entry_points: list[str],
    on_progress: Callable[[str], None] | None = None,
    max_concurrency: int = DEFAULT_DOC_GEN_CONCURRENCY,
    *,
    run_verifier: bool = True,
) -> BulkDocGenResult:
    cb = on_progress or (lambda _msg: None)

    entity_ids = _list_entities_for_scope(repo_id, scope, repo_critical_entry_points)
    if not entity_ids:
        cb("nothing to doc-gen (already up to date)")
        return BulkDocGenResult(True, 0, 0, 0, 0, 0)

    cb(
        f"starting doc-gen — scope={scope}, entities={len(entity_ids)}, "
        f"concurrency={max_concurrency}, verifier={'on' if run_verifier else 'off'}"
    )
    run_id = start_run(
        repo_id, "doc_gen",
        notes=f"scope={scope}, n={len(entity_ids)}, conc={max_concurrency}, verifier={run_verifier}",
    )

    try:
        succeeded, failed, total_in, total_out = asyncio.run(
            _adoc_gen_repo(entity_ids, cb, max_concurrency, run_verifier=run_verifier)
        )
    except Exception as e:
        finish_run(
            run_id, "error",
            error_message=f"{type(e).__name__}: {e}",
            counts={"attempted": 0, "succeeded": 0, "failed": 0,
                    "prompt_tokens": 0, "completion_tokens": 0},
        )
        return BulkDocGenResult(False, 0, 0, 0, 0, 0, str(e))

    counts = {
        "attempted": len(entity_ids),
        "succeeded": succeeded,
        "failed": failed,
        "prompt_tokens": total_in,
        "completion_tokens": total_out,
    }
    finish_run(run_id, "success" if failed == 0 else "error", counts=counts)
    cb(f"done — ok={succeeded} fail={failed} tokens(in/out)={total_in}/{total_out}")

    return BulkDocGenResult(
        success=failed == 0,
        attempted=len(entity_ids),
        succeeded=succeeded,
        failed=failed,
        prompt_tokens=total_in,
        completion_tokens=total_out,
    )


# ---------------------------------------------------------------------------
# Helpers to find seed entities (critical entry points)
# ---------------------------------------------------------------------------


def list_critical_entry_point_entities(repo_id: UUID, repo_critical_entry_points: list[str]) -> list[UUID]:
    if not repo_critical_entry_points:
        return []
    with get_conn() as conn, conn.cursor() as cur:
        out: list[UUID] = []
        for hint in repo_critical_entry_points:
            stem = hint.replace(".java", "").replace(".xml", "")
            cur.execute(
                """
                SELECT DISTINCT e.entity_id
                  FROM entities e
                  JOIN repo_files f ON f.file_id = e.file_id
                 WHERE e.repo_id = %s
                   AND e.kind IN ('class','interface','enum','record')
                   AND f.path NOT ILIKE '%%/test/%%'
                   AND f.path NOT ILIKE '%%/tests/%%'
                   AND e.name NOT ILIKE '%%Tests'
                   AND e.name NOT ILIKE '%%Test'
                   AND (e.name = %s OR f.path ILIKE %s)
                 ORDER BY e.entity_id
                 LIMIT 5
                """,
                (
                    repo_id,
                    stem,
                    f"%{hint}%" if "/" in hint or "." in hint else f"%/{stem}.java",
                ),
            )
            out.extend(row[0] for row in cur.fetchall())
        seen: set[UUID] = set()
        unique: list[UUID] = []
        for eid in out:
            if eid not in seen:
                seen.add(eid)
                unique.append(eid)
        return unique
