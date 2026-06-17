"""
RealPage AO Performance Recorder
Launches Chrome, records the full session (video + HAR + metrics + console errors).
Type 'done' in the terminal when finished.

Usage:
  python scripts/perf_record_ocr.py --env ocr   # aoocr.realpage.com (reskin)
  python scripts/perf_record_ocr.py --env qa    # aoqa.realpage.com  (prod)
"""

import argparse, json, time, sys, traceback
from pathlib import Path
from datetime import datetime
from playwright.sync_api import sync_playwright, Page

# Write all output to a log file — readable even when running in background
_LOG = Path("docs/perf_reports/recorder.log")
_LOG.parent.mkdir(parents=True, exist_ok=True)
_log_fh = open(_LOG, "w", buffering=1, encoding="utf-8")
sys.stdout = _log_fh
sys.stderr = _log_fh

ENVS = {
    "ocr": "https://aoocr.realpage.com",
    "qa":  "https://aoqa.realpage.com",
}

REPORT_DIR = Path("docs/perf_reports")


def collect_vitals(page: Page) -> dict:
    try:
        return page.evaluate("() => window.__perfData || {}")
    except Exception:
        return {}


def collect_nav_timing(page: Page) -> dict:
    try:
        return page.evaluate("""() => {
            const n = performance.getEntriesByType('navigation')[0];
            if (!n) return {};
            return {
                dns:         n.domainLookupEnd  - n.domainLookupStart,
                tcp:         n.connectEnd       - n.connectStart,
                ttfb:        n.responseStart    - n.requestStart,
                response:    n.responseEnd      - n.responseStart,
                dom_loading: n.domContentLoadedEventEnd - n.responseEnd,
                load_event:  n.loadEventEnd     - n.loadEventStart,
                total_load:  n.loadEventEnd     - n.fetchStart,
            };
        }""")
    except Exception:
        return {}


def collect_resource_timing(page: Page) -> list:
    try:
        return page.evaluate("""() =>
            performance.getEntriesByType('resource').map(r => ({
                name:     r.name,
                type:     r.initiatorType,
                duration: Math.round(r.duration),
                size:     r.transferSize || 0,
                start:    Math.round(r.startTime),
            }))
        """)
    except Exception:
        return []


def inject_vitals(page: Page):
    try:
        page.evaluate("""() => {
            if (window.__perfData) return;
            window.__perfData = { lcp: null, cls: 0, fid: null, longTasks: [] };
            try { new PerformanceObserver(l => { l.getEntries().forEach(e => { window.__perfData.lcp = e.startTime; }); }).observe({type:'largest-contentful-paint',buffered:true}); } catch(e) {}
            try { new PerformanceObserver(l => { l.getEntries().forEach(e => { window.__perfData.cls += e.value; }); }).observe({type:'layout-shift',buffered:true}); } catch(e) {}
            try { new PerformanceObserver(l => { l.getEntries().forEach(e => { if(!window.__perfData.fid) window.__perfData.fid = e.processingStart - e.startTime; }); }).observe({type:'first-input',buffered:true}); } catch(e) {}
            try { new PerformanceObserver(l => { l.getEntries().forEach(e => { window.__perfData.longTasks.push({start:e.startTime,duration:e.duration}); }); }).observe({type:'longtask',buffered:true}); } catch(e) {}
        }""")
    except Exception:
        pass


def summarize_resources(resources: list) -> dict:
    by_type = {}
    slow  = [r for r in resources if r["duration"] > 500]
    large = [r for r in resources if r["size"] > 500_000]
    for r in resources:
        t = r["type"]
        if t not in by_type:
            by_type[t] = {"count": 0, "total_ms": 0, "total_bytes": 0}
        by_type[t]["count"] += 1
        by_type[t]["total_ms"]    += r["duration"]
        by_type[t]["total_bytes"] += r["size"]
    return {
        "by_type": by_type,
        "slow_requests":  sorted(slow,  key=lambda x: -x["duration"])[:20],
        "large_requests": sorted(large, key=lambda x: -x["size"])[:10],
    }


