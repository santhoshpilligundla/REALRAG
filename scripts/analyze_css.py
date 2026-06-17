import json, sys
from pathlib import Path

# Find latest OCR and QA HARs
perf_dir = Path("docs/perf_reports")
ocr_hars = sorted(perf_dir.glob("network_ocr_*.har"))
qa_hars  = sorted(perf_dir.glob("network_qa_*.har"))

def analyze(har_path, label):
    har = json.loads(har_path.read_text(encoding="utf-8"))
    entries = har["log"]["entries"]

    css = []
    for e in entries:
        url = e["request"]["url"]
        mime = e["response"]["content"].get("mimeType", "")
        is_css = (
            ".css" in url.lower()
            or mime.startswith("text/css")
            or "stylesheet" in mime
        )
        if is_css:
            css.append(e)

    print(f"\n{'='*100}")
    print(f"  CSS LOADING ANALYSIS — {label}")
    print(f"  HAR: {har_path.name}")
    print(f"  Total CSS requests: {len(css)}")
    print(f"{'='*100}")

    if not css:
        print("  No CSS entries found.")
        return

    hdr = f"  {'File':<55} {'DNS':>5} {'TCP':>5} {'SSL':>5} {'Send':>5} {'Wait':>6} {'Recv':>6} {'Total':>7} {'KB':>7} {'Status':>6}"
    print(hdr)
    print("  " + "-"*98)

    total_time = 0
    total_kb = 0
    for e in sorted(css, key=lambda x: -x["time"]):
        t = e["timings"]
        url_short = e["request"]["url"].split("?")[0]
        # strip protocol and domain for readability
        parts = url_short.split("/")
        file_part = "/".join(parts[3:])[-54:]

        dns  = max(t.get("dns",     -1), 0)
        tcp  = max(t.get("connect", -1), 0)
        ssl  = max(t.get("ssl",     -1), 0)
        send = max(t.get("send",    -1), 0)
        wait = max(t.get("wait",    -1), 0)
        recv = max(t.get("receive", -1), 0)
        total = e["time"]
        body  = e["response"].get("bodySize", 0)
        if body < 0:
            body = e["response"]["content"].get("size", 0)
        kb = body / 1024
        status = e["response"]["status"]
        cached = "(cached)" if status == 304 or (dns == 0 and tcp == 0 and body == 0) else ""

        total_time += total
        total_kb += kb

        flag = "  <-- SLOW" if total > 1000 else ("  <-- LARGE" if kb > 100 else "")
        print(f"  {file_part:<55} {dns:>5.0f} {tcp:>5.0f} {ssl:>5.0f} {send:>5.0f} {wait:>6.0f} {recv:>6.0f} {total:>7.0f} {kb:>7.1f} {status:>6}  {cached}{flag}")

    print("  " + "-"*98)
    print(f"  {'TOTAL':<55} {'':>5} {'':>5} {'':>5} {'':>5} {'':>6} {'':>6} {total_time:>7.0f} {total_kb:>7.1f}")

    # Bottleneck breakdown
    print(f"\n  Bottleneck breakdown:")
    wait_dominated = [e for e in css if max(e["timings"].get("wait",-1),0) > max(e["timings"].get("receive",-1),0) and e["time"] > 200]
    recv_dominated = [e for e in css if max(e["timings"].get("receive",-1),0) > max(e["timings"].get("wait",-1),0) and e["time"] > 200]
    conn_dominated = [e for e in css if (max(e["timings"].get("connect",-1),0) + max(e["timings"].get("ssl",-1),0)) > 500]

    if wait_dominated:
        print(f"    Server-side slow (wait > receive): {len(wait_dominated)} files — server is taking time to generate/serve the CSS")
    if recv_dominated:
        print(f"    Network-transfer slow (receive > wait): {len(recv_dominated)} files — file size is the bottleneck")
    if conn_dominated:
        print(f"    Connection overhead slow (TCP+SSL > 500ms): {len(conn_dominated)} files — new TCP connections being opened per CDN domain")

    # Domain grouping
    from urllib.parse import urlparse
    domains = {}
    for e in css:
        domain = urlparse(e["request"]["url"]).netloc
        if domain not in domains:
            domains[domain] = {"count": 0, "total_ms": 0, "kb": 0}
        domains[domain]["count"] += 1
        domains[domain]["total_ms"] += e["time"]
        body = e["response"].get("bodySize", 0)
        if body < 0:
            body = e["response"]["content"].get("size", 0)
        domains[domain]["kb"] += body / 1024

    print(f"\n  CSS by origin domain:")
    for domain, d in sorted(domains.items(), key=lambda x: -x[1]["total_ms"]):
        print(f"    {domain:<50} {d['count']:>3} files  {d['total_ms']:>7.0f} ms  {d['kb']:>8.1f} KB")


if ocr_hars:
    analyze(ocr_hars[-1], "OCR (Reskin)")
else:
    print("No OCR HAR found.")

if qa_hars:
    analyze(qa_hars[-1], "QA (Production)")
else:
    print("No QA HAR found.")
