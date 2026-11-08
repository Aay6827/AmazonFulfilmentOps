"""Incident drill: inject real failures under load and record how monitoring reacts.

Needs the stack running with monitoring:
  docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d --wait
Usage: python scripts/incident_drill.py
"""
import json
import random
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

GW = "http://127.0.0.1:45004"
PROM = "http://127.0.0.1:45090"
ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.monitoring.yml"]
T0 = time.time()
events = []


def log(msg):
    now = datetime.now()
    stamp = round(time.time() - T0, 1)
    events.append({"t_seconds": stamp, "clock": now.strftime("%H:%M:%S"), "event": msg})
    print(f"[{stamp:7.1f}s {now:%H:%M:%S}] {msg}", flush=True)


def compose(*args, stdin=None):
    return subprocess.run(
        COMPOSE + list(args), cwd=ROOT, input=stdin, capture_output=True, text=True, check=False
    )


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def checkout(sku, qty):
    body = json.dumps(
        {
            "customer_id": "C-DRILL",
            "region": random.choice(["south", "west", "north", "east", "remote"]),
            "service_level": random.choice(["standard", "express"]),
            "items": [{"sku": sku, "qty": qty}],
        }
    ).encode()
    req = urllib.request.Request(
        GW + "/checkout", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def prom(query):
    url = PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": query})
    try:
        result = http_json(url)["data"]["result"]
    except (OSError, KeyError, ValueError):
        return None
    return float(result[0]["value"][1]) if result else 0.0


def firing():
    try:
        alerts = http_json(PROM + "/api/v1/alerts")["data"]["alerts"]
    except (OSError, KeyError, ValueError):
        return set()
    return {
        (a["labels"]["alertname"], a["labels"].get("job", ""))
        for a in alerts
        if a["state"] == "firing"
    }


def fmt(key):
    return f"{key[0]}({key[1]})" if key[1] else key[0]


def wait_firing(wanted, since, timeout):
    seen = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(seen) < len(wanted):
        now = firing()
        for key in wanted:
            if key not in seen and key in now:
                seen[key] = round(time.time() - since, 1)
                log(f"ALERT FIRING   {fmt(key)} after {seen[key]}s")
        time.sleep(2)
    return seen


def wait_resolved(wanted, since, timeout):
    seen = {}
    deadline = time.time() + timeout
    while time.time() < deadline and len(seen) < len(wanted):
        now = firing()
        for key in wanted:
            if key not in seen and key not in now:
                seen[key] = round(time.time() - since, 1)
                log(f"ALERT RESOLVED {fmt(key)} after {seen[key]}s")
        time.sleep(2)
    return seen


def wait_healthy(timeout):
    start = time.time()
    while time.time() - start < timeout:
        try:
            if http_json(GW + "/health/deps")["status"] == "ok":
                return round(time.time() - start, 1)
        except (OSError, KeyError, ValueError):
            pass
        time.sleep(1)
    return None


def seed():
    script = (ROOT / "scripts" / "seed_regions.py").read_text()
    out = compose("exec", "-T", "inventory-service", "python", "-", stdin=script)
    return out.returncode == 0


class Load:
    def __init__(self, rps=5.0):
        self.rps = rps
        self.phase = "setup"
        self.counts = defaultdict(Counter)
        self.running = True

    def run(self):
        while self.running:
            started = time.time()
            sku, qty = ("SKU-2", 5) if random.random() < 0.1 else ("SKU-1", 1)
            self.counts[self.phase][checkout(sku, qty)] += 1
            time.sleep(max(0.0, 1.0 / self.rps - (time.time() - started)))


def incident(load, phase, service, wanted, fire_timeout):
    load.phase = phase
    compose("stop", service)
    since = time.time()
    log(f"INJECTED: {service} stopped")
    fired = wait_firing(wanted, since, fire_timeout)
    time.sleep(10)
    return {
        "service": service,
        "detected_after_s": {fmt(k): v for k, v in fired.items()},
        "not_fired": [fmt(k) for k in wanted if k not in fired],
    }


def inventory_stats():
    code = (
        "import urllib.request; "
        "print(urllib.request.urlopen('http://127.0.0.1:45001/stats', timeout=5).read().decode())"
    )
    out = compose("exec", "-T", "inventory-service", "python", "-c", code)
    if out.returncode != 0:
        raise SystemExit("could not read inventory stats: " + out.stderr)
    return json.loads(out.stdout)


def main():
    if prom("sum(up)") is None:
        sys.exit("Prometheus is not reachable on 45090. Start the monitoring stack first.")
    if not seed():
        sys.exit("Could not seed stock. Is the stack running?")

    load = Load()
    threading.Thread(target=load.run, daemon=True).start()
    results = {"incidents": {}}

    try:
        load.phase = "baseline"
        log("baseline traffic (30s)")
        time.sleep(30)

        # ---- Incident 1: promise-service down ----
        wanted1 = [("ServiceDown", "promise-service"), ("CheckoutDegraded", "")]
        block1 = incident(load, "promise_down", "promise-service", wanted1, 90)
        load.phase = "promise_recovery"
        compose("start", "promise-service")
        since = time.time()
        log("RECOVERY: promise-service started")
        block1["healthy_after_s"] = wait_healthy(60)
        resolved = wait_resolved(wanted1, since, 180)
        block1["resolved_after_s"] = {fmt(k): v for k, v in resolved.items()}
        results["incidents"]["incident_1_promise_service_down"] = block1

        load.phase = "baseline_2"
        log("steady state (15s)")
        time.sleep(15)

        # ---- Incident 2: inventory-service down ----
        wanted2 = [
            ("ServiceDown", "inventory-service"),
            ("CheckoutUnavailable", ""),
            ("GatewayHighErrorRate", ""),
        ]
        block2 = incident(load, "inventory_down", "inventory-service", wanted2, 150)
        load.phase = "inventory_restarted_unseeded"
        compose("start", "inventory-service")
        since = time.time()
        log("RECOVERY: inventory-service started (in-memory state is gone)")
        block2["healthy_after_s"] = wait_healthy(60)
        time.sleep(15)
        probe = checkout("SKU-1", 1)
        block2["probe_status_before_reseed"] = probe
        log(f"probe checkout before reseeding returned HTTP {probe}")
        load.phase = "reseeded"
        seed()
        log("stock reseeded")
        time.sleep(15)
        resolved = wait_resolved(wanted2, since, 180)
        block2["resolved_after_s"] = {fmt(k): v for k, v in resolved.items()}
        results["incidents"]["incident_2_inventory_service_down"] = block2
    finally:
        load.running = False
        compose("start", "promise-service", "inventory-service")

    time.sleep(8)
    final = {
        "orders_created": prom("sum(orders_created_total)"),
        "orders_cancelled": prom("sum(orders_cancelled_total)"),
        "checkouts_success": prom('sum(gateway_checkouts_total{outcome="success"})'),
        "checkouts_degraded": prom('sum(gateway_checkouts_total{outcome="degraded"})'),
        "checkouts_out_of_stock": prom('sum(gateway_checkouts_total{outcome="out_of_stock"})'),
        "checkouts_unavailable": prom('sum(gateway_checkouts_total{outcome="unavailable"})'),
        "compensation_failed": prom("sum(gateway_compensation_failed_total)"),
        "inventory_reserved_since_restart": prom('sum(inventory_reservations_total{outcome="reserved"})'),
        "inventory_released_since_restart": prom('sum(inventory_reservations_total{outcome="released"})'),
    }
    if all(v is not None for v in final.values()):
        live_orders = final["orders_created"] - final["orders_cancelled"]
        live_reservations = inventory_stats()["active_reservations"]
        final["derived"] = {
            "no_phantom_orders": abs(
                live_orders - (final["checkouts_success"] + final["checkouts_degraded"])
            )
            < 0.5,
            "live_orders": live_orders,
            "active_reservations_in_inventory": live_reservations,
            "orders_without_reservation": live_orders - live_reservations,
        }
    results["final_metrics"] = final
    results["client_counts_by_phase"] = {
        phase: {str(code): n for code, n in sorted(counter.items())}
        for phase, counter in load.counts.items()
    }
    results["events"] = events
    results["total_seconds"] = round(time.time() - T0, 1)

    out = ROOT / "docs" / "drill-results.json"
    out.write_text(json.dumps(results, indent=2), encoding="ascii")

    print("\n==== DRILL SUMMARY ====")
    for name, block in results["incidents"].items():
        print(f"\n{name}")
        for key, secs in block["detected_after_s"].items():
            print(f"  fired    {key:<42} after {secs}s")
        for key in block["not_fired"]:
            print(f"  MISSING  {key:<42} never fired")
        print(f"  healthy again {block['healthy_after_s']}s after restart")
        for key, secs in block["resolved_after_s"].items():
            print(f"  resolved {key:<42} {secs}s after restart")
        if "probe_status_before_reseed" in block:
            print(f"  checkout before reseed returned HTTP {block['probe_status_before_reseed']}")
    print("\nclient-side HTTP status counts per phase")
    for phase, counts in results["client_counts_by_phase"].items():
        print(f"  {phase:<30} {counts}")
    print("\nfinal metrics")
    for key, val in final.items():
        print(f"  {key:<36} {val}")
    print(f"\nwrote {out} ({results['total_seconds']}s total)")


if __name__ == "__main__":
    main()
