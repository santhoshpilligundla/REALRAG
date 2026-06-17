import sys
from pathlib import Path
from html.parser import HTMLParser
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

p = Path("docs/realrag_flowchart_detailed.html")
html = p.read_text(encoding="utf-8")
print(f"file: {p}  ({len(html):,} bytes)")

VOID = {"meta", "br", "img", "hr", "input", "link"}


class V(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"mismatch </{tag}> (open tail: {self.stack[-3:]})")
            if tag in self.stack:
                while self.stack and self.stack.pop() != tag:
                    pass
        else:
            self.stack.pop()


v = V()
v.parse_err = None
try:
    v.feed(html)
except Exception as e:
    v.parse_err = e
print("parse error:", v.parse_err)
print("unclosed tags at end:", v.stack)
print("tag mismatches:", v.errors or "none")

checks = {
    "DOCTYPE": html.lstrip().lower().startswith("<!doctype html>"),
    "has <title>": "<title>" in html,
    "runtime section": "Runtime &mdash; answering a question (detailed)".replace("&mdash;", "") not in html or "Runtime" in html,
    "offline section": "building the knowledge base (detailed)" in html,
    "agent section": "multi-step agent" in html,
    "file-reference section": "What each file does" in html,
    "runtime table": "Query orchestrator" in html,
    "indexing table": "Dispatch files to parsers" in html,
    "foundation table": "Embedded Postgres" in html,
    "legend present": "green = function" in html,
}
# all runtime steps R1..R11 + AG
for n in list(range(1, 12)):
    checks[f"runtime step R{n}"] = f'<span class="id">R{n}</span>' in html
checks["agent step AG"] = '<span class="id">AG</span>' in html
# all offline steps O1..O10
for n in list(range(1, 11)):
    checks[f"offline step O{n}"] = f'<span class="id">O{n}</span>' in html

# every lib file should be referenced
LIB = ["chat", "intent", "retrieval", "faiss_store", "kg", "business_docs", "code_search",
       "llm", "embedder", "glossary", "agent", "query_cache", "sessions_repo",
       "clone", "walker", "chunker", "parser_java", "parser_typescript", "parser_xml",
       "parser_sql", "parser_ddl", "parser_jrxml", "parser_markdown", "doc_gen",
       "doc_gen_pass2", "doc_gen_pass3", "doc_gen_l4_enrich", "cross_repo",
       "embed_pipeline", "coverage", "git_history", "db", "config", "models",
       "presets", "repos_repo", "runs_repo"]
for f in LIB:
    checks[f"mentions lib/{f}.py"] = f"lib/{f}.py" in html
checks["mentions frontend"] = "frontend/streamlit_app.py" in html

# key data stores named
for d in ["sessions", "messages", "entities", "code_chunks", "facts",
          "generated_docs", "cross_repo_edges", "query_cache"]:
    checks[f"data store {d}"] = d in html

bad = [k for k, ok in checks.items() if not ok]
print("\nstructural checks:", "ALL PASS" if not bad else f"{len(bad)} FAILED")
for k in bad:
    print("  MISSING:", k)
ok = not bad and not v.errors and not v.stack and not v.parse_err
print("\nVALID" if ok else "\nNEEDS FIX")
