"""Database drill: restart order-service and take PostgreSQL down, under load.

Needs the docker compose stack running with monitoring (see incident_drill.py).
Usage: python scripts/incident_drill_db.py
Writes docs/drill-results-db.json
"""
import json
import sys
import threading
import time

import incident_drill as drill


def stats(service, port):
    code = (
        "import urllib.request; "
        f"print(urllib.request.urlopen('http://127.0.0.1:{port}/stats', timeout=5).read().decode())"
    )
    out = drill.compose("exec", "-T", service, "python", "-c", code)
    if out.returncode != 0:
        return None
    return json.loads(out.stdout)


def reconcile(label):
    orders = stats("order-service", 45000)
    inventory = stats("inventory-service", 45001)
    if orders is None or inventory is None:
        drill.log(f"{label}: stats unavailable")
        return None
    live = orders["live_orders"]
    active = inventory["active_reservations"]
    result = {
        "live_orders": live,
        "active_reservations": active,
        "orders_without_reservation": max(live - active, 0),
        "reservations_without_order": max(active - live, 0),
    }
    drill.log(f"{label}: {result}")
    return result


def timed_checkout():
    started = time.time()
    status = drill.checkout("SKU-1", 1)
    return status, round(time.time() - started, 2)


def resolved_map(wanted, since, timeout):
    resolved = drill.wait_resolved(wanted, since, timeout)
    return {drill.fmt(key): secs for key, secs in resolved.items()}


def main():
    if drill.prom("sum(up)") is None:
        sys.exit("Prometheus is not reachable on 45090. Start the monitoring stack first.")
    if not drill.seed():
        sys.exit("Could not seed stock. Is the stack running?")

    load = drill.Load()
    threading.Thread(target=load.run, daemon=True).start()
    results = {"incidents": {}}

    try:
        load.phase = "baseline"
        drill.log("baseline traffic (30s)")
        time.sleep(30)
        results["reconcile_baseline"] = reconcile("baseline")

        # ---- Incident 3: order-service down ----
        wanted = [("ServiceDown", "order-service"), ("CheckoutUnavailable", "")]
        block = drill.incident(load, "order_down", "order-service", wanted, 90)
        load.phase = "order_recovery"
        drill.compose("start", "order-service")
        since = time.time()
        drill.log("RECOVERY: order-service started")
        block["healthy_after_s"] = drill.wait_healthy(60)
        block["resolved_after_s"] = resolved_map(wanted, since, 180)
        block["reconcile_after"] = reconcile("after order-service restart")
        results["incidents"]["incident_3_order_service_down"] = block

        load.phase = "steady"
        drill.log("steady state (20s)")
        time.sleep(20)

        # ---- Incident 4: PostgreSQL down ----
        before = reconcile("before PostgreSQL outage")
        wanted = [("CheckoutUnavailable", ""), ("GatewayHighErrorRate", "")]
        block = drill.incident(load, "postgres_down", "postgres", wanted, 150)
        block["service_down_alerts_firing"] = sorted(
            drill.fmt(key) for key in drill.firing() if key[0] == "ServiceDown"
        )
        probes = [timed_checkout() for _ in range(3)]
        block["probe_checkouts_during_outage"] = [
            {"status": status, "seconds": secs} for status, secs in probes
        ]
        drill.log(f"probe checkouts during the outage: {probes}")

        load.phase = "postgres_recovery"
        drill.compose("start", "postgres")
        since = time.time()
        drill.log("RECOVERY: postgres started")
        recovered = None
        while time.time() - since < 180:
            status, _ = timed_checkout()
            if status == 201:
                recovered = round(time.time() - since, 1)
                break
            time.sleep(1)
        block["first_successful_checkout_after_s"] = recovered
        drill.log(f"first successful checkout after {recovered}s")
        waited = drill.wait_healthy(120)
        block["healthy_after_s"] = None if waited is None else round(time.time() - since, 1)
        block["resolved_after_s"] = resolved_map(wanted, since, 240)
        block["reconcile_before"] = before
        block["reconcile_after"] = reconcile("after PostgreSQL outage")
        results["incidents"]["incident_4_postgres_down"] = block
    finally:
        load.running = False
        drill.compose("start", "postgres", "order-service")

    time.sleep(8)
    results["final"] = {
        "compensation_failed": drill.prom("sum(gateway_compensation_failed_total)"),
        "checkouts_success": drill.prom('sum(gateway_checkouts_total{outcome="success"})'),
        "checkouts_unavailable": drill.prom('sum(gateway_checkouts_total{outcome="unavailable"})'),
        "reconcile": reconcile("final"),
    }
    results["client_counts_by_phase"] = {
        phase: {str(code): n for code, n in sorted(counter.items())}
        for phase, counter in load.counts.items()
    }
    results["events"] = drill.events
    results["total_seconds"] = round(time.time() - drill.T0, 1)

    out = drill.ROOT / "docs" / "drill-results-db.json"
    out.write_text(json.dumps(results, indent=2), encoding="ascii")

    print("\n==== DATABASE DRILL SUMMARY ====")
    for name, block in results["incidents"].items():
        print(f"\n{name}")
        for key, secs in block["detected_after_s"].items():
            print(f"  fired    {key:<42} after {secs}s")
        for key in block["not_fired"]:
            print(f"  MISSING  {key:<42} never fired")
        print(f"  healthy again {block['healthy_after_s']}s after restart")
        for key, secs in block["resolved_after_s"].items():
            print(f"  resolved {key:<42} {secs}s after restart")
        for key in (
            "service_down_alerts_firing",
            "probe_checkouts_during_outage",
            "first_successful_checkout_after_s",
            "reconcile_before",
            "reconcile_after",
        ):
            if key in block:
                print(f"  {key}: {block[key]}")
    print("\nclient-side HTTP status counts per phase")
    for phase, counts in results["client_counts_by_phase"].items():
        print(f"  {phase:<30} {counts}")
    print("\nfinal")
    for key, val in results["final"].items():
        print(f"  {key:<24} {val}")
    print(f"\nwrote {out} ({results['total_seconds']}s total)")


if __name__ == "__main__":
    main()
