"""Markdown parser. Splits files into top-level sections.

Each top-level heading (# or ##) becomes one 'markdown_section' entity. Files
without headings produce a single 'markdown_doc' entity.

These land in `doc_chunks` for prose retrieval, separate from `code_chunks`.
"""

from __future__ import annotations

import re
from pathlib import Path

from lib.parsers_common import ParsedEntity


_H1_OR_H2 = re.compile(r"^(#{1,2})\s+(.+?)\s*$", re.MULTILINE)


def parse_markdown_file(path: Path) -> list[ParsedEntity]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if not raw.strip():
        return []

    matches = list(_H1_OR_H2.finditer(raw))
    if not matches:
        return [ParsedEntity(
            kind="markdown_doc",
            name=path.stem,
            qualified_name=path.stem,
            signature=f"# {path.stem}",
            body=raw[:32_000],
            start_line=1,
            end_line=raw.count("\n") + 1,
        )]

    out: list[ParsedEntity] = []
    for i, m in enumerate(matches):
        title = m.group(2).strip()
        section_start = m.start()
        section_end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[section_start:section_end]
        start_line = raw.count("\n", 0, section_start) + 1
        end_line = raw.count("\n", 0, section_end) + 1
        out.append(ParsedEntity(
            kind="markdown_section",
            name=title,
            qualified_name=f"{path.stem}::{title}",
            signature=m.group(0).strip(),
            body=body[:32_000],
            start_line=start_line,
            end_line=end_line,
        ))
    return out
