"""Cross-repo edge discovery from a hand-curated pattern catalog (bible §6 + scope §10).

Reads `data/patterns_*.yaml`, runs each pattern's extractors over the parsed
corpus (entities + facts + code chunks), and populates cross_repo_edges.

Generic across products — patterns are data, not code. New TFS codebases ship
their own patterns_<product>.yaml and the engine picks them up automatically.

Extractor types (resolver functions):
  - ts_string_literal_near_key  — regex on TS source for `key: 'X'` patterns
  - xml_entry_key               — entities with kind='sql_query' (XML <entry key="X">)
  - regex_in_body               — generic body-text regex with capture group
  - db_table_name               — entities with kind='db_table'
  - jrxml_report_name           — entities with kind='jrxml_report'
  - facts_predicate             — entities whose facts contain a given predicate
  - java_request_mapping        — Java methods/classes with @RequestMapping(...)
  - db_table_in_schema          — db_table entities sourced from a specific schema file
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import UUID

import yaml

from lib.db import get_conn
from lib.runs_repo import finish_run, start_run


PATTERNS_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass
class FromMatch:
    entity_id: UUID
    repo_id: UUID
    name: str       # the captured key
    qualified_name: str
    kind: str
    extra: dict | None = None


@dataclass
class ToMatch:
    entity_id: UUID
    repo_id: UUID
    name: str
    qualified_name: str
    kind: str


@dataclass
class EdgeBuildResult:
    success: bool
    patterns_run: int
    edges_created: int
    error: str | None = None


# ---------------------------------------------------------------------------
# Pattern catalog loader
# ---------------------------------------------------------------------------


def load_patterns() -> list[dict]:
    out: list[dict] = []
    for path in sorted(PATTERNS_DIR.glob("patterns_*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue
        if not isinstance(data, dict):
            continue
        for p in data.get("patterns") or []:
            p["_source_file"] = path.name
            p["_product_hint"] = data.get("product")
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Extractors — FROM side: produce list of FromMatch with captured `name` (the key)
# ---------------------------------------------------------------------------


_TS_KEY_RE_CACHE: dict[str, re.Pattern] = {}


def _build_ts_key_regex(keys: list[str]) -> re.Pattern:
    """Build a regex that captures string literals near any of the given keys.

    Matches patterns like:
        query: 'getFoo'
        queryName: "barName"
        { queryId: 'baz' }
    """
    cache_key = "|".join(sorted(keys))
    if cache_key in _TS_KEY_RE_CACHE:
        return _TS_KEY_RE_CACHE[cache_key]
    alt = "|".join(re.escape(k.rstrip(":")) for k in keys)
    # (key)\s*:\s*(['"])(captured)(['"])
    pat = re.compile(rf"\b({alt})\s*:\s*['\"]([^'\"]+)['\"]")
    _TS_KEY_RE_CACHE[cache_key] = pat
    return pat


def _extract_from_ts_string_literal(spec: dict, repo_role_filter: str | None) -> list[FromMatch]:
    """Scan TS code chunks for `key: 'X'` literals."""
    keys = spec.get("keys") or []
    if not keys:
        return []
    pattern = _build_ts_key_regex(keys)

    sql = """
        SELECT e.entity_id, e.repo_id, e.qualified_name, e.kind, cc.content
          FROM entities e
          JOIN repo_files f ON f.file_id = e.file_id
          JOIN repos r      ON r.repo_id = e.repo_id
          LEFT JOIN code_chunks cc ON cc.entity_id = e.entity_id
         WHERE e.kind LIKE 'ts_%%'
    """
    params: tuple = ()
    if repo_role_filter:
        sql += " AND r.repo_role = %s"
        params = (repo_role_filter,)

    out: list[FromMatch] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for entity_id, repo_id, qname, kind, content in cur.fetchall():
            if not content:
                continue
            for m in pattern.finditer(content):
                captured = m.group(2)
                out.append(FromMatch(
                    entity_id=entity_id, repo_id=repo_id, kind=kind,
                    qualified_name=qname or "", name=captured,
                ))
    return out


def _extract_from_regex_in_body(spec: dict, repo_role_filter: str | None) -> list[FromMatch]:
    pat_str = spec.get("pattern")
    if not pat_str:
        return []
    pattern = re.compile(pat_str)
    kinds = spec.get("kinds") or []

    sql = """
        SELECT e.entity_id, e.repo_id, e.qualified_name, e.kind, cc.content
          FROM entities e
          LEFT JOIN code_chunks cc ON cc.entity_id = e.entity_id
          JOIN repos r ON r.repo_id = e.repo_id
         WHERE 1=1
    """
    params: list = []
    if kinds:
        sql += " AND e.kind = ANY(%s)"
        params.append(list(kinds))
    if repo_role_filter:
        sql += " AND r.repo_role = %s"
        params.append(repo_role_filter)

    out: list[FromMatch] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        for entity_id, repo_id, qname, kind, content in cur.fetchall():
            if not content:
                continue
            for m in pattern.finditer(content):
                # Use first non-None capture group
                captured = next((g for g in m.groups() if g), None)
                if captured is None:
                    continue
                out.append(FromMatch(
                    entity_id=entity_id, repo_id=repo_id, kind=kind,
                    qualified_name=qname or "", name=captured,
                ))
    return out


def _extract_from_facts_predicate(spec: dict, _repo_role_filter: str | None) -> list[FromMatch]:
    predicate = spec.get("predicate")
    if not predicate:
        return []
    from_kinds = spec.get("from_kinds") or []

    sql = """
        SELECT DISTINCT fa.entity_id, e.repo_id, e.qualified_name, e.kind, fa.object
          FROM facts fa
          JOIN entities e ON e.entity_id = fa.entity_id
         WHERE fa.predicate = %s
    """
    params: list = [predicate]
    if from_kinds:
        sql += " AND e.kind = ANY(%s)"
        params.append(list(from_kinds))

    out: list[FromMatch] = []
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        for entity_id, repo_id, qname, kind, obj in cur.fetchall():
            out.append(FromMatch(
                entity_id=entity_id, repo_id=repo_id, kind=kind,
                qualified_name=qname or "", name=obj,
            ))
    return out


# ---------------------------------------------------------------------------
# Extractors — TO side: build a lookup name → list[ToMatch]
# ---------------------------------------------------------------------------


def _index_xml_entry_keys(filename_glob: str | None, repo_role_filter: str | None) -> dict[str, list[ToMatch]]:
    sql = """
        SELECT e.entity_id, e.repo_id, e.name, e.qualified_name, e.kind, f.path
          FROM entities e
          JOIN repo_files f ON f.file_id = e.file_id
          JOIN repos r ON r.repo_id = e.repo_id
         WHERE e.kind = 'sql_query'
    """
    params: list = []
    if repo_role_filter:
        sql += " AND r.repo_role = %s"
        params.append(repo_role_filter)

    out: dict[str, list[ToMatch]] = {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        for entity_id, repo_id, name, qname, kind, fpath in cur.fetchall():
            if filename_glob:
                # Crude glob check: supports leading * only.
                pat = filename_glob.lstrip("*")
                if not fpath.endswith(pat) and pat not in fpath:
                    continue
            out.setdefault(name, []).append(ToMatch(
                entity_id=entity_id, repo_id=repo_id, kind=kind,
                name=name, qualified_name=qname or "",
            ))
    return out


def _index_db_table_names(schema_filter: str | None = None) -> dict[str, list[ToMatch]]:
    sql = """
        SELECT e.entity_id, e.repo_id, e.name, e.qualified_name, e.kind, f.path
          FROM entities e
          JOIN repo_files f ON f.file_id = e.file_id
         WHERE e.kind = 'db_table'
    """
    out: dict[str, list[ToMatch]] = {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for entity_id, repo_id, name, qname, kind, fpath in cur.fetchall():
            if schema_filter and schema_filter.lower() not in fpath.lower():
                continue
            out.setdefault(name.lower(), []).append(ToMatch(
                entity_id=entity_id, repo_id=repo_id, kind=kind,
                name=name, qualified_name=qname or "",
            ))
    return out


def _index_jrxml_report_names() -> dict[str, list[ToMatch]]:
    out: dict[str, list[ToMatch]] = {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT entity_id, repo_id, name, qualified_name, kind
              FROM entities WHERE kind = 'jrxml_report'
            """
        )
        for entity_id, repo_id, name, qname, kind in cur.fetchall():
            for key in {name, qname}:
                if key:
                    out.setdefault(key.lower(), []).append(ToMatch(
                        entity_id=entity_id, repo_id=repo_id, kind=kind,
                        name=name, qualified_name=qname or "",
                    ))
    return out


