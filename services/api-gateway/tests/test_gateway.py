import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main
from app.clients import build

client = TestClient(main.app)

SERVICES = ("order", "inventory", "promise", "returns")


class World:
    """Fake downstream services recording every call."""

    def __init__(self):
        self.calls = []
        self.down = set()
        self.stock = {"SKU-1": 10, "SKU-2": 5}
        self.orders = {}
        self.reservations = set()
        self.order_headers = []

    def transport(self, service):
        def handler(request):
            self.calls.append((service, request.method, request.url.path))
            if service in self.down:
                raise httpx.ConnectError("down", request=request)
            if request.url.path == "/health":
                return httpx.Response(200, json={"status": "ok"})
            return getattr(self, f"_{service}")(request)

        return httpx.MockTransport(handler)

    def _order(self, request):
        path = request.url.path
        if request.method == "POST" and path == "/orders":
            self.order_headers.append(request.headers.get("idempotency-key"))
            body = json.loads(request.content)
            oid = f"ORD-{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, "status": "CREATED", **body}
            return httpx.Response(201, json=self.orders[oid])
        oid = path.split("/")[2]
        if oid not in self.orders:
            return httpx.Response(404, json={"detail": "order not found"})
        if path.endswith("/cancel"):
            self.orders[oid]["status"] = "CANCELLED"
        return httpx.Response(200, json=self.orders[oid])

    def _inventory(self, request):
        path = request.url.path
        if path == "/reservations":
            body = json.loads(request.content)
            shortages = [
                {
                    "sku": i["sku"],
                    "requested": i["qty"],
                    "available": self.stock.get(i["sku"], 0),
                }
                for i in body["items"]
                if i["qty"] > self.stock.get(i["sku"], 0)
            ]
            if shortages:
                return httpx.Response(
                    409,
                    json={
                        "detail": {
                            "message": "insufficient stock",
                            "shortages": shortages,
                        }
                    },
                )
            self.reservations.add(body["order_id"])
            return httpx.Response(201, json={**body, "status": "RESERVED"})
        oid = path.split("/")[2]
        if oid in self.reservations:
            self.reservations.remove(oid)
            return httpx.Response(
                200, json={"order_id": oid, "status": "RELEASED"}
            )
        return httpx.Response(404, json={"detail": "reservation not found"})

    def _promise(self, request):
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "region": body["region"],
                "service_level": body["service_level"],
                "ship_date": "2026-11-04",
                "earliest_delivery": "2026-11-06",
                "latest_delivery": "2026-11-09",
                "transit_business_days": 2,
                "backordered": False,
                "missed_cutoff": False,
            },
        )

    def _returns(self, request):
        body = json.loads(request.content)
        return httpx.Response(201, json={"id": "RET-1", **body})

    def services_called(self):
        return [c[0] for c in self.calls]


@pytest.fixture
def world():
    w = World()
    main.clients = build({name: w.transport(name) for name in SERVICES})
    yield w


def checkout(**over):
    body = {
        "customer_id": "C1",
        "region": "south",
        "service_level": "standard",
        "items": [{"sku": "SKU-1", "qty": 2}],
    }
    body.update(over)
    return client.post("/checkout", json=body)


def test_health(world):
    assert client.get("/health").json()["service"] == "api-gateway"


def test_checkout_happy_path(world):
    r = checkout()
    assert r.status_code == 201
    b = r.json()
    assert b["order"]["id"] == "ORD-1"
    assert b["reservation"]["status"] == "RESERVED"
    assert b["promise"]["earliest_delivery"] == "2026-11-06"
    assert b["degraded"] is False
    assert world.services_called() == ["order", "inventory", "promise"]


def test_out_of_stock_cancels_order_and_skips_promise(world):
    r = checkout(items=[{"sku": "SKU-1", "qty": 50}])
    assert r.status_code == 409
    shortages = r.json()["detail"]["shortages"]
    assert shortages == [{"sku": "SKU-1", "requested": 50, "available": 10}]
    assert ("order", "POST", "/orders/ORD-1/cancel") in world.calls
    assert "promise" not in world.services_called()
    assert world.orders["ORD-1"]["status"] == "CANCELLED"


def test_inventory_down_cancels_order_and_returns_503(world):
    world.down.add("inventory")
    r = checkout()
    assert r.status_code == 503
    assert "inventory" in r.json()["detail"]
    assert world.orders["ORD-1"]["status"] == "CANCELLED"


def test_promise_down_degrades_but_checkout_succeeds(world):
    world.down.add("promise")
    r = checkout()
    assert r.status_code == 201
    b = r.json()
    assert b["promise"] is None
    assert b["degraded"] is True
    assert ("inventory", "POST", "/reservations/ORD-1/release") not in world.calls
    assert "ORD-1" in world.reservations


def test_order_service_down_never_touches_inventory(world):
    world.down.add("order")
    r = checkout()
    assert r.status_code == 503
    assert "inventory" not in world.services_called()


def test_validation_rejects_bad_requests(world):
    assert checkout(region="moon").status_code == 422
    assert checkout(items=[]).status_code == 422
    assert checkout(items=[{"sku": "SKU-1", "qty": 0}]).status_code == 422
    assert world.calls == []


def test_idempotency_key_is_forwarded_to_order_service(world):
    body = {
        "customer_id": "C1",
        "region": "south",
        "items": [{"sku": "SKU-1", "qty": 1}],
    }
    client.post("/checkout", json=body, headers={"Idempotency-Key": "k-1"})
    assert world.order_headers == ["k-1"]


def test_cancel_releases_stock(world):
    checkout()
    r = client.post("/orders/ORD-1/cancel")
    assert r.status_code == 200
    assert r.json()["order"]["status"] == "CANCELLED"
    assert r.json()["stock_released"] is True
    assert world.reservations == set()


def test_cancel_when_inventory_down_reports_unreleased_stock(world):
    checkout()
    world.down.add("inventory")
    r = client.post("/orders/ORD-1/cancel")
    assert r.status_code == 200
    assert r.json()["stock_released"] is False


def test_cancel_unknown_order_404(world):
    assert client.post("/orders/ORD-X/cancel").status_code == 404


def test_get_order_proxy(world):
    checkout()
    assert client.get("/orders/ORD-1").json()["id"] == "ORD-1"
    assert client.get("/orders/ORD-9").status_code == 404


def test_health_deps_all_up(world):
    b = client.get("/health/deps").json()
    assert b["status"] == "ok"
    assert set(b["services"].values()) == {"up"}


def test_health_deps_degraded_when_one_down(world):
    world.down.add("promise")
    b = client.get("/health/deps").json()
    assert b["status"] == "degraded"
    assert b["services"]["promise"] == "down"
    assert b["services"]["order"] == "up"


def test_returns_passthrough(world):
    r = client.post("/returns", json={"order_id": "ORD-1", "sku": "SKU-1"})
    assert r.status_code == 201
    assert r.json()["id"] == "RET-1"


def test_returns_down_gives_503(world):
    world.down.add("returns")
    r = client.post("/returns", json={"order_id": "ORD-1"})
    assert r.status_code == 503


def test_metrics_endpoint(world):
    checkout()
    text = client.get("/metrics").text
    assert "gateway_checkouts_total" in text
    assert "gateway_downstream_calls_total" in text