def print_summary(r: dict):
    v  = r["web_vitals"]
    n  = r["navigation_timing"]
    rs = r["resource_summary"]

    sep = "=" * 64
    print(f"\n{sep}")
    print(f"  PERFORMANCE REPORT — {r['env'].upper()}  |  {r['url']}")
    print(sep)

    lcp  = v.get("lcp_ms", 0)
    cls  = v.get("cls_score", 0)
    fid  = v.get("fid_ms", 0)
    lt   = v.get("long_tasks", 0)
    ltms = v.get("long_task_total_ms", 0)

    def grade(val, good, poor, unit="ms"):
        tag = "GOOD" if val <= good else ("POOR" if val > poor else "NEEDS IMPROVEMENT")
        return f"{val:.1f} {unit}  [{tag}]"

    print("\n── Web Vitals ─────────────────────────────────────────────")
    print(f"  LCP  Largest Contentful Paint  {grade(lcp,  2500, 4000)}")
    print(f"  CLS  Cumulative Layout Shift   {grade(cls,  0.1,  0.25, '')}")
    print(f"  FID  First Input Delay         {grade(fid,  100,  300)}")
    print(f"  Long Tasks  {lt} tasks  /  {ltms:.0f} ms total  {'⚠  BLOCKS MAIN THREAD' if ltms > 500 else ''}")

    if n:
        print("\n── Navigation Timing ──────────────────────────────────────")
        labels = {
            "dns": "DNS lookup",
            "tcp": "TCP connect",
            "ttfb": "Time to First Byte",
            "response": "Response download",
            "dom_loading": "DOM / JS parse",
            "load_event": "Load event",
            "total_load": "Total page load",
        }
        for k, label in labels.items():
            val = n.get(k, 0)
            flag = ""
            if k == "ttfb"       and val > 600:   flag = "  ⚠ slow server"
            if k == "total_load" and val > 5000:  flag = "  ⚠ very slow"
            if k == "dom_loading"and val > 2000:  flag = "  ⚠ heavy JS"
            print(f"  {label:<28} {val:>7.0f} ms{flag}")

    by_type = rs.get("by_type", {})
    if by_type:
        print("\n── Resources by Type ──────────────────────────────────────")
        print(f"  {'Type':<14} {'Reqs':>5}  {'Total ms':>9}  {'Total KB':>9}")
        for typ, d in sorted(by_type.items(), key=lambda x: -x[1]["total_ms"]):
            print(f"  {typ:<14} {d['count']:>5}  {d['total_ms']:>9.0f}  {d['total_bytes']/1024:>9.1f}")

    slow = rs.get("slow_requests", [])
    if slow:
        print(f"\n── Slowest Requests  (>{500}ms) ────────────────────────────")
        for req in slow[:12]:
            name = req["name"].split("?")[0][-72:]
            print(f"  {req['duration']:>7} ms  [{req['type']:<8}]  {name}")

    large = rs.get("large_requests", [])
    if large:
        print(f"\n── Largest Responses  (>500 KB) ───────────────────────────")
        for req in large[:8]:
            name = req["name"].split("?")[0][-72:]
            print(f"  {req['size']/1024:>8.1f} KB  {name}")

    errs = r.get("console_errors", [])
    if errs:
        print(f"\n── Console Errors / Warnings  ({len(errs)}) ──────────────────────")
        for e in errs[:20]:
            print(f"  [{e['type'].upper():<7}]  {e['text'][:100]}")
    else:
        print("\n── Console Errors / Warnings  — none ─────────────────────")

    fails = r.get("failed_requests", [])
    if fails:
        print(f"\n── Failed Network Requests  ({len(fails)}) ──────────────────────────")
        for f in fails[:12]:
            print(f"  {f['method']}  {f['url'][:80]}")
            if f.get("failure"):
                print(f"         → {f['failure']}")

    http_errs = r.get("http_errors", [])
    if http_errs:
        print(f"\n── HTTP 4xx / 5xx Responses  ({len(http_errs)}) ──────────────────────")
        for e in http_errs[:12]:
            print(f"  {e['status']}  {e['method']}  {e['url'][:80]}")

    print(f"\n  Screenshots : {r['shots_dir']}")
    print(f"  Video       : {r['video_path']}")
    print(f"  Full report : {r['report_path']}")
    print(f"  HAR         : {r['har_path']}")
    print(sep + "\n")


