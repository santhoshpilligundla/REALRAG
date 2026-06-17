import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn
from lib.parser_xml import parse_xml_file, _xml_root, _read_text, _collect_entries

p = Path("storage/repos/RMS/po/master/rms-etl/src/main/java/com/yieldstar/etl/po/etl2posql.xml")
print("file exists:", p.exists(), "size:", p.stat().st_size if p.exists() else 0)

# 1. what language did indexing assign?
with get_conn() as c, c.cursor() as cur:
    cur.execute("SELECT path, language, size_bytes FROM repo_files WHERE path ILIKE '%etl2posql.xml%'")
    print("repo_files row:", cur.fetchone())

# 2. does the XML even parse?
raw = _read_text(p)
root = _xml_root(raw)
print("xml_root parsed:", root is not None)
if root is not None:
    print("  <service> elements found by lxml:", len(_collect_entries(root, "service")))

# 3. does the parser return entities now?
ents = parse_xml_file(p)
print("parse_xml_file entities:", len(ents))
for e in ents[:8]:
    print("   ", e.kind, e.name)
