import os
import threading

import pytest
from app.models import ReserveItem, ReserveRequest
from app.store import InsufficientStock

DSN = os.getenv("TEST_DATABASE_URL")
REQUIRED = os.getenv("REQUIRE_PG_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not DSN and not REQUIRED, reason="TEST_DATABASE_URL not set"
)


@pytest.fixture
def store():
    from app.pg_store import PgStore

    s = PgStore(DSN)
    s.reset()
    yield s
    s.close()


def req(order_id, items, region="south"):
    return ReserveRequest(
        order_id=order_id,
        region=region,
        items=[ReserveItem(sku=sku, qty=qty) for sku, qty in items],
    )


def run_threads(target, count):
    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def test_set_and_get_stock(store):
    view = store.set_stock("SKU-1", "south", 10)
    assert (view.on_hand, view.reserved, view.available) == (10, 0, 10)
    assert store.get_stock("SKU-1", "south").available == 10
    assert store.get_stock("NOPE", "south") is None


def test_reserve_reduces_available(store):
    store.set_stock("SKU-1", "south", 10)
    reservation, created = store.reserve(req("ORD-1", [("SKU-1", 4)]))
    assert created is True
    assert reservation.status.value == "RESERVED"
    view = store.get_stock("SKU-1", "south")
    assert (view.reserved, view.available) == (4, 6)


def test_insufficient_stock_is_all_or_nothing(store):
    store.set_stock("SKU-1", "south", 10)
    store.set_stock("SKU-2", "south", 1)
    with pytest.raises(InsufficientStock) as exc:
        store.reserve(req("ORD-2", [("SKU-1", 3), ("SKU-2", 5)]))
    assert exc.value.shortages == [{"sku": "SKU-2", "requested": 5, "available": 1}]
    assert store.get_stock("SKU-1", "south").reserved == 0
    assert store.stats()["active_reservations"] == 0


def test_reserve_is_idempotent_by_order_id(store):
    store.set_stock("SKU-1", "south", 10)
    _, first = store.reserve(req("ORD-3", [("SKU-1", 4)]))
    _, second = store.reserve(req("ORD-3", [("SKU-1", 4)]))
    assert (first, second) == (True, False)
    assert store.get_stock("SKU-1", "south").reserved == 4


def test_release_restores_stock_and_double_release_conflicts(store):
    store.set_stock("SKU-1", "south", 10)
    store.reserve(req("ORD-4", [("SKU-1", 4)]))
    assert store.release("ORD-4").status.value == "RELEASED"
    assert store.get_stock("SKU-1", "south").available == 10
    with pytest.raises(ValueError):
        store.release("ORD-4")
    assert store.release("NOPE") is None


def test_cannot_set_on_hand_below_reserved(store):
    store.set_stock("SKU-1", "south", 10)
    store.reserve(req("ORD-5", [("SKU-1", 6)]))
    with pytest.raises(ValueError):
        store.set_stock("SKU-1", "south", 5)
    assert store.get_stock("SKU-1", "south").on_hand == 10


def test_state_survives_a_new_store_instance(store):
    from app.pg_store import PgStore

    store.set_stock("SKU-1", "south", 10)
    store.reserve(req("ORD-6", [("SKU-1", 3)]))
    restarted = PgStore(DSN)
    try:
        assert restarted.get_stock("SKU-1", "south").reserved == 3
        _, created = restarted.reserve(req("ORD-6", [("SKU-1", 3)]))
        assert created is False
        assert restarted.release("ORD-6").status.value == "RELEASED"
    finally:
        restarted.close()


def test_concurrent_reservations_never_oversell(store):
    store.set_stock("SKU-9", "south", 10)
    results = []

    def worker(i):
        try:
            store.reserve(req(f"ORD-C{i}", [("SKU-9", 1)]))
            results.append("ok")
        except InsufficientStock:
            results.append("fail")

    run_threads(worker, 40)
    assert results.count("ok") == 10
    assert results.count("fail") == 30
    assert store.get_stock("SKU-9", "south").available == 0


def test_concurrent_requests_for_one_order_reserve_once(store):
    store.set_stock("SKU-1", "south", 10)
    created = []

    def worker(_):
        _, was_created = store.reserve(req("ORD-SAME", [("SKU-1", 3)]))
        created.append(was_created)

    run_threads(worker, 10)
    assert len(created) == 10
    assert created.count(True) == 1
    assert store.get_stock("SKU-1", "south").reserved == 3


def test_stats_counts_active_and_released(store):
    store.set_stock("SKU-1", "south", 10)
    store.reserve(req("ORD-7", [("SKU-1", 1)]))
    store.reserve(req("ORD-8", [("SKU-1", 1)]))
    store.release("ORD-8")
    assert store.stats() == {
        "active_reservations": 1,
        "released_reservations": 1,
        "stock_records": 1,
    }


def test_ping(store):
    assert store.ping() is True
