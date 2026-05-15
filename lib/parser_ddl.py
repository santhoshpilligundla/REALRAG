"""DDL parser for SQL schema files (POConfigSchema.sql, POLogSchema.sql, pg_dump output).

Each CREATE TABLE produces:
  - one 'db_table' entity
  - one child 'db_column' entity per column

Each CREATE TABLE produces a Fact triple:
  (table_name, has_column, column_name)
"""

from __future__ import annotations

from pathlib import Path

import sqlglot
from sqlglot import expressions as ex

from lib.parsers_common import Fact, ParsedEntity


def _column_signature(col: ex.ColumnDef) -> str:
    name = col.name
    typ = col.args.get("kind")
    type_str = typ.sql() if typ is not None else "?"
    constraints = []
    if col.args.get("constraints"):
        for c in col.args["constraints"]:
            constraints.append(c.sql())
    suffix = (" " + " ".join(constraints)) if constraints else ""
    return f"{name} {type_str}{suffix}"[:240]


def parse_ddl_file(path: Path) -> list[ParsedEntity]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not raw.strip():
        return []

    try:
        statements = sqlglot.parse(raw, error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        return []

    text_lines = raw.splitlines()
    out: list[ParsedEntity] = []

    for stmt in statements:
        if stmt is None:
            continue
        if not isinstance(stmt, ex.Create):
            continue
        kind = (stmt.args.get("kind") or "").upper()
        if kind != "TABLE":
            continue

        table_node = stmt.find(ex.Table)
        if table_node is None:
            continue
        table_name = table_node.name
        if not table_name:
            continue

        # Approximate line range from sqlglot meta if available; else rendered text.
        rendered = stmt.sql()
        # Try to find first occurrence of CREATE TABLE table_name in raw
        needle = f"CREATE TABLE"
        idx = raw.upper().find(needle)
        # Reset for THIS table's name
        for off in range(idx if idx >= 0 else 0, len(raw)):
            sub = raw[off:off + 60].upper()
            if "CREATE TABLE" in sub and table_name.upper() in raw[off:off + 200].upper():
                start_line = raw.count("\n", 0, off) + 1
                break
        else:
            start_line = 1
        end_line = start_line + rendered.count("\n")

        col_signatures: list[str] = []
        children: list[ParsedEntity] = []
        facts: list[Fact] = [Fact(subject=table_name, predicate="defined_in_file", object=path.name)]

        schema_node = stmt.find(ex.Schema)
        if schema_node is not None:
            for col in schema_node.find_all(ex.ColumnDef):
                cname = col.name
                if not cname:
                    continue
                csig = _column_signature(col)
                col_signatures.append(csig)
                child = ParsedEntity(
                    kind="db_column",
                    name=cname,
                    qualified_name=f"{table_name}.{cname}",
                    signature=csig,
                    body=csig,
                    start_line=start_line,
                    end_line=start_line,
                    facts=[Fact(subject=table_name, predicate="has_column", object=cname)],
                )
                children.append(child)
                facts.append(Fact(subject=table_name, predicate="has_column", object=cname))

        body = rendered[:32_000]
        out.append(ParsedEntity(
            kind="db_table",
            name=table_name,
            qualified_name=table_name,
            signature=f"TABLE {table_name} ({len(col_signatures)} cols)",
            body=body,
            start_line=start_line,
            end_line=end_line,
            children=children,
            facts=facts,
        ))

    return out


def looks_like_ddl(path: Path) -> bool:
    """Heuristic: is this .sql file a schema dump rather than ad-hoc SQL?"""
    try:
        sample = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    upper = sample.upper()
    return "CREATE TABLE" in upper
