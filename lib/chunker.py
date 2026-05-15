"""Walk + parse + chunk orchestrator. One repo at a time.

Generic across languages: dispatches to the right parser by file extension /
filename pattern. All parsers produce `ParsedEntity` records, which are
written to the same DB tables (entities, code_chunks, doc_chunks, facts).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import UUID

from lib.db import get_conn
from lib.models import Repo
from lib.parser_ddl import looks_like_ddl, parse_ddl_file
from lib.parser_java import flatten as java_flatten
from lib.parser_java import parse_java_file
from lib.parser_jrxml import parse_jrxml_file
from lib.parser_markdown import parse_markdown_file
from lib.parser_typescript import parse_typescript_file
from lib.parser_xml import parse_xml_file
from lib.parsers_common import Fact, ParsedEntity
from lib.parsers_common import flatten as flat
from lib.repos_repo import set_repo_status
from lib.runs_repo import finish_run, start_run
from lib.walker import FileInfo, walk_repo


ProgressFn = Callable[[str], None]


@dataclass
class ChunkResult:
    success: bool
    files_seen: int
    files_parsed: int
    entities: int
    chunks: int
    facts: int
    error: str | None = None


def _noop(_msg: str) -> None:
    pass


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _wipe_repo_pipeline_data(repo_id: UUID) -> None:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM facts             WHERE repo_id = %s", (repo_id,))
        cur.execute("DELETE FROM doc_chunks        WHERE repo_id = %s", (repo_id,))
        cur.execute("DELETE FROM code_chunks       WHERE repo_id = %s", (repo_id,))
        cur.execute("DELETE FROM entities          WHERE repo_id = %s", (repo_id,))
        cur.execute("DELETE FROM repo_files        WHERE repo_id = %s", (repo_id,))
        # cross_repo_edges and dependencies cascade via ON DELETE on entities.
        # generated_docs/examples cascade similarly. Atomic since this is one tx
        # within the connection's autocommit-off block.
        conn.commit()


def _insert_files_batch(repo_id: UUID, files: list[FileInfo]) -> dict[str, UUID]:
    if not files:
        return {}
    with get_conn() as conn, conn.cursor() as cur:
        rows = [(repo_id, f.rel_path, f.language, f.size_bytes) for f in files]
        with cur.copy(
            "COPY repo_files (repo_id, path, language, size_bytes) FROM STDIN"
        ) as cp:
            for row in rows:
                cp.write_row(row)
        conn.commit()
        cur.execute("SELECT path, file_id FROM repo_files WHERE repo_id = %s", (repo_id,))
        return {path: fid for (path, fid) in cur.fetchall()}


def _mark_files_parsed(file_ids: list[UUID]) -> None:
    if not file_ids:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE repo_files SET parsed = true WHERE file_id = ANY(%s)",
            (file_ids,),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Parser dispatch
# ---------------------------------------------------------------------------


def _java_to_common(java_entities) -> list[ParsedEntity]:
    """Convert JavaEntity records (legacy) to ParsedEntity (common shape)."""
    out: list[ParsedEntity] = []
    for je in java_entities:
        pe = ParsedEntity(
            kind=je.kind,
            name=je.name,
            qualified_name=je.qualified_name,
            signature=je.signature,
            body=je.body,
            start_line=je.start_line,
            end_line=je.end_line,
            children=_java_to_common(je.children),
        )
        out.append(pe)
    return out


def _parse_dispatch(file_info: FileInfo) -> list[ParsedEntity]:
    """Pick the right parser by language + filename pattern. Returns top-level entities."""
    lang = file_info.language
    path = file_info.abs_path

    if lang == "java":
        java_entities = parse_java_file(path)
        return _java_to_common(java_entities)

    if lang == "typescript":
        return parse_typescript_file(path)

    if lang == "xml":
        # JRXML is XML by extension but has a dedicated parser.
        if path.suffix.lower() == ".jrxml":
            return parse_jrxml_file(path)
        return parse_xml_file(path)

    if lang == "sql":
        # Distinguish DDL schema dumps from ad-hoc SQL files.
        if looks_like_ddl(path):
            return parse_ddl_file(path)
        return []

    if lang == "markdown":
        return parse_markdown_file(path)

    return []


def _is_doc_chunk_kind(kind: str) -> bool:
    return kind in {"markdown_section", "markdown_doc"}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_entities_recursive(cur, repo_id: UUID, file_id: UUID, entities: list[ParsedEntity]) -> int:
    """Insert entities recursively, wiring parent_entity_id. Returns count."""
    count = 0
    stack: list[tuple[ParsedEntity, UUID | None]] = [(e, None) for e in entities]
    while stack:
        e, parent_id = stack.pop()
        cur.execute(
            """
            INSERT INTO entities
              (repo_id, file_id, parent_entity_id, kind, name, qualified_name,
               signature, body_hash, start_line, end_line)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING entity_id
            """,
            (repo_id, file_id, parent_id, e.kind, e.name, e.qualified_name,
             e.signature, e.body_hash, e.start_line, e.end_line),
        )
        new_id = cur.fetchone()[0]
        e._db_id = new_id  # type: ignore[attr-defined] - stamp for fact-writing
        count += 1
        for child in e.children:
            stack.append((child, new_id))
    return count


def _persist_chunks_and_facts(
    cur,
    repo_id: UUID,
    file_id: UUID,
    entities: list[ParsedEntity],
    file_language: str,
) -> tuple[int, int, int]:
    """For each entity, write a code_chunk OR doc_chunk and any facts. Returns (code, doc, facts)."""
    code = 0
    doc = 0
    facts_count = 0

    for e in flat(entities):
        eid = getattr(e, "_db_id", None)
        if eid is None:
            continue
        is_doc = _is_doc_chunk_kind(e.kind)

        # Decide whether to write a chunk for this entity.
        # Chunkable: methods, constructors, ts methods, sql_query, xml_service, jrxml_report,
        #            ts_class/ts_function/ts_interface, db_table, markdown sections, xml entries.
        # Non-chunkable: db_column (too small; embedded with parent body context).
        if e.kind == "db_column":
            # Still write its facts (has_column triples) but no chunk.
            for f in e.facts:
                cur.execute(
                    """
                    INSERT INTO facts (repo_id, entity_id, subject, predicate, object, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (repo_id, eid, f.subject, f.predicate, f.object, f.confidence),
                )
                facts_count += 1
            continue

        body = e.body or e.signature or e.name
        if is_doc:
            cur.execute(
                """
                INSERT INTO doc_chunks
                  (repo_id, file_id, entity_id, source_path, section, content,
                   start_line, end_line, sha)
                VALUES (%s, %s, %s,
                        (SELECT path FROM repo_files WHERE file_id = %s),
                        %s, %s, %s, %s, %s)
                """,
                (repo_id, file_id, eid, file_id,
                 e.name, body, e.start_line, e.end_line, _hash(body)),
            )
            doc += 1
        else:
            cur.execute(
                """
                INSERT INTO code_chunks
                  (repo_id, file_id, entity_id, content, language,
                   start_line, end_line, sha)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (repo_id, file_id, eid, body, file_language,
                 e.start_line, e.end_line, _hash(body)),
            )
            code += 1

        for f in e.facts:
            cur.execute(
                """
                INSERT INTO facts (repo_id, entity_id, subject, predicate, object, confidence)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (repo_id, eid, f.subject, f.predicate, f.object, f.confidence),
            )
            facts_count += 1

    return code, doc, facts_count


