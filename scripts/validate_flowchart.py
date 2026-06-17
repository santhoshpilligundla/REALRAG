import sys
from pathlib import Path
from html.parser import HTMLParser
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

p = Path("docs/realrag_flowchart.html")
html = p.read_text(encoding="utf-8")
print(f"file: {p}  ({len(html):,} bytes)")

# 1. Well-formed parse + tag balance
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
            self.errors.append(f"mismatch </{tag}> (open: {self.stack[-3:]})")
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

# 2. Structural checks
checks = {
    "DOCTYPE": html.lstrip().lower().startswith("<!doctype html>"),
    "has <title>": "<title>" in html,
    "offline section": "Build the knowledge base" in html,
    "runtime section": "Answer a question (runtime)" in html,
    "agent section": "AGENT LOOP" in html,
}
# every offline step 1..13 + DB present
for n in list(range(1, 14)):
    checks[f"offline step {n}"] = f'<span class="n">{n}</span>' in html
# runtime steps 1..11
for n in list(range(1, 12)):
    checks[f"runtime step {n}"] = f'<span class="n">{n}</span>' in html

# key files referenced
for f in ["lib/clone.py", "lib/walker.py", "lib/chunker.py", "lib/parser_xml.py",
          "lib/parser_sql.py", "lib/doc_gen.py", "lib/embed_pipeline.py", "lib/faiss_store.py",
          "lib/retrieval.py", "lib/chat.py", "lib/intent.py", "lib/kg.py", "lib/embedder.py",
          "lib/business_docs.py", "lib/code_search.py", "lib/agent.py", "lib/sessions_repo.py",
          "lib/query_cache.py", "lib/llm.py", "lib/glossary.py", "lib/cross_repo.py",
          "frontend/streamlit_app.py"]:
    checks[f"mentions {f}"] = f in html

bad = [k for k, ok in checks.items() if not ok]
print("\nstructural checks:", "ALL PASS" if not bad else f"{len(bad)} FAILED")
for k in bad:
    print("  MISSING:", k)
print("\nVALID" if not bad and not v.errors and not v.stack and not v.parse_err else "\nNEEDS FIX")
