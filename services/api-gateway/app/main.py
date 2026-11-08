import logging
import os
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .clients import build
from .models import CheckoutRequest
from .resilience import (
    CircuitBreaker,
    CircuitOpen,
    DeadlineExceeded,
    Guard,
    Saturated,
)

log = logging.getLogger("api-gateway")

app = FastAPI(title="api-gateway", version="0.2.0")
clients = build()

CRITICAL_BUDGET = float(os.getenv("CRITICAL_BUDGET", "1.5"))
OPTIONAL_BUDGET = float(os.getenv("OPTIONAL_BUDGET", "0.5"))
TOTAL_BUDGET = float(os.getenv("TOTAL_BUDGET", "3.0"))
BREAKER_FAILURES = int(os.getenv("BREAKER_FAILURES", "3"))
BREAKER_RESET = float(os.getenv("BREAKER_RESET", "10.0"))
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "16"))

BUDGETS = {
    "order": CRITICAL_BUDGET,
    "inventory": CRITICAL_BUDGET,
    "returns": CRITICAL_BUDGET,
    "promise": OPTIONAL_BUDGET,
}
STATE_CODES = {"closed": 0, "half_open": 1, "open": 2}


def build_guards():
    return {
        name: Guard(CircuitBreaker(BREAKER_FAILURES, BREAKER_RESET), MAX_INFLIGHT)
        for name in BUDGETS
    }


guards = build_guards()


def reset_guards():
    old = list(guards.values())
    guards.clear()
    guards.update(build_guards())
    for guard in old:
        guard.close()


REQUESTS = Counter(
    "gateway_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
LATENCY = Histogram("gateway_http_request_seconds", "HTTP request latency", ["path"])
DOWNSTREAM = Counter(
    "gateway_downstream_calls_total", "Downstream calls", ["service", "outcome"]
)
CHECKOUTS = Counter("gateway_checkouts_total", "Checkout results", ["outcome"])
COMPENSATION_FAILED = Counter(
    "gateway_compensation_failed_total",
    "Order cancellations that failed during checkout rollback",
)
CIRCUIT_STATE = Gauge(
    "gateway_circuit_state", "Circuit breaker state: 0 closed, 1 half-open, 2 open", ["service"]
)
for _name in BUDGETS:
    CIRCUIT_STATE.labels(_name).set_function(
        lambda n=_name: STATE_CODES[guards[n].breaker.state()]
    )


class Unavailable(Exception):
    def __init__(self, service: str) -> None:
        super().__init__(service)
        self.service = service


@app.exception_handler(Unavailable)
async def unavailable_handler(request: Request, exc: Unavailable):
    return JSONResponse(
        status_code=503, content={"detail": f"{exc.service} unavailable"}
    )


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = route.path if route else request.url.path
    LATENCY.labels(path).observe(time.perf_counter() - start)
    REQUESTS.labels(request.method, path, response.status_code).inc()
    return response


def call(
    service: str,
    method: str,
    path: str,
    budget: float | None = None,
    bypass_breaker: bool = False,
    **kwargs,
) -> httpx.Response:
    """Call a downstream with a hard deadline and a circuit breaker.

    Skipped calls, deadlines, network errors and 5xx all count as unavailable.
    """
    guard = guards[service]
    limit = BUDGETS[service] if budget is None else budget
    try:
        resp = guard.run(
            lambda: clients[service].request(method, path, **kwargs),
            limit,
            bypass_breaker=bypass_breaker,
        )
    except CircuitOpen:
        DOWNSTREAM.labels(service, "circuit_open").inc()
        raise Unavailable(service) from None
    except Saturated:
        DOWNSTREAM.labels(service, "saturated").inc()
        raise Unavailable(service) from None
    except DeadlineExceeded:
        DOWNSTREAM.labels(service, "deadline").inc()
        raise Unavailable(service) from None
    except httpx.RequestError:
        guard.breaker.record_failure()
        DOWNSTREAM.labels(service, "unavailable").inc()
        raise Unavailable(service) from None
    if resp.status_code >= 500:
        guard.breaker.record_failure()
        DOWNSTREAM.labels(service, "error").inc()
        raise Unavailable(service)
    guard.breaker.record_success()
    DOWNSTREAM.labels(service, "ok").inc()
    return resp


def _budget(service: str, started: float) -> float:
    """Per-call budget, capped by what is left of the total checkout budget."""
    left = TOTAL_BUDGET - (time.monotonic() - started)
    if left <= 0.05:
        raise Unavailable(service)
    return min(BUDGETS[service], left)


def _detail(resp: httpx.Response):
    try:
        data = resp.json()
    except ValueError:
        return resp.text
    return data.get("detail", data) if isinstance(data, dict) else data


def _cancel_order_quietly(order_id: str) -> None:
    try:
        call("order", "POST", f"/orders/{order_id}/cancel")
    except Unavailable:
        COMPENSATION_FAILED.inc()
        log.error("could not cancel order %s during checkout rollback", order_id)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "api-gateway"}


