import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from app.models import ReserveItem, ReserveRequest
from app.store import InsufficientStock

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    store.reset()


def seed(sku="SKU-1", region="south", on_hand=10):
    return client.put(f"/stock/{sku}", json={"region": region, "on_hand": on_hand})


def reserve(order_id, items, region="south"):
    return client.post(
        "/reservations",
        json={"order_id": order_id, "region": region, "items": items},
    )


def test_health():
    assert client.get("/health").json()["service"] == "inventory-service"


def test_set_and_get_stock():
    r = seed(on_hand=10)
    assert r.status_code == 200
    body = client.get("/stock/SKU-1", params={"region": "south"}).json()
    assert body == {
        "sku": "SKU-1", "region": "south",
        "on_hand": 10, "reserved": 0, "available": 10,
    }


def test_get_unknown_stock_404():
    r = client.get("/stock/NOPE", params={"region": "south"})
    assert r.status_code == 404


def test_reserve_reduces_available():
    seed(on_hand=10)
    r = reserve("ORD-1", [{"sku": "SKU-1", "qty": 4}])
    assert r.status_code == 201
    assert r.json()["status"] == "RESERVED"
    view = client.get("/stock/SKU-1", params={"region": "south"}).json()
    assert view["reserved"] == 4
    assert view["available"] == 6


def test_insufficient_stock_is_all_or_nothing():
    seed("SKU-1", on_hand=10)
    seed("SKU-2", on_hand=1)
    r = reserve("ORD-2", [{"sku": "SKU-1", "qty": 3}, {"sku": "SKU-2", "qty": 5}])
    assert r.status_code == 409
    shortages = r.json()["detail"]["shortages"]
    assert shortages == [{"sku": "SKU-2", "requested": 5, "available": 1}]
    first = client.get("/stock/SKU-1", params={"region": "south"}).json()
    assert first["reserved"] == 0


def test_reserve_unknown_sku_is_insufficient():
    r = reserve("ORD-3", [{"sku": "GHOST", "qty": 1}])
    assert r.status_code == 409


def test_duplicate_skus_in_one_request_are_aggregated():
    seed(on_hand=5)
    r = reserve("ORD-4", [{"sku": "SKU-1", "qty": 3}, {"sku": "SKU-1", "qty": 3}])
    assert r.status_code == 409
    ok = reserve("ORD-5", [{"sku": "SKU-1", "qty": 2}, {"sku": "SKU-1", "qty": 2}])
    assert ok.status_code == 201
    assert ok.json()["items"] == [{"sku": "SKU-1", "qty": 4}]


def test_reserve_is_idempotent_by_order_id():
    seed(on_hand=10)
    first = reserve("ORD-6", [{"sku": "SKU-1", "qty": 4}])
    second = reserve("ORD-6", [{"sku": "SKU-1", "qty": 4}])
    assert first.status_code == 201
    assert second.status_code == 200
    view = client.get("/stock/SKU-1", params={"region": "south"}).json()
    assert view["reserved"] == 4


def test_release_restores_stock_and_double_release_conflicts():
    seed(on_hand=10)
    reserve("ORD-7", [{"sku": "SKU-1", "qty": 4}])
    r = client.post("/reservations/ORD-7/release")
    assert r.status_code == 200
    assert r.json()["status"] == "RELEASED"
    view = client.get("/stock/SKU-1", params={"region": "south"}).json()
    assert view["available"] == 10
    assert client.post("/reservations/ORD-7/release").status_code == 409


def test_release_unknown_404():
    assert client.post("/reservations/NOPE/release").status_code == 404


def test_cannot_set_on_hand_below_reserved():
    seed(on_hand=10)
    reserve("ORD-8", [{"sku": "SKU-1", "qty": 6}])
    assert seed(on_hand=5).status_code == 409


def test_concurrent_reservations_never_oversell():
    store.set_stock("SKU-9", "south", 10)
    results = []

    def worker(i):
        req = ReserveRequest(
            order_id=f"ORD-C{i}",
            region="south",
            items=[ReserveItem(sku="SKU-9", qty=1)],
        )
        try:
            store.reserve(req)
            results.append("ok")
        except InsufficientStock:
            results.append("fail")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 10
    assert results.count("fail") == 30
    assert store.get_stock("SKU-9", "south").available == 0


def test_metrics_endpoint():
    seed(on_hand=1)
    reserve("ORD-M", [{"sku": "SKU-1", "qty": 1}])
    text = client.get("/metrics").text
    assert "inventory_reservations_total" in text
