import re
import sys
from pathlib import Path
from html.parser import HTMLParser
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

p = Path("docs/realrag_flow_full.html")
html = p.read_text(encoding="utf-8")
print(f"file: {p}  ({len(html):,} bytes)")

VOID = {"meta", "br", "img", "hr", "input", "link"}


class V(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.errors = []
        self.ids = set()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if "id" in d:
            self.ids.add(d["id"])
        if tag == "a" and d.get("href", "").startswith("#"):
            self.hrefs.append(d["href"][1:])
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"mismatch </{tag}> tail={self.stack[-3:]}")
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
print("unclosed tags:", v.stack)
print("tag mismatches:", v.errors or "none")

# 1. all anchor targets exist
broken = [h for h in v.hrefs if h and h not in v.ids]
print("broken in-page links:", broken or "none")

# 2. every detail card id present
expected_ids = [f"s{i}" for i in range(1, 20)] + ["sa"] + [f"o{i}" for i in range(1, 11)]
missing_ids = [i for i in expected_ids if i not in v.ids]
print("missing detail cards:", missing_ids or "none")

# 3. every detail card is linked from a node in the flow
missing_links = [i for i in expected_ids if i not in v.hrefs]
print("detail cards not linked from flow:", missing_links or "none")

# 4. key files referenced
files = ["frontend/streamlit_app.py", "lib/sessions_repo.py", "lib/query_cache.py",
         "lib/chat.py", "lib/intent.py", "lib/embedder.py", "lib/kg.py", "lib/retrieval.py",
         "lib/faiss_store.py", "lib/business_docs.py", "lib/code_search.py", "lib/llm.py",
         "lib/glossary.py", "lib/agent.py", "lib/clone.py", "lib/walker.py", "lib/chunker.py",
         "lib/parser_java.py", "lib/parser_typescript.py", "lib/parser_xml.py",
         "lib/parser_sql.py", "lib/parser_ddl.py", "lib/parser_jrxml.py", "lib/parser_markdown.py",
         "lib/doc_gen.py", "lib/doc_gen_pass2.py", "lib/doc_gen_pass3.py", "lib/doc_gen_l4_enrich.py",
         "lib/cross_repo.py", "lib/embed_pipeline.py", "lib/coverage.py", "lib/git_history.py",
         "lib/db.py"]
missing_files = [f for f in files if f not in html]
print("files not mentioned:", missing_files or "none")

# 5. key verified function names referenced
fns = ["_render_chat_tab", "prepare_answer", "_fast_intent", "classify", "embed_texts",
       "_kg_answer_for", "retrieve", "vector_arm_code", "vector_arm_generated", "fts_arm",
       "symbol_arm", "business_arm", "_fuse_and_rank", "_llm_rerank", "_code_fallback_answer",
       "search_code", "_file_full_listing", "stream_answer", "stream_text",
       "answer_looks_unsure", "answer_has_fabrication", "context_covers_subjects",
       "run_agent", "_dispatch", "chunk_repo", "_parse_dispatch", "doc_gen_repo",
       "discover_edges", "embed_repo", "write_index", "save_message", "clone_repo", "walk"]
missing_fns = [f for f in fns if f not in html]
print("functions not mentioned:", missing_fns or "none")

ok = (not v.parse_err and not v.stack and not v.errors and not broken
      and not missing_ids and not missing_links and not missing_files and not missing_fns)
print("\nVALID" if ok else "\nNEEDS FIX")