# ---------------------------------------------------------------------------
# Main entry: chunk a repo
# ---------------------------------------------------------------------------


def chunk_repo(
    repo: Repo,
    on_progress: ProgressFn = _noop,
) -> ChunkResult:
    if not repo.clone_path:
        return ChunkResult(False, 0, 0, 0, 0, 0, error="repo has no clone_path")

    clone_path = Path(repo.clone_path)
    if not clone_path.exists():
        return ChunkResult(
            False, 0, 0, 0, 0, 0, error=f"clone_path missing: {clone_path}"
        )

    run_id = start_run(repo.repo_id, "parse", notes=f"chunk_repo({repo.display_name})")
    set_repo_status(repo.repo_id, "parsing")

    try:
        on_progress(f"wiping previous pipeline data for {repo.display_name}…")
        _wipe_repo_pipeline_data(repo.repo_id)

        on_progress("walking files…")
        files = list(walk_repo(clone_path, special_notes=repo.special_notes))
        on_progress(f"walked {len(files)} files; persisting…")

        path_to_id = _insert_files_batch(repo.repo_id, files)
        by_lang: dict[str, int] = {}
        for f in files:
            by_lang[f.language] = by_lang.get(f.language, 0) + 1
        on_progress(
            "persisted files; counts by lang: "
            + ", ".join(f"{k}={v}" for k, v in sorted(by_lang.items(), key=lambda x: -x[1]))
        )

        set_repo_status(repo.repo_id, "chunking")

        # Filter to parseable languages.
        parseable_langs = {"java", "typescript", "xml", "sql", "markdown"}
        parseable_files = [f for f in files if f.language in parseable_langs]
        on_progress(f"parsing {len(parseable_files)} parseable files…")

        files_parsed = 0
        total_entities = 0
        total_code_chunks = 0
        total_doc_chunks = 0
        total_facts = 0
        parsed_file_ids: list[UUID] = []

        with get_conn() as conn, conn.cursor() as cur:
            for i, f in enumerate(parseable_files, 1):
                if i % 200 == 0 or i == len(parseable_files):
                    on_progress(
                        f"parsing… {i}/{len(parseable_files)}  "
                        f"(entities={total_entities}, "
                        f"chunks={total_code_chunks}+{total_doc_chunks}, "
                        f"facts={total_facts})"
                    )

                file_id = path_to_id.get(f.rel_path)
                if file_id is None:
                    continue

                top_entities = _parse_dispatch(f)
                if not top_entities:
                    continue

                ent_count = _persist_entities_recursive(cur, repo.repo_id, file_id, top_entities)
                code_n, doc_n, fact_n = _persist_chunks_and_facts(
                    cur, repo.repo_id, file_id, top_entities, f.language
                )

                files_parsed += 1
                parsed_file_ids.append(file_id)
                total_entities += ent_count
                total_code_chunks += code_n
                total_doc_chunks += doc_n
                total_facts += fact_n

                if i % 500 == 0:
                    conn.commit()

            conn.commit()

        _mark_files_parsed(parsed_file_ids)

        counts = {
            "files": len(files),
            "files_parsed": files_parsed,
            "entities": total_entities,
            "code_chunks": total_code_chunks,
            "doc_chunks": total_doc_chunks,
            "facts": total_facts,
        }
        finish_run(run_id, "success", counts=counts)
        set_repo_status(repo.repo_id, "ready")
        on_progress(
            f"done — files={len(files)} parsed={files_parsed} "
            f"entities={total_entities} "
            f"chunks(code/doc)={total_code_chunks}/{total_doc_chunks} "
            f"facts={total_facts}"
        )

        return ChunkResult(
            success=True,
            files_seen=len(files),
            files_parsed=files_parsed,
            entities=total_entities,
            chunks=total_code_chunks + total_doc_chunks,
            facts=total_facts,
        )

    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        finish_run(run_id, "error", error_message=err[:1000])
        set_repo_status(repo.repo_id, "error", error_message=err[:1000])
        return ChunkResult(False, 0, 0, 0, 0, 0, error=err)
