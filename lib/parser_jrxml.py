"""JRXML (JasperReports XML) parser. Each `.jrxml` becomes one 'jrxml_report' entity.

Extracted facts:
  - parameters (name, type)
  - fields (name, type)
  - groups
  - sub-reports referenced
  - dataset SQL (queryString) sub-parsed for reads_table facts
"""

from __future__ import annotations

from pathlib import Path

from lxml import etree

from lib.parser_sql import extract_sql_facts, to_facts
from lib.parsers_common import Fact, ParsedEntity


def _strip_ns(tag) -> str:
    """Tag may be non-string for lxml ProcessingInstruction/Comment/Entity nodes."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def parse_jrxml_file(path: Path) -> list[ParsedEntity]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not raw.strip():
        return []

    try:
        parser = etree.XMLParser(recover=True, ns_clean=True)
        root = etree.fromstring(raw.encode("utf-8", errors="replace"), parser=parser)
    except etree.XMLSyntaxError:
        return []

    name = root.get("name") or path.stem

    parameters: list[str] = []
    fields: list[str] = []
    groups: list[str] = []
    subreports: list[str] = []
    query_string = ""

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = _strip_ns(el.tag)
        if tag == "parameter":
            pname = el.get("name")
            ptype = el.get("class") or el.get("type")
            if pname:
                parameters.append(f"{pname}:{ptype}" if ptype else pname)
        elif tag == "field":
            fname = el.get("name")
            ftype = el.get("class") or el.get("type")
            if fname:
                fields.append(f"{fname}:{ftype}" if ftype else fname)
        elif tag == "group":
            gname = el.get("name")
            if gname:
                groups.append(gname)
        elif tag == "subreport":
            for child in el.iter():
                if _strip_ns(child.tag) == "subreportExpression" and child.text:
                    subreports.append(child.text.strip())
                    break
        elif tag == "queryString" and el.text:
            query_string = el.text.strip()

    facts: list[Fact] = []
    if query_string:
        sql_facts = extract_sql_facts(query_string)
        facts.extend(to_facts(subject=name, sql_facts=sql_facts))
    for sr in subreports:
        facts.append(Fact(subject=name, predicate="includes_subreport", object=sr, confidence=0.9))

    body_lines = [
        f"JRXML report: {name}",
        f"file: {path.name}",
    ]
    if parameters:
        body_lines.append("parameters: " + ", ".join(parameters))
    if fields:
        body_lines.append("fields: " + ", ".join(fields[:30]))
    if groups:
        body_lines.append("groups: " + ", ".join(groups))
    if subreports:
        body_lines.append("sub-reports: " + ", ".join(subreports))
    if query_string:
        body_lines.append("\nqueryString:")
        body_lines.append(query_string)
    body = "\n".join(body_lines)

    return [ParsedEntity(
        kind="jrxml_report",
        name=name,
        qualified_name=path.stem,
        signature=f"jrxml({name})",
        body=body[:32_000],
        start_line=1,
        end_line=raw.count("\n") + 1,
        facts=facts,
    )]
