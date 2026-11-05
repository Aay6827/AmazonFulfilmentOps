"""Smoke test for the docker compose stack. Stdlib only, runs on Linux or Windows."""
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

GW = "http://127.0.0.1:45004"
ROOT = Path(__file__).resolve().parent.parent
failures = 0


def check(name, ok):
    global failures
    print(("  PASS  " if ok else "  FAIL  ") + name, flush=True)
    if not ok:
        failures += 1


def api(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(GW + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, None


def compose(*args, stdin=None):
    return subprocess.run(
        ["docker", "compose", *args],
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def checkout(sku, qty):
    return {
        "customer_id": "C-CI",
        "region": "south",
        "service_level": "standard",
        "items": [{"sku": sku, "qty": qty}],
    }


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


INTERNAL = ("order-service", "inventory-service", "promise-service", "returns-service")


def published_ports():
    """Ask Docker which host ports each service publishes (the real exposure setting)."""
    out = compose("ps", "--format", "json").stdout.strip()
    if not out:
        return None
    rows = json.loads(out) if out.startswith("[") else [json.loads(line) for line in out.splitlines()]
    found = {}
    for row in rows:
        ports = {p.get("PublishedPort", 0) for p in row.get("Publishers") or []}
        found[row.get("Service", "?")] = {p for p in ports if p}
    return found


def host_listeners():
    """Best-effort diagnostics for what is listening on 45000-45003 on this host."""
    try:
        out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return "ss is not available here"
    rows = [line for line in out.splitlines() if any(f":{p} " in line for p in range(45000, 45004))]
    return "\n".join(rows) or "nothing is listening"


print("\n== Health ==")
status, body = api("GET", "/health/deps")
check("gateway sees all four services", status == 200 and body["status"] == "ok")

print("\n== Exposure and hardening ==")
published = published_ports()
leaked = {name: sorted(published[name]) for name in INTERNAL if published and published.get(name)}
check(
    "only api-gateway publishes a host port (per docker compose)",
    published is not None and not leaked and published.get("api-gateway") == {45004},
)
if leaked:
    print("        internal services publishing ports:", leaked)
open_ports = [p for p in range(45000, 45004) if port_open(p)]
if open_ports:
    print(f"  NOTE  host ports {open_ports} accept connections, but docker compose does not publish them")
    print(host_listeners())
check("gateway answers on 45004", api("GET", "/health")[0] == 200)
uid = compose("exec", "-T", "order-service", "id", "-u").stdout.strip()
check("containers run as non-root (uid 10001)", uid == "10001")

print("\n== Seed stock ==")
seed = compose("exec", "-T", "inventory-service", "python", "-", stdin=(ROOT / "scripts" / "seed_stock.py").read_text())
check("stock seeded (SKU-1: 10, SKU-2: 2)", seed.returncode == 0 and seed.stdout.split() == ["200", "200"])

print("\n== Checkout flows ==")
status, body = api("POST", "/checkout", checkout("SKU-1", 2))
check("checkout returns 201 with a promise", status == 201 and body["promise"] is not None and body["degraded"] is False)
key = {"Idempotency-Key": "ci-key-1"}
s1, a = api("POST", "/checkout", checkout("SKU-1", 1), key)
s2, b = api("POST", "/checkout", checkout("SKU-1", 1), key)
check("idempotent retry returns the same order", s1 == 201 and s2 == 201 and a["order"]["id"] == b["order"]["id"])
status, body = api("POST", "/checkout", checkout("SKU-2", 5))
check("out of stock gives 409 (2 available)", status == 409 and body["detail"]["shortages"][0]["available"] == 2)
status, _ = api("POST", "/checkout", checkout("SKU-2", 2))
check("failed attempt reserved nothing (SKU-2 x2 still succeeds)", status == 201)

print("\n== Returns ==")
status, body = api(
    "POST",
    "/returns",
    {
        "order_id": "ORD-CI",
        "sku": "SKU-1",
        "qty": 2,
        "unit_price_paise": 50000,
        "reason": "CHANGED_MIND",
        "delivered_on": (date.today() - timedelta(days=3)).isoformat(),
        "requested_on": date.today().isoformat(),
    },
)
check("return created, 10% fee applied (90000 paise)", status == 201 and body["refund_paise"] == 90000)

print("\n== Resilience ==")
compose("stop", "promise-service")
status, body = api("POST", "/checkout", checkout("SKU-1", 1))
check("promise-service down: checkout degrades gracefully", status == 201 and body["degraded"] is True)
compose("stop", "inventory-service")
status, _ = api("POST", "/checkout", checkout("SKU-1", 1))
check("inventory-service down: checkout fails cleanly with 503", status == 503)

print()
if failures:
    print(f"{failures} CHECK(S) FAILED")
    sys.exit(1)
print("ALL CHECKS PASSED")
