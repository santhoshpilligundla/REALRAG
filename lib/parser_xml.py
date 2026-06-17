"""XML parser. Atomic-units extracted depend on the XML's purpose.

Detected file types (by filename / root-element heuristics):

  - SQL query catalog (sql.xml, *_queries.xml):
      every <entry key="X"> becomes one 'sql_query' entity.
      SQL body is sub-parsed with sqlglot for reads_table / writes_table facts.

  - ETL service catalog (etl2posql.xml, *etlPO*.xml):
      every <service name="X"> becomes one 'xml_service' entity.
      Embedded SELECT / INSERT / UPDATE SQL is sub-parsed.

  - Spring/Hibernate config (applicationContext.xml, etc.):
      every top-level <bean id="X">/<property name="X"> becomes one 'xml_bean' entity.
      Light-touch — just records the bean for cross-link.

  - Other XML:
      one 'xml_doc' entity for the whole file, kept for chunk + retrieval.

This module is product-agnostic. Detection is purely structural / file-name
based, no RMS-specific knowledge baked in.
"""

from __future__ import annotations

import re
from pathlib import Path

from lxml import etree

from lib.parser_sql import extract_sql_facts, to_facts
from lib.parsers_common import Fact, ParsedEntity


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _start_line(text: str, char_offset: int) -> int:
    return text.count("\n", 0, char_offset) + 1


_QUERY_CATALOG_HINTS = (
    "_queries.xml", "queries.xml", "sql.xml",
)


_ETL_SERVICE_HINTS = (
    "etl2posql.xml", "etltopo.xml", "etl2po.xml",
)


def _is_query_catalog(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _QUERY_CATALOG_HINTS)


def _is_etl_service(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in _ETL_SERVICE_HINTS)


_KEY_LINE_RE = re.compile(r'<entry[^>]*key="([^"]+)"', re.IGNORECASE)
_SERVICE_LINE_RE = re.compile(r'<service[^>]*name="([^"]+)"', re.IGNORECASE)


def _line_for_text_match(raw: str, pattern: re.Pattern, value: str) -> int:
    """Find the line where pattern's group(1) == value first appears. Falls back to 1."""
    for m in pattern.finditer(raw):
        if m.group(1) == value:
            return _start_line(raw, m.start())
    return 1


def _entity_text_lines(raw: str, start: int, end: int) -> str:
    lines = raw.splitlines()
    return "\n".join(lines[max(0, start - 1):end])


def _xml_root(raw: str) -> etree._Element | None:
    try:
        parser = etree.XMLParser(recover=True, ns_clean=True)
        root = etree.fromstring(raw.encode("utf-8", errors="replace"), parser=parser)
        return root
    except etree.XMLSyntaxError:
        return None


def _strip_ns(tag) -> str:
    """Tag may be non-string for lxml ProcessingInstruction/Comment/Entity nodes."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _collect_entries(root: etree._Element, target_tag: str) -> list[etree._Element]:
    return [
        el for el in root.iter()
        if isinstance(el.tag, str) and _strip_ns(el.tag) == target_tag
    ]


def parse_query_catalog(path: Path) -> list[ParsedEntity]:
    """Each `<entry key="X">` becomes one 'sql_query' entity."""
    raw = _read_text(path)
    root = _xml_root(raw)
    if root is None:
        return []

    entries: list[ParsedEntity] = []
    for el in _collect_entries(root, "entry"):
        key = el.get("key") or el.get("id")
        if not key:
            continue
        body_sql = (el.text or "").strip()
        if not body_sql:
            # Some catalogs nest CDATA differently; fall back to whole element text
            body_sql = etree.tostring(el, method="text", encoding="unicode").strip()

        start_line = _line_for_text_match(raw, _KEY_LINE_RE, key)
        # End-line is approximate: count lines in the rendered element
        rendered = etree.tostring(el, encoding="unicode")
        end_line = start_line + rendered.count("\n")

        signature = f'<entry key="{key}">'

        facts: list[Fact] = []
        sql_facts = extract_sql_facts(body_sql)
        facts.extend(to_facts(subject=key, sql_facts=sql_facts))

        entries.append(ParsedEntity(
            kind="sql_query",
            name=key,
            qualified_name=key,
            signature=signature,
            body=body_sql,
            start_line=start_line,
            end_line=end_line,
            facts=facts,
        ))
    return entries


def parse_etl_service_catalog(path: Path) -> list[ParsedEntity]:
    """Each `<service name="X">` becomes one 'xml_service' entity.

    Embedded SQL within child elements (e.g. <select>, <insert>, <delete>,
    <preprocess>, <postprocess>, <sql>) is concatenated and parsed for
    table reads/writes.
    """
    raw = _read_text(path)
    root = _xml_root(raw)
    if root is None:
        return []

    services: list[ParsedEntity] = []
    for el in _collect_entries(root, "service"):
        name = el.get("name") or el.get("id")
        if not name:
            continue

        # Concatenate text from all descendants — captures CDATA SQL too.
        body_text = etree.tostring(el, method="text", encoding="unicode").strip()
        rendered = etree.tostring(el, encoding="unicode")

        start_line = _line_for_text_match(raw, _SERVICE_LINE_RE, name)
        end_line = start_line + rendered.count("\n")

        signature = f'<service name="{name}">'

        facts: list[Fact] = []
        sql_facts = extract_sql_facts(body_text)
        facts.extend(to_facts(subject=name, sql_facts=sql_facts))

        services.append(ParsedEntity(
            kind="xml_service",
            name=name,
            qualified_name=name,
            signature=signature,
            body=rendered,
            start_line=start_line,
            end_line=end_line,
            facts=facts,
        ))
    return services


def parse_generic_xml(path: Path) -> list[ParsedEntity]:
    """Fallback: emit one 'xml_doc' entity for the whole file."""
    raw = _read_text(path)
    if not raw.strip():
        return []
    return [ParsedEntity(
        kind="xml_doc",
        name=path.name,
        qualified_name=path.name,
        signature=f"<xml file={path.name}>",
        body=raw[:32_000],
        start_line=1,
        end_line=raw.count("\n") + 1,
    )]


def parse_xml_file(path: Path) -> list[ParsedEntity]:
    """Top-level dispatcher.

    Detection is STRUCTURAL first (robust), then falls back to filename hints.
    Structural beats filenames because substring filename matching is fragile —
    e.g. 'etl2po**sql.xml**' contains 'sql.xml' and was wrongly treated as a query
    catalog, dropping all its <service> entries from the index.
    """
    name = path.name
    if name.endswith(".jrxml"):
        # JRXML has a dedicated parser; this module shouldn't be called for them.
        return []

    root = _xml_root(_read_text(path))
    if root is not None:
        n_service = len(_collect_entries(root, "service"))
        n_entry = len(_collect_entries(root, "entry"))
        if n_service and n_service >= n_entry:
            return parse_etl_service_catalog(path)
        if n_entry:
            return parse_query_catalog(path)

    # Fallback to filename hints (ETL checked before query catalog).
    if _is_etl_service(name):
        return parse_etl_service_catalog(path)
    if _is_query_catalog(name):
        return parse_query_catalog(path)
    return parse_generic_xml(path)
