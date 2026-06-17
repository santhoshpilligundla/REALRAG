"""Eval sweep: ~100 business-phrased questions across rm-web widgets/workflows,
ys-reports reports, and po ETLs/forecasting. Runs each through the real fast
chat path and classifies answered / weak / refused. Logs failures so we can see
coverage gaps. Mirrors the UI default path (no manual deep/agent)."""
import sys
import re
import random
import time
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import get_conn
from lib.faiss_store import load_index
load_index.cache_clear()
from lib.chat import prepare_answer, stream_answer, answer_looks_unsure, context_covers_subjects

random.seed(7)

_SUFFIX = re.compile(r"(WidgetComponent|Component|Widget|ServiceImpl|Service|Controller|"
                     r"ReportProcessor|Report|TaskHelper|Task|Builder|Helper|DaoImpl|Dao|"
                     r"Executor|Job|Page2|Page)$")


def humanize(qn: str) -> str:
    base = qn.split(".")[0].split("/")[-1]
    base = _SUFFIX.sub("", base)
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", base).strip()
    words = re.sub(r"\s+", " ", words)
    return words or base


def sample(sql, params, n):
    with get_conn() as c, c.cursor() as cur:
        cur.execute(sql, params) if params else cur.execute(sql)
        rows = [r[0] for r in cur.fetchall() if r[0]]
    random.shuffle(rows)
    return rows[:n]


def build_questions():
    qs = []  # (area, question)

    # rm-web widgets / components
    for qn in sample("""SELECT DISTINCT e.qualified_name FROM entities e JOIN repos r ON r.repo_id=e.repo_id
        WHERE r.display_name='rm-web' AND (e.qualified_name ILIKE '%Widget%' OR e.qualified_name ILIKE '%Chart%'
              OR e.qualified_name ILIKE '%Component') AND e.qualified_name NOT ILIKE '%spec%'""", (), 30):
        label = humanize(qn)
        if len(label) < 4:
            continue
        qs.append(("rm-web", random.choice([
            f"What does the {label} widget show?",
            f"Where does the {label} get its data from?",
            f"What is the {label} used for?"])))

    # rm-web workflows (known)
    for wf in ["Rent Roll Grid", "Rent Roll Unit Level", "Month to Month", "Renewal Expirations",
               "New Lease Workflow", "Lease Audit", "Expiration Summary Statistics", "Unit Management",
               "Revenue Management", "Lease Up Analysis", "Competitor", "Unit Rent Components"]:
        qs.append(("rm-web", f"How does the {wf} workflow work in RMS?"))

    # ys-reports
    for qn in sample("""SELECT DISTINCT e.qualified_name FROM entities e JOIN repos r ON r.repo_id=e.repo_id
        WHERE r.display_name='ys-reports' AND e.qualified_name IS NOT NULL AND e.qualified_name NOT ILIKE '%spec%'""", (), 25):
        label = humanize(qn)
        if len(label) < 4:
            continue
        qs.append(("ys-reports", random.choice([
            f"What does the {label} report show?",
            f"What data does the {label} report use?"])))

    # po ETL services
    for qn in sample("""SELECT DISTINCT e.qualified_name FROM entities e
        WHERE e.kind='xml_service' AND e.qualified_name IS NOT NULL""", (), 22):
        qs.append(("po-etl", random.choice([
            f"What does the {qn} ETL step do?",
            f"What data does {qn} load and where from?"])))

    # po forecasting / calc functions
    for qn in sample("""SELECT DISTINCT e.qualified_name FROM entities e
        WHERE e.kind IN ('sql_function','sql_procedure')
           OR e.qualified_name ILIKE '%RecommendationArchive%' OR e.qualified_name ILIKE '%Forecast%'
           OR e.qualified_name ILIKE '%Seasonal%' OR e.qualified_name ILIKE '%Rates%'""", (), 16):
        label = humanize(qn)
        qs.append(("po-calc", f"How is {label} calculated or used in forecasting?"))

    return qs


def classify(prep, ans, question):
    if prep["mode"] == "final":
        return "refused"
    if answer_looks_unsure(ans) or len((ans or "").split()) < 20:
        return "weak"
    if not context_covers_subjects(question, prep.get("context_text", "")):
        return "weak"
    return "answered"


def main():
    pid = None
    with get_conn() as c:
        pid = c.execute("SELECT product_id FROM products WHERE name='RMS'").fetchone()[0]
    qs = build_questions()
    print(f"[{time.strftime('%H:%M:%S')}] generated {len(qs)} questions", flush=True)
    buckets = {"answered": 0, "weak": 0, "refused": 0}
    by_area = {}
    failures = []
    for i, (area, q) in enumerate(qs, 1):
        try:
            prep = prepare_answer(q, product_id=pid)
            ans = prep.get("answer") if prep["mode"] == "final" else "".join(stream_answer(prep))
            verdict = classify(prep, ans, q)
        except Exception as e:
            verdict, ans = "refused", f"ERROR {type(e).__name__}: {e}"
        buckets[verdict] += 1
        by_area.setdefault(area, {"answered": 0, "weak": 0, "refused": 0})[verdict] += 1
        if verdict != "answered":
            failures.append((area, verdict, q))
        if i % 10 == 0:
            print(f"[{time.strftime('%H:%M:%S')}] {i}/{len(qs)}  {buckets}", flush=True)

    print("\n===== RESULTS =====", flush=True)
    total = sum(buckets.values())
    print(f"answered={buckets['answered']}  weak={buckets['weak']}  refused={buckets['refused']}  "
          f"(answer rate {buckets['answered']/total*100:.0f}%)")
    print("\nby area:")
    for area, b in by_area.items():
        t = sum(b.values())
        print(f"  {area:<12} answered={b['answered']}/{t}  weak={b['weak']}  refused={b['refused']}")
    print(f"\n----- NON-ANSWERED ({len(failures)}) -----")
    for area, verdict, q in failures:
        print(f"  [{area}|{verdict}] {q}")
    print("\nDONE", flush=True)


if __name__ == "__main__":
    main()
