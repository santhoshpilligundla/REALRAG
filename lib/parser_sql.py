"""sqlglot-based SQL fact extractor.

Used by parser_xml (SQL inside <entry>/<service>), parser_jrxml (queryString),
parser_ddl (CREATE TABLE), and parser_chunker for standalone .sql files.

Extracts table reads/writes and column references.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import sqlglot
from sqlglot import expressions as ex

from lib.parsers_common import Fact, ParsedEntity


@dataclass
class SqlFacts:
    tables_read: list[str] = field(default_factory=list)
    tables_written: list[str] = field(default_factory=list)
    columns_referenced: list[str] = field(default_factory=list)
    statements: int = 0
    error: str | None = None


def _table_name(node: ex.Expression) -> str | None:
    if isinstance(node, ex.Table):
        return node.name
    return None


def extract_sql_facts(sql: str, dialect: str | None = None) -> SqlFacts:
    """Run sqlglot over an SQL string and return read/write/column facts.

    Resilient: returns an `error` string but never raises.
    """
    out = SqlFacts()
    if not sql or not sql.strip():
        return out

    try:
        statements = sqlglot.parse(sql, dialect=dialect, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception as e:  # noqa: BLE001 - sqlglot has many exception types
        out.error = f"sqlglot parse failed: {type(e).__name__}: {str(e)[:200]}"
        return out

    seen_read: set[str] = set()
    seen_write: set[str] = set()
    seen_col: set[str] = set()

    for stmt in statements:
        if stmt is None:
            continue
        out.statements += 1

        # Tables in INSERT/UPDATE/DELETE/MERGE are writes; in SELECT/USING are reads.
        for node in stmt.walk():
            if isinstance(node, (ex.Insert, ex.Update, ex.Delete, ex.Merge)):
                target = node.find(ex.Table)
                if target is not None:
                    name = target.name
                    if name and name.lower() not in seen_write:
                        seen_write.add(name.lower())
                        out.tables_written.append(name)
            elif isinstance(node, ex.Table):
                name = node.name
                if not name:
                    continue
                # Skip tables we've already classified as written
                if name.lower() in seen_write:
                    continue
                if name.lower() not in seen_read:
                    seen_read.add(name.lower())
                    out.tables_read.append(name)

        for col in stmt.find_all(ex.Column):
            cname = col.name
            if cname and cname.lower() not in seen_col:
                seen_col.add(cname.lower())
                out.columns_referenced.append(cname)

    return out


_CREATE_RE = re.compile(
    r'\bCREATE\s+(?:OR\s+REPLACE\s+)?(FUNCTION|PROCEDURE|PROC|VIEW|TRIGGER)\s+'
    r'(?:IF\s+NOT\s+EXISTS\s+)?([\w".\[\]]+)',   # captures "schema"."name" / [s].[n] / s.n / n
    re.IGNORECASE,
)


def _clean_name(raw_name: str) -> str:
    """Strip quotes/brackets and take the last dotted segment ('"public"."mkteffmax"' -> 'mkteffmax')."""
    cleaned = re.sub(r'["\[\]]', "", raw_name)
    return cleaned.split(".")[-1] or cleaned
_KIND = {"FUNCTION": "sql_function", "PROCEDURE": "sql_procedure", "PROC": "sql_procedure",
         "VIEW": "sql_view", "TRIGGER": "sql_trigger"}


def parse_sql_file(path: Path) -> list[ParsedEntity]:
    """Parse a standalone .sql file into entities — one per CREATE
    FUNCTION/PROCEDURE/VIEW/TRIGGER (these hold core calc logic, e.g. the
    forecasting Func_* files). Falls back to one 'sql_script' entity for the
    whole file when no CREATE is found. Non-DDL SQL was previously dropped.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not raw.strip():
        return []

    matches = list(_CREATE_RE.finditer(raw))
    out: list[ParsedEntity] = []
    if matches:
        for i, m in enumerate(matches):
            kind = _KIND.get(m.group(1).upper(), "sql_object")
            name = _clean_name(m.group(2))
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
            body = raw[start:end].strip()
            start_line = raw.count("\n", 0, start) + 1
            out.append(ParsedEntity(
                kind=kind, name=name, qualified_name=name,
                signature=f"{m.group(1).upper()} {name}",
                body=body, start_line=start_line,
                end_line=start_line + body.count("\n"),
                facts=to_facts(name, extract_sql_facts(body)),
            ))
    else:
        name = path.stem
        out.append(ParsedEntity(
            kind="sql_script", name=name, qualified_name=name,
            signature=f"SQL {path.name}", body=raw[:32_000],
            start_line=1, end_line=raw.count("\n") + 1,
            facts=to_facts(name, extract_sql_facts(raw)),
        ))
    return out


def to_facts(subject: str, sql_facts: SqlFacts) -> list[Fact]:
    """Convert SqlFacts to Fact triples keyed by `subject` (the entity qualified_name)."""
    facts: list[Fact] = []
    for t in sql_facts.tables_read:
        facts.append(Fact(subject=subject, predicate="reads_table", object=t))
    for t in sql_facts.tables_written:
        facts.append(Fact(subject=subject, predicate="writes_table", object=t))
    return facts
