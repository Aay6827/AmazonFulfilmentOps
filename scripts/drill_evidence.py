"""Query Prometheus for latency and alert evidence about the drill, and append it to the report."""
import json
import math
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

PROM = "http://127.0.0.1:45090"
ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "incident-report.md"
events = json.loads((ROOT / "docs" / "drill-results.json").read_text())["events"]

first = events[0]
clock = datetime.strptime(first["clock"], "%H:%M:%S").time()
start = datetime.combine(datetime.now().date(), clock)
if start > datetime.now():
    start -= timedelta(days=1)
t0 = start - timedelta(seconds=first["t_seconds"])


def at(seconds):
    return (t0 + timedelta(seconds=seconds)).timestamp()


def find(prefix):
    for event in events:
        if event["event"].startswith(prefix):
            return event["t_seconds"]
    raise SystemExit("event not found: " + prefix)


def query_range(expr, begin, end):
    params = urllib.parse.urlencode({"query": expr, "start": begin, "end": end, "step": 5})
    with urllib.request.urlopen(PROM + "/api/v1/query_range?" + params, timeout=15) as resp:
        return json.loads(resp.read())["data"]["result"]


def peak(series, begin, end):
    best = None
    for item in series:
        for ts, val in item["values"]:
            value = float(val)
            if begin <= ts <= end and not math.isnan(value):
                best = value if best is None else max(best, value)
    return best


def show(value):
    return "no data" if value is None else f"{value:.2f}s"


P95 = (
    "histogram_quantile(0.95, sum by (le) "
    '(rate(gateway_http_request_seconds_bucket{path="/checkout"}[30s])))'
)
MEAN = (
    'sum(rate(gateway_http_request_seconds_sum{path="/checkout"}[30s])) / '
    'sum(rate(gateway_http_request_seconds_count{path="/checkout"}[30s]))'
)

whole = (at(0) - 30, at(events[-1]["t_seconds"]) + 60)
p95_series = query_range(P95, *whole)
mean_series = query_range(MEAN, *whole)

windows = {
    "Baseline (before incident 1)": (at(10), at(find("INJECTED: promise-service"))),
    "Incident 1 (promise-service down)": (
        at(find("INJECTED: promise-service")),
        at(find("RECOVERY: promise-service")),
    ),
    "Incident 2 (inventory-service down)": (
        at(find("INJECTED: inventory-service")),
        at(find("RECOVERY: inventory-service")),
    ),
}

lines = [
    "",
    "## Latency evidence (queried from Prometheus)",
    "",
    "| Window | Peak mean checkout latency | Peak p95 checkout latency |",
    "|---|---|---|",
]
for name, (begin, end) in windows.items():
    lines.append(
        f"| {name} | {show(peak(mean_series, begin, end))} | {show(peak(p95_series, begin, end))} |"
    )

alert_series = query_range('ALERTS{alertname="CheckoutLatencyP95High"}', *whole)
states = sorted({item["metric"].get("alertstate", "?") for item in alert_series})
lines += [
    "",
    "CheckoutLatencyP95High (threshold 0.5 s for 1 minute) during the drill: "
    + (", ".join(states) if states else "never pending or firing")
    + ".",
]

instant = urllib.parse.urlencode({"query": "count(process_start_time_seconds)"})
with urllib.request.urlopen(PROM + "/api/v1/query?" + instant, timeout=15) as resp:
    result = json.loads(resp.read())["data"]["result"]
count = int(float(result[0]["value"][1])) if result else 0
lines.append(
    f"process_start_time_seconds is exported by {count} scrape targets, "
    "so a restart alert based on it is possible."
)

text = REPORT.read_text()
marker = text.find("\n## Latency evidence")
if marker != -1:
    text = text[:marker]
REPORT.write_text(text.rstrip() + "\n" + "\n".join(lines) + "\n")
print("\n".join(lines))
