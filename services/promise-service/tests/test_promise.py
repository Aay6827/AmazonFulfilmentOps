from datetime import date

from fastapi.testclient import TestClient

from app.logic import add_business_days, next_business_day
from app.main import app

client = TestClient(app)


def ask(**kwargs):
    return client.post("/promise", json=kwargs)


def test_health():
    assert client.get("/health").json()["service"] == "promise-service"


def test_standard_in_stock_same_day_ship():
    # Wed 2026-11-04 10:00, south, standard
    r = ask(region="south", order_time="2026-11-04T10:00:00")
    assert r.status_code == 200
    b = r.json()
    assert b["ship_date"] == "2026-11-04"
    assert b["earliest_delivery"] == "2026-11-06"
    assert b["latest_delivery"] == "2026-11-09"
    assert b["missed_cutoff"] is False


def test_missed_cutoff_ships_next_business_day():
    r = ask(region="south", order_time="2026-11-04T15:00:00")
    b = r.json()
    assert b["missed_cutoff"] is True
    assert b["ship_date"] == "2026-11-05"
    assert b["earliest_delivery"] == "2026-11-09"
    assert b["latest_delivery"] == "2026-11-10"


def test_weekend_order_ships_monday_express():
    # Sat 2026-11-07
    r = ask(region="south", service_level="express", order_time="2026-11-07T10:00:00")
    b = r.json()
    assert b["ship_date"] == "2026-11-09"
    assert b["earliest_delivery"] == "2026-11-10"
    assert b["latest_delivery"] == "2026-11-10"


def test_express_is_faster_than_standard():
    std = ask(region="north", order_time="2026-11-04T10:00:00").json()
    exp = ask(
        region="north", service_level="express", order_time="2026-11-04T10:00:00"
    ).json()
    assert exp["earliest_delivery"] < std["earliest_delivery"]


def test_backorder_delays_ship_date():
    r = ask(region="south", all_in_stock=False, order_time="2026-11-04T10:00:00")
    b = r.json()
    assert b["backordered"] is True
    assert b["ship_date"] == "2026-11-11"
    assert b["earliest_delivery"] == "2026-11-13"
    assert b["latest_delivery"] == "2026-11-16"


def test_holiday_is_skipped():
    # Thu 2026-10-01; Fri 2026-10-02 is a holiday
    r = ask(region="south", order_time="2026-10-01T10:00:00")
    b = r.json()
    assert b["ship_date"] == "2026-10-01"
    assert b["earliest_delivery"] == "2026-10-06"
    assert b["latest_delivery"] == "2026-10-07"


def test_remote_region_has_longest_transit():
    b = ask(region="remote", order_time="2026-11-04T10:00:00").json()
    assert b["transit_business_days"] == 6
    assert b["earliest_delivery"] == "2026-11-12"
    assert b["latest_delivery"] == "2026-11-13"


def test_invalid_region_rejected():
    assert ask(region="moon").status_code == 422


def test_invalid_service_level_rejected():
    assert ask(region="south", service_level="teleport").status_code == 422


def test_default_order_time_works():
    r = ask(region="west")
    assert r.status_code == 200
    assert r.json()["earliest_delivery"] >= r.json()["ship_date"]


def test_add_business_days_skips_weekend():
    friday = date(2026, 11, 6)
    assert add_business_days(friday, 0) == friday
    assert add_business_days(friday, 1) == date(2026, 11, 9)
    assert next_business_day(friday) == date(2026, 11, 9)


def test_metrics_endpoint():
    ask(region="south", order_time="2026-11-04T10:00:00")
    assert "promise_computed_total" in client.get("/metrics").text