_REQUEST_MAPPING_RE = re.compile(r'@RequestMapping\s*\([^)]*?["\'](/[^"\']*)["\']')


def _index_java_request_mappings() -> dict[str, list[ToMatch]]:
    out: dict[str, list[ToMatch]] = {}
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT e.entity_id, e.repo_id, e.name, e.qualified_name, e.kind, cc.content
              FROM entities e
              LEFT JOIN code_chunks cc ON cc.entity_id = e.entity_id
             WHERE e.kind IN ('class', 'method')
            """
        )
        for entity_id, repo_id, name, qname, kind, content in cur.fetchall():
            if not content:
                continue
            for m in _REQUEST_MAPPING_RE.finditer(content):
                path = m.group(1)
                out.setdefault(path, []).append(ToMatch(
                    entity_id=entity_id, repo_id=repo_id, kind=kind,
                    name=name, qualified_name=qname or "",
                ))
    return out


# ---------------------------------------------------------------------------
# Pattern runner
# ---------------------------------------------------------------------------


def _persist_edges(rows: list[dict]) -> int:
    if not rows:
        return 0
    with get_conn() as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO cross_repo_edges
                  (from_entity_id, to_entity_id, from_repo_id, to_repo_id,
                   kind, confidence, discovered_via)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (r["from_entity_id"], r["to_entity_id"], r["from_repo_id"],
                 r["to_repo_id"], r["kind"], r["confidence"], r["discovered_via"]),
            )
        conn.commit()
    return len(rows)


def _run_pattern(p: dict) -> int:
    name = p.get("name", "<unnamed>")
    edge_kind = p.get("edge", name)
    confidence = float(p.get("confidence", 1.0))

    from_spec = p.get("from") or {}
    to_spec = p.get("to") or {}
    from_extractor = from_spec.get("extractor")
    to_extractor = to_spec.get("extractor")

    if from_extractor == "ts_string_literal_near_key":
        froms = _extract_from_ts_string_literal(from_spec, from_spec.get("repo_role"))
    elif from_extractor == "regex_in_body":
        froms = _extract_from_regex_in_body(from_spec, from_spec.get("repo_role"))
    elif from_extractor == "facts_predicate":
        froms = _extract_from_facts_predicate(from_spec, from_spec.get("repo_role"))
    else:
        return 0

    if to_extractor == "xml_entry_key":
        to_index = _index_xml_entry_keys(to_spec.get("filename_glob"), to_spec.get("repo_role"))
        normalize = lambda s: s
    elif to_extractor == "db_table_name":
        to_index = _index_db_table_names()
        normalize = lambda s: s.lower()
    elif to_extractor == "db_table_in_schema":
        to_index = _index_db_table_names(schema_filter=to_spec.get("schema"))
        normalize = lambda s: s.lower()
    elif to_extractor == "jrxml_report_name":
        to_index = _index_jrxml_report_names()
        normalize = lambda s: s.lower()
    elif to_extractor == "java_request_mapping":
        to_index = _index_java_request_mappings()
        normalize = lambda s: s
    else:
        return 0

    rows: list[dict] = []
    seen: set[tuple[UUID, UUID, str]] = set()  # de-dup
    for fm in froms:
        key = normalize(fm.name)
        targets = to_index.get(key) or []
        for tm in targets:
            if fm.repo_id == tm.repo_id and fm.entity_id == tm.entity_id:
                continue  # don't link an entity to itself
            dedup_key = (fm.entity_id, tm.entity_id, edge_kind)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            rows.append({
                "from_entity_id": fm.entity_id,
                "to_entity_id":   tm.entity_id,
                "from_repo_id":   fm.repo_id,
                "to_repo_id":     tm.repo_id,
                "kind":           edge_kind,
                "confidence":     confidence,
                "discovered_via": f"pattern:{name}",
            })

    return _persist_edges(rows)


# ---------------------------------------------------------------------------
# Public entry — drive from UI button or CLI
# ---------------------------------------------------------------------------


def discover_edges(on_progress: Callable[[str], None] | None = None) -> EdgeBuildResult:
    cb = on_progress or (lambda _msg: None)
    patterns = load_patterns()
    if not patterns:
        cb("no pattern catalog files found under data/patterns_*.yaml")
        return EdgeBuildResult(True, 0, 0)

    cb(f"loaded {len(patterns)} patterns from catalog")
    run_id = start_run(None, "cross_repo_edges", notes=f"patterns={len(patterns)}")

    total_edges = 0
    try:
        # Fresh build: clear previous edges so re-running is idempotent.
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM cross_repo_edges")
            conn.commit()

        for i, p in enumerate(patterns, 1):
            n = _run_pattern(p)
            total_edges += n
            cb(f"[{i}/{len(patterns)}] {p.get('name')}: {n} edges (cumulative {total_edges})")
    except Exception as e:
        finish_run(run_id, "error",
                   error_message=f"{type(e).__name__}: {e}",
                   counts={"patterns_run": 0, "edges_created": total_edges})
        return EdgeBuildResult(False, 0, total_edges, str(e))

    finish_run(run_id, "success",
               counts={"patterns_run": len(patterns), "edges_created": total_edges})
    cb(f"done — {total_edges} edges across {len(patterns)} patterns")
    return EdgeBuildResult(True, len(patterns), total_edges)


def count_edges_by_kind() -> dict[str, int]:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, COUNT(*) FROM cross_repo_edges GROUP BY kind ORDER BY COUNT(*) DESC"
        )
        return {k: n for (k, n) in cur.fetchall()}
