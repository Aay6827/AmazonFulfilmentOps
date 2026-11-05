import json
import urllib.request

for region in ("south", "west", "north", "east", "remote"):
    for sku, qty in (("SKU-1", 5000), ("SKU-2", 2)):
        req = urllib.request.Request(
            f"http://127.0.0.1:45001/stock/{sku}",
            data=json.dumps({"region": region, "on_hand": qty}).encode(),
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        print(urllib.request.urlopen(req).status)
