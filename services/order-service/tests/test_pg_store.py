import os
import threading

import pytest
from app.models import OrderCreate, OrderItem

DSN = os.getenv("TEST_DATABASE_URL")
REQUIRED = os.getenv("REQUIRE_PG_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not DSN and not REQUIRED, reason="TEST_DATABASE_URL not set"
)


@pytest.fixture
def store():
    from app.pg_store import PgOrderStore

    s = PgOrderStore(DSN)
    s.reset()
    yield s
    s.close()


def payload(items=None):
    return OrderCreate(
        customer_id="C100",
        region="south",
        items=items or [OrderItem(sku="SKU-1", qty=2)],
    )


def test_create_and_get_roundtrip_keeps_item_order_and_duplicates(store):
    items = [
        OrderItem(sku="SKU-1", qty=2),
        OrderItem(sku="SKU-2", qty=1),
        OrderItem(sku="SKU-1", qty=3),
    ]
    order, created = store.create(payload(items), None)
    assert created is True
    assert order.id.startswith("ORD-")
    assert order.status.value == "CREATED"
    assert store.get(order.id) == order
    assert [(i.sku, i.qty) for i in order.items] == [("SKU-1", 2), ("SKU-2", 1), ("SKU-1", 3)]
    assert store.get("ORD-MISSING") is None


def test_idempotent_replay_returns_same_order(store):
    first, first_created = store.create(payload(), "key-1")
    second, second_created = store.create(payload(), "key-1")
    assert (first_created, second_created) == (True, False)
    assert first.id == second.id
    assert store.stats()["live_orders"] == 1


def test_concurrent_requests_with_one_key_create_one_order(store):
    results = []

    def worker():
        order, created = store.create(payload(), "same-key")
        results.append((order.id, created))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 10
    assert len({order_id for order_id, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1
    assert store.stats()["live_orders"] == 1


def test_cancel_then_double_cancel_conflicts(store):
    order, _ = store.create(payload(), None)
    assert store.cancel(order.id).status.value == "CANCELLED"
    with pytest.raises(ValueError):
        store.cancel(order.id)
    assert store.cancel("ORD-MISSING") is None
    assert store.stats() == {"live_orders": 0, "cancelled_orders": 1}


def test_state_survives_a_new_store_instance(store):
    from app.pg_store import PgOrderStore

    order, _ = store.create(payload(), "key-restart")
    restarted = PgOrderStore(DSN)
    try:
        assert restarted.get(order.id) == order
        again, created = restarted.create(payload(), "key-restart")
        assert created is False
        assert again.id == order.id
        assert restarted.cancel(order.id).status.value == "CANCELLED"
    finally:
        restarted.close()


def test_stats_counts_live_and_cancelled(store):
    first, _ = store.create(payload(), None)
    store.create(payload(), None)
    store.create(payload(), None)
    store.cancel(first.id)
    assert store.stats() == {"live_orders": 2, "cancelled_orders": 1}


def test_ping(store):
    assert store.ping() is True
