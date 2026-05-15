"""Common parser output shapes.

Every parser produces the same `ParsedEntity` records plus optional `Fact` triples
for the knowledge graph. The chunker writes them all to the same DB tables
(entities, code_chunks, facts).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass
class Fact:
    """Subject-predicate-object triple. Bible §8 facts table.

    predicate examples: 'reads_table', 'writes_table', 'calls_named_query',
    'launches_report', 'uses_config_key', 'extends', 'implements', 'has_table'.
    """
    subject: str
    predicate: str
    object: str
    confidence: float = 1.0


@dataclass
class ParsedEntity:
    kind: str                          # 'class','method','sql_query','xml_service','jrxml_report','db_table','db_column','config_key','ts_component','ts_service','ts_pipe','ts_directive','ts_class','ts_function','ts_interface','ts_type_alias','markdown_section','xml_entry'
    name: str
    qualified_name: str
    signature: str
    body: str
    start_line: int
    end_line: int
    children: list["ParsedEntity"] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)

    @property
    def body_hash(self) -> str:
        return hashlib.sha1(self.body.encode("utf-8", errors="replace")).hexdigest()


def flatten(entities: list[ParsedEntity]) -> list[ParsedEntity]:
    out: list[ParsedEntity] = []
    stack = list(entities)
    while stack:
        e = stack.pop()
        out.append(e)
        stack.extend(e.children)
    return out
