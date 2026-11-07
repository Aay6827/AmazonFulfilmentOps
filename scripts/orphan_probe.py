"""Reconcile orders against reservations across an order-service restart.

Needs the docker compose stack running with stock seeded.
Usage: python scripts/orphan_probe.py
Exits 1 if orders were lost or live orders and active reservations disagree.
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

GW = "http://127.0.0.1:45004"
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.monitoring.yml"]
ORDERS = 5


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        GW + path, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


def stats(service, port):
    code = (
        "import urllib.request; "
        f"print(urllib.request.urlopen('http://127.0.0.1:{port}/stats', timeout=5).read().decode())"
    )
    out = subprocess.run(
        COMPOSE + ["exec", "-T", service, "python", "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def snapshot(label):
    live = stats("order-service", 45000)["live_orders"]
    active = stats("inventory-service", 45001)["active_reservations"]
    print(f"{label}: live orders {live}, active reservations {active}")
    return live, active


def wait_healthy():
    for _ in range(30):
        status, data = call("GET", "/health/deps")
        if status == 200 and data and data.get("status") == "ok":
            return
        time.sleep(1)


def main():
    body = {
        "customer_id": "C-PROBE",
        "region": "south",
        "service_level": "standard",
        "items": [{"sku": "SKU-1", "qty": 1}],
    }
    ids = []
    for _ in range(ORDERS):
        status, data = call("POST", "/checkout", body)
        if status != 201:
            sys.exit(f"checkout failed with HTTP {status}")
        ids.append(data["order"]["id"])

    before = snapshot("before restart")
    subprocess.run(COMPOSE + ["restart", "order-service"], check=True, capture_output=True)
    wait_healthy()

    missing = []
    for order_id in ids:
        code = 503
        for _ in range(5):
            code = call("GET", "/orders/" + order_id)[0]
            if code != 503:
                break
            time.sleep(1)
        if code != 200:
            missing.append((order_id, code))

    after = snapshot("after restart")
    print(f"orders still readable after restart: {len(ids) - len(missing)} of {len(ids)}")
    consistent = after[0] == after[1]
    print(f"live orders equal active reservations: {consistent}")
    ok = not missing and consistent and before == after
    print("PROBE PASSED" if ok else "PROBE FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
