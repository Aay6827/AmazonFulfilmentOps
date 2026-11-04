import pytest
from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)

PAYLOAD = {
    "customer_id": "C100",
    "region": "south",
    "items": [{"sku": "SKU-1", "qty": 2}],
}


@pytest.fixture(autouse=True)
def clean_store():
    store.reset()


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["service"] == "order-service"


def test_create_order():
    r = client.post("/orders", json=PAYLOAD)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "CREATED"
    assert body["id"].startswith("ORD-")


def test_idempotent_replay_returns_same_order():
    headers = {"Idempotency-Key": "abc-123"}
    first = client.post("/orders", json=PAYLOAD, headers=headers)
    second = client.post("/orders", json=PAYLOAD, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_validation_rejects_empty_items_and_bad_qty():
    assert client.post("/orders", json={**PAYLOAD, "items": []}).status_code == 422
    bad = {**PAYLOAD, "items": [{"sku": "S", "qty": 0}]}
    assert client.post("/orders", json=bad).status_code == 422


def test_get_order_and_404():
    oid = client.post("/orders", json=PAYLOAD).json()["id"]
    assert client.get(f"/orders/{oid}").status_code == 200
    assert client.get("/orders/ORD-MISSING").status_code == 404


def test_cancel_then_double_cancel_conflicts():
    oid = client.post("/orders", json=PAYLOAD).json()["id"]
    r = client.post(f"/orders/{oid}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "CANCELLED"
    assert client.post(f"/orders/{oid}/cancel").status_code == 409


def test_cancel_unknown_order_404():
    assert client.post("/orders/ORD-MISSING/cancel").status_code == 404


def test_metrics_endpoint_exposes_counters():
    client.post("/orders", json=PAYLOAD)
    text = client.get("/metrics").text
    assert "orders_created_total" in text
    assert "order_http_requests_total" in text
