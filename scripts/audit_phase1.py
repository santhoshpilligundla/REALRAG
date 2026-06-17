"""Phase-1 coverage audit across ALL repos.

For every walked file: does it have entities? For files with ZERO entities, does
the (fixed) parser now produce entities? That gap = silently-missing content
(like the etl2posql.xml bug). Read-only — safe to run anytime.
"""
import sys
from pathlib import Path
from collections import defaultdict
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn
from lib.parser_xml import parse_xml_file
from lib.parser_jrxml import parse_jrxml_file
from lib.parser_ddl import parse_ddl_file, looks_like_ddl
from lib.parser_java import parse_java_file
from lib.parser_typescript import parse_typescript_file
from lib.parser_markdown import parse_markdown_file

PARSEABLE = {"java", "typescript", "xml", "sql", "markdown"}


def reparse(lang: str, ap: Path) -> int:
    try:
        if lang == "java":
            return len(parse_java_file(ap))
        if lang == "typescript":
            return len(parse_typescript_file(ap))
        if lang == "xml":
            if ap.suffix.lower() == ".jrxml":
                return len(parse_jrxml_file(ap))
            return len(parse_xml_file(ap))
        if lang == "sql":
            return len(parse_ddl_file(ap)) if looks_like_ddl(ap) else 0
        if lang == "markdown":
            return len(parse_markdown_file(ap))
    except Exception:
        return -1
    return 0


def main() -> None:
    with get_conn() as c, c.cursor() as cur:
        cur.execute("SELECT repo_id, display_name, clone_path FROM repos ORDER BY display_name")
        repos = cur.fetchall()

    print(f"{'repo':<12}{'lang':<12}{'files':>7}{'0-ent':>7}{'parseable?':>11}")
    print("-" * 52)
    fixable: list[tuple] = []
    skipped_langs: dict[str, int] = defaultdict(int)

    for rid, name, clone in repos:
        with get_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT rf.language, count(*) AS files,
                       count(*) FILTER (WHERE NOT EXISTS (
                           SELECT 1 FROM entities e WHERE e.file_id = rf.file_id)) AS zero
                  FROM repo_files rf WHERE rf.repo_id = %s
                 GROUP BY rf.language ORDER BY zero DESC
            """, (rid,))
            rows = cur.fetchall()
        for lang, files, zero in rows:
            mark = "yes" if lang in PARSEABLE else "NO (skipped)"
            print(f"{name:<12}{str(lang):<12}{files:>7}{zero:>7}{mark:>11}")
            if lang not in PARSEABLE:
                skipped_langs[str(lang)] += files

        # For zero-entity files in PARSEABLE langs, re-parse a sample to find fixable gaps.
        with get_conn() as c, c.cursor() as cur:
            cur.execute("""
                SELECT rf.file_id, rf.language, rf.path FROM repo_files rf
                 WHERE rf.repo_id = %s AND rf.language = ANY(%s)
                   AND NOT EXISTS (SELECT 1 FROM entities e WHERE e.file_id = rf.file_id)
            """, (rid, list(PARSEABLE)))
            zfiles = cur.fetchall()
        per_lang_checked: dict[str, int] = defaultdict(int)
        for fid, lang, path in zfiles:
            if per_lang_checked[lang] >= 40:   # cap re-parses per lang for speed
                continue
            per_lang_checked[lang] += 1
            ap = Path(clone) / path
            if not ap.exists() or ap.stat().st_size > 600_000:
                continue
            n = reparse(lang, ap)
            if n > 0:
                fixable.append((name, lang, path, n))

    print("\n=== FIXABLE GAPS (0 entities in DB, but parser now yields entities) ===")
    if not fixable:
        print("  none found in sampled zero-entity files.")
    for name, lang, path, n in fixable[:60]:
        print(f"  [{name}] {lang}  +{n}  {path}")
    print(f"\n  total fixable found (sampled): {len(fixable)}")

    print("\n=== languages walked but NOT parsed by design (no entities ever) ===")
    for lang, n in sorted(skipped_langs.items(), key=lambda x: -x[1]):
        print(f"  {lang}: {n} files")


if __name__ == "__main__":
    main()