@app.get("/health/deps")
def health_deps() -> dict:
    services = {}
    for name in clients:
        try:
            r = call(name, "GET", "/health", bypass_breaker=True)
            services[name] = "up" if r.status_code == 200 else "down"
        except Unavailable:
            services[name] = "down"
    overall = "ok" if all(v == "up" for v in services.values()) else "degraded"
    circuits = {name: guards[name].breaker.state() for name in BUDGETS}
    return {"status": overall, "services": services, "circuits": circuits}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/checkout", status_code=201)
def checkout(
    payload: CheckoutRequest,
    idempotency_key: str | None = Header(default=None),
) -> dict:
    started = time.monotonic()
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    items = [i.model_dump() for i in payload.items]

    # A critical dependency whose circuit is open will fail anyway: do not create an
    # order only to cancel it.
    if guards["inventory"].breaker.state() == "open":
        CHECKOUTS.labels("unavailable").inc()
        raise Unavailable("inventory")

    # 1. create the order (its id is the reservation key)
    try:
        order_resp = call(
            "order",
            "POST",
            "/orders",
            budget=_budget("order", started),
            json={
                "customer_id": payload.customer_id,
                "region": payload.region,
                "items": items,
            },
            headers=headers,
        )
    except Unavailable:
        CHECKOUTS.labels("unavailable").inc()
        raise
    if order_resp.status_code not in (200, 201):
        raise HTTPException(order_resp.status_code, detail=_detail(order_resp))
    order = order_resp.json()

    # 2. reserve stock; roll the order back if that fails
    try:
        res = call(
            "inventory",
            "POST",
            "/reservations",
            budget=_budget("inventory", started),
            json={
                "order_id": order["id"],
                "region": payload.region,
                "items": items,
            },
        )
    except Unavailable:
        _cancel_order_quietly(order["id"])
        CHECKOUTS.labels("unavailable").inc()
        raise
    if res.status_code == 409:
        _cancel_order_quietly(order["id"])
        CHECKOUTS.labels("out_of_stock").inc()
        raise HTTPException(409, detail=_detail(res))
    if res.status_code not in (200, 201):
        _cancel_order_quietly(order["id"])
        raise HTTPException(502, detail=_detail(res))
    reservation = res.json()

    # 3. delivery promise is best effort: checkout survives without it
    promise = None
    try:
        pr = call(
            "promise",
            "POST",
            "/promise",
            budget=_budget("promise", started),
            json={
                "region": payload.region,
                "service_level": payload.service_level,
                "all_in_stock": True,
            },
        )
        if pr.status_code == 200:
            promise = pr.json()
    except Unavailable:
        pass

    degraded = promise is None
    CHECKOUTS.labels("degraded" if degraded else "success").inc()
    return {
        "order": order,
        "reservation": reservation,
        "promise": promise,
        "degraded": degraded,
    }


@app.get("/orders/{order_id}")
def get_order(order_id: str) -> JSONResponse:
    r = call("order", "GET", f"/orders/{order_id}")
    return JSONResponse(status_code=r.status_code, content=r.json())


@app.post("/orders/{order_id}/cancel")
def cancel_order(order_id: str) -> dict:
    r = call("order", "POST", f"/orders/{order_id}/cancel")
    if r.status_code != 200:
        raise HTTPException(r.status_code, detail=_detail(r))
    released = False
    try:
        rel = call("inventory", "POST", f"/reservations/{order_id}/release")
        released = rel.status_code == 200
    except Unavailable:
        pass
    return {"order": r.json(), "stock_released": released}


@app.post("/returns")
def create_return(body: dict) -> JSONResponse:
    r = call("returns", "POST", "/returns", json=body)
    return JSONResponse(status_code=r.status_code, content=r.json())
