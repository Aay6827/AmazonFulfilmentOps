"""Alert check: take PostgreSQL down, then quick-restart a service, and time the alerts.

Needs the docker compose stack running with monitoring (see incident_drill.py).
Usage: python scripts/alert_check.py
Writes docs/alert-check.json
"""
import json
import sys
import threading
import time

import incident_drill as drill


def alerts_loaded():
    groups = drill.http_json(drill.PROM + "/api/v1/rules")["data"]["groups"]
    return {
        rule["name"]
        for group in groups
        for rule in group["rules"]
        if rule["type"] == "alerting"
    }


def main():
    if drill.prom("sum(up)") is None:
        sys.exit("Prometheus is not reachable on 45090. Start the monitoring stack first.")
    missing = {"DatabaseUnreachable", "ServiceRestarted"} - alerts_loaded()
    if missing:
        sys.exit(f"Alert rules not loaded: {sorted(missing)}. Recreate the stack.")
    if not drill.seed():
        sys.exit("Could not seed stock. Is the stack running?")

    load = drill.Load()
    threading.Thread(target=load.run, daemon=True).start()
    results = {}

    try:
        load.phase = "baseline"
        drill.log("baseline traffic (20s)")
        time.sleep(20)

        # ---- Scenario A: PostgreSQL down ----
        wanted = [
            ("DatabaseUnreachable", "order-service"),
            ("DatabaseUnreachable", "inventory-service"),
            ("CheckoutUnavailable", ""),
        ]
        block = drill.incident(load, "postgres_down", "postgres", wanted, 120)
        load.phase = "postgres_recovery"
        drill.compose("start", "postgres")
        since = time.time()
        drill.log("RECOVERY: postgres started")
        recovered = None
        while time.time() - since < 120:
            if drill.checkout("SKU-1", 1) == 201:
                recovered = round(time.time() - since, 1)
                break
            time.sleep(1)
        block["first_successful_checkout_after_s"] = recovered
        resolved = drill.wait_resolved(wanted, since, 240)
        block["resolved_after_s"] = {drill.fmt(key): secs for key, secs in resolved.items()}
        results["postgres_outage"] = block

        # ---- Scenario B: quick restart ----
        load.phase = "quick_restart"
        drill.compose("restart", "inventory-service")
        since = time.time()
        drill.log("RESTARTED: inventory-service (quick restart)")
        fired = drill.wait_firing([("ServiceRestarted", "inventory-service")], since, 60)
        time.sleep(20)
        results["quick_restart"] = {
            "service_restarted_after_s": {drill.fmt(key): secs for key, secs in fired.items()},
            "service_down_fired": sorted(
                drill.fmt(key) for key in drill.firing() if key[0] == "ServiceDown"
            ),
        }
    finally:
        load.running = False
        drill.compose("start", "postgres", "inventory-service")

    results["events"] = drill.events
    out = drill.ROOT / "docs" / "alert-check.json"
    out.write_text(json.dumps(results, indent=2), encoding="ascii")

    outage = results["postgres_outage"]
    print("\n==== ALERT CHECK SUMMARY ====")
    print("\nPostgreSQL outage")
    for key, secs in outage["detected_after_s"].items():
        print(f"  fired    {key:<44} after {secs}s")
    for key in outage["not_fired"]:
        print(f"  MISSING  {key:<44} never fired")
    db_times = [
        secs for key, secs in outage["detected_after_s"].items()
        if key.startswith("DatabaseUnreachable")
    ]
    checkout_time = outage["detected_after_s"].get("CheckoutUnavailable")
    if db_times and checkout_time is not None:
        gap = round(checkout_time - min(db_times), 1)
        print(f"  DatabaseUnreachable fired {gap}s before CheckoutUnavailable (polling resolution about 2s)")
    print(f"  first successful checkout {outage['first_successful_checkout_after_s']}s after PostgreSQL restarted")
    for key, secs in outage["resolved_after_s"].items():
        print(f"  resolved {key:<44} {secs}s after restart")

    quick = results["quick_restart"]
    print("\nQuick inventory-service restart")
    print(f"  ServiceRestarted: {quick['service_restarted_after_s'] or 'NEVER FIRED'}")
    print(f"  ServiceDown firing afterwards: {quick['service_down_fired'] or 'none'}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
