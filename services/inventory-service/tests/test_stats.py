import pytest
from app.main import app, store
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    store.reset()


def test_stats_endpoint_counts_reservations():
    client.put("/stock/SKU-S", json={"region": "south", "on_hand": 5})
    for order_id in ("ORD-S1", "ORD-S2"):
        body = {
            "order_id": order_id,
            "region": "south",
            "items": [{"sku": "SKU-S", "qty": 1}],
        }
        client.post("/reservations", json=body)
    client.post("/reservations/ORD-S2/release")
    assert client.get("/stats").json() == {
        "active_reservations": 1,
        "released_reservations": 1,
        "stock_records": 1,
    }
