import pytest
from app.main import app, store
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    store.reset()


def test_stats_counts_live_and_cancelled_orders():
    payload = {
        "customer_id": "C1",
        "region": "south",
        "items": [{"sku": "SKU-1", "qty": 1}],
    }
    first = client.post("/orders", json=payload).json()["id"]
    client.post("/orders", json=payload)
    client.post(f"/orders/{first}/cancel")
    assert client.get("/stats").json() == {"live_orders": 1, "cancelled_orders": 1}
