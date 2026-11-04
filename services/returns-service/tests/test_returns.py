import pytest
from app.main import app, store
from fastapi.testclient import TestClient

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_store():
    store.reset()


def create(**over):
    payload = {
        "order_id": "ORD-1",
        "sku": "SKU-1",
        "qty": 1,
        "unit_price_paise": 50000,
        "reason": "DEFECTIVE",
        "delivered_on": "2026-11-01",
        "requested_on": "2026-11-04",
    }
    payload.update(over)
    return client.post("/returns", json=payload)


def test_health():
    assert client.get("/health").json()["service"] == "returns-service"


def test_defective_return_gets_full_refund():
    r = create(qty=2)
    assert r.status_code == 201
    b = r.json()
    assert b["status"] == "REQUESTED"
    assert b["gross_paise"] == 100000
    assert b["restocking_fee_paise"] == 0
    assert b["refund_paise"] == 100000
    assert b["id"].startswith("RET-")


def test_changed_mind_deducts_restocking_fee():
    b = create(reason="CHANGED_MIND", qty=2).json()
    assert b["gross_paise"] == 100000
    assert b["restocking_fee_paise"] == 10000
    assert b["refund_paise"] == 90000


def test_return_window_boundary():
    # delivered 2026-10-01: day 30 is allowed, day 31 is not
    ok = create(delivered_on="2026-10-01", requested_on="2026-10-31")
    assert ok.status_code == 201
    late = create(
        order_id="ORD-2", delivered_on="2026-10-01", requested_on="2026-11-01"
    )
    assert late.status_code == 422
    assert "expired" in late.json()["detail"]


def test_requested_before_delivered_rejected():
    r = create(delivered_on="2026-11-05", requested_on="2026-11-04")
    assert r.status_code == 422


def test_duplicate_active_return_conflicts():
    assert create(order_id="ORD-3").status_code == 201
    assert create(order_id="ORD-3").status_code == 409


def test_new_return_allowed_after_rejection():
    rid = create(order_id="ORD-4").json()["id"]
    assert client.post(f"/returns/{rid}/reject").status_code == 200
    assert create(order_id="ORD-4").status_code == 201


def test_happy_path_to_refund_with_history():
    rid = create().json()["id"]
    for action, expected in [
        ("approve", "APPROVED"),
        ("receive", "RECEIVED"),
        ("refund", "REFUNDED"),
    ]:
        r = client.post(f"/returns/{rid}/{action}")
        assert r.status_code == 200
        assert r.json()["status"] == expected
    final = client.get(f"/returns/{rid}").json()
    assert [e["status"] for e in final["history"]] == [
        "REQUESTED", "APPROVED", "RECEIVED", "REFUNDED",
    ]


def test_invalid_transition_conflicts():
    rid = create().json()["id"]
    r = client.post(f"/returns/{rid}/refund")
    assert r.status_code == 409
    assert client.get(f"/returns/{rid}").json()["status"] == "REQUESTED"


def test_rejected_return_cannot_be_approved():
    rid = create().json()["id"]
    assert client.post(f"/returns/{rid}/reject").status_code == 200
    assert client.post(f"/returns/{rid}/approve").status_code == 409


def test_reject_after_receive_allowed_for_failed_inspection():
    rid = create().json()["id"]
    client.post(f"/returns/{rid}/approve")
    client.post(f"/returns/{rid}/receive")
    r = client.post(f"/returns/{rid}/reject")
    assert r.status_code == 200
    assert r.json()["status"] == "REJECTED"


def test_unknown_return_404():
    assert client.get("/returns/RET-NOPE").status_code == 404
    assert client.post("/returns/RET-NOPE/approve").status_code == 404


def test_unknown_action_422():
    rid = create().json()["id"]
    assert client.post(f"/returns/{rid}/explode").status_code == 422


def test_list_filters_by_order():
    create(order_id="ORD-A", sku="SKU-1")
    create(order_id="ORD-A", sku="SKU-2")
    create(order_id="ORD-B", sku="SKU-1")
    assert len(client.get("/returns").json()) == 3
    assert len(client.get("/returns", params={"order_id": "ORD-A"}).json()) == 2


def test_validation_rejects_bad_qty_and_reason():
    assert create(qty=0).status_code == 422
    assert create(reason="BORED").status_code == 422


def test_metrics_endpoint():
    rid = create().json()["id"]
    for a in ("approve", "receive", "refund"):
        client.post(f"/returns/{rid}/{a}")
    text = client.get("/metrics").text
    assert "returns_created_total" in text
    assert "returns_refunded_paise_total" in text