def run(env: str):
    base_url = ENVS[env]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    shots_dir  = REPORT_DIR / f"shots_{env}_{ts}"
    shots_dir.mkdir(parents=True, exist_ok=True)
    har_path    = str(REPORT_DIR / f"network_{env}_{ts}.har")
    report_path = str(REPORT_DIR / f"perf_{env}_{ts}.json")

    console_errors   = []
    failed_requests  = []
    http_errors      = []

    print(f"\n{'='*64}")
    print(f"  Recording started — {env.upper()}  ({base_url})")
    print(f"  Video, HAR, screenshots and metrics are being captured.")
    print(f"  Go ahead and use the app normally.")
    print(f"  Type  done  here and press ENTER when you are finished.")
    print(f"{'='*64}\n")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--start-maximized"],
        )
        ctx = browser.new_context(
            viewport=None,
            record_har_path=har_path,
        )
        page = ctx.new_page()

        # Wire up listeners
        page.on("console", lambda msg: console_errors.append({
            "type": msg.type,
            "text": msg.text,
            "location": f"{msg.location.get('url','')}:{msg.location.get('lineNumber','')}",
        }) if msg.type in ("error", "warning") else None)

        page.on("requestfailed", lambda req: failed_requests.append({
            "url":     req.url,
            "method":  req.method,
            "failure": req.failure,
        }))

        page.on("response", lambda resp: http_errors.append({
            "url":    resp.url,
            "status": resp.status,
            "method": resp.request.method,
        }) if resp.status >= 400 else None)

        # Inject vitals observer on every navigation
        page.on("framenavigated", lambda frame: inject_vitals(page) if frame == page.main_frame else None)

        # Navigate
        page.goto(base_url, wait_until="domcontentloaded")
        inject_vitals(page)
        page.screenshot(path=str(shots_dir / "00_start.png"))

        # Wait until stop flag is created (created by stop_recording.py or manually)
        stop_flag = REPORT_DIR / f"STOP_{env}"
        stop_flag.unlink(missing_ok=True)
        print(f"  Stop flag: {stop_flag}")
        print(f"  (Create that file, or run:  python scripts/perf_record_ocr.py --stop {env})")
        while not stop_flag.exists():
            time.sleep(1)
        stop_flag.unlink(missing_ok=True)

        # Final screenshot
        page.screenshot(path=str(shots_dir / "99_final.png"))

        # Collect metrics from current page
        vitals    = collect_vitals(page)
        nav       = collect_nav_timing(page)
        resources = collect_resource_timing(page)
        res_sum   = summarize_resources(resources)

        video_path = "not recorded (ffmpeg unavailable)"

        ctx.close()  # flushes HAR
        browser.close()

    report = {
        "env":      env,
        "url":      base_url,
        "recorded_at": ts,
        "web_vitals": {
            "lcp_ms":             round(vitals.get("lcp") or 0, 1),
            "cls_score":          round(vitals.get("cls") or 0, 4),
            "fid_ms":             round(vitals.get("fid") or 0, 1),
            "long_tasks":         len(vitals.get("longTasks") or []),
            "long_task_total_ms": sum(t["duration"] for t in (vitals.get("longTasks") or [])),
        },
        "navigation_timing":  nav,
        "resource_summary":   res_sum,
        "console_errors":     console_errors,
        "failed_requests":    failed_requests,
        "http_errors":        http_errors,
        "shots_dir":          str(shots_dir),
        "video_path":         str(video_path),
        "report_path":        report_path,
        "har_path":           har_path,
    }

    Path(report_path).write_text(json.dumps(report, indent=2))
    print_summary(report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", choices=["ocr", "qa"], default="ocr")
    parser.add_argument("--stop", choices=["ocr", "qa"], default=None,
                        help="Signal a running recorder to stop and generate report")
    args = parser.parse_args()

    if args.stop:
        flag = REPORT_DIR / f"STOP_{args.stop}"
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
        print(f"Stop signal sent for env={args.stop}")
    else:
        try:
            run(args.env)
        except Exception:
            traceback.print_exc()
            sys.exit(1)
