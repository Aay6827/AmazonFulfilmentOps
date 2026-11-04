"""Generate checkout traffic. Usage: python scripts/traffic.py [seconds] [rps]"""
import json
import random
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

GW = "http://127.0.0.1:45004"


def checkout(sku, qty):
    body = json.dumps(
        {
            "customer_id": "C-LOAD",
            "region": random.choice(["south", "west", "north", "east", "remote"]),
            "service_level": random.choice(["standard", "express"]),
            "items": [{"sku": sku, "qty": qty}],
        }
    ).encode()
    req = urllib.request.Request(
        GW + "/checkout",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    rps = float(sys.argv[2]) if len(sys.argv) > 2 else 5
    counts = Counter()
    end = time.time() + seconds
    while time.time() < end:
        started = time.time()
        sku, qty = ("SKU-2", 5) if random.random() < 0.1 else ("SKU-1", 1)
        counts[checkout(sku, qty)] += 1
        time.sleep(max(0.0, 1.0 / rps - (time.time() - started)))
    print("status counts:", dict(sorted(counts.items())))


if __name__ == "__main__":
    main()
