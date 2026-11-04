import json
import sys
import urllib.request

sku1 = int(sys.argv[1]) if len(sys.argv) > 1 else 10
for sku, qty in (("SKU-1", sku1), ("SKU-2", 2)):
    req = urllib.request.Request(
        f"http://127.0.0.1:45001/stock/{sku}",
        data=json.dumps({"region": "south", "on_hand": qty}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    print(urllib.request.urlopen(req).status)
