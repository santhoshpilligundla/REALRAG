import sys
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.db import get_conn

with get_conn() as c, c.cursor() as cur:
    # find the file
    cur.execute("SELECT file_id, path FROM repo_files WHERE path ILIKE '%etl2posql.xml%'")
    files = cur.fetchall()
    print("files:", files)
    for fid, path in files:
        cur.execute("SELECT count(*) FROM entities WHERE file_id=%s", (fid,))
        n_ent = cur.fetchone()[0]
        cur.execute("SELECT count(*), coalesce(avg(length(content))::int,0), coalesce(max(length(content)),0) FROM code_chunks WHERE file_id=%s", (fid,))
        nch, avg, mx = cur.fetchone()
        print(f"\n  {path}")
        print(f"    entities: {n_ent}   code_chunks: {nch}  avg_len={avg}  max_len={mx}")
        cur.execute("SELECT qualified_name, kind FROM entities WHERE file_id=%s ORDER BY qualified_name LIMIT 15", (fid,))
        print("    sample entities:", [r[0] for r in cur.fetchall()])
        # how many generated docs for these entities
        cur.execute("""SELECT count(*) FROM generated_docs d JOIN entities e ON e.entity_id=d.entity_id WHERE e.file_id=%s""", (fid,))
        print("    generated_docs for this file's entities:", cur.fetchone()[0])
