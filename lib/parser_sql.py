"""sqlglot-based SQL fact extractor.

Used by parser_xml (SQL inside <entry>/<service>), parser_jrxml (queryString),
parser_ddl (CREATE TABLE), and parser_chunker for standalone .sql files.

Extracts table reads/writes and column references.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import expressions as ex

from lib.parsers_common import Fact


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


def to_facts(subject: str, sql_facts: SqlFacts) -> list[Fact]:
    """Convert SqlFacts to Fact triples keyed by `subject` (the entity qualified_name)."""
    facts: list[Fact] = []
    for t in sql_facts.tables_read:
        facts.append(Fact(subject=subject, predicate="reads_table", object=t))
    for t in sql_facts.tables_written:
        facts.append(Fact(subject=subject, predicate="writes_table", object=t))
    return facts
