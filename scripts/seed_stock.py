import json
import urllib.request

for sku, qty in (("SKU-1", 10), ("SKU-2", 2)):
    req = urllib.request.Request(
        f"http://127.0.0.1:45001/stock/{sku}",
        data=json.dumps({"region": "south", "on_hand": qty}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    print(urllib.request.urlopen(req).status)
