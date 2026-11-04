import logging
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .clients import build
from .models import CheckoutRequest

log = logging.getLogger("api-gateway")

app = FastAPI(title="api-gateway", version="0.1.0")
clients = build()

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


def call(service: str, method: str, path: str, **kwargs) -> httpx.Response:
    """Call a downstream. Network errors and 5xx both count as unavailable."""
    try:
        resp = clients[service].request(method, path, **kwargs)
    except httpx.RequestError:
        DOWNSTREAM.labels(service, "unavailable").inc()
        raise Unavailable(service)
    if resp.status_code >= 500:
        DOWNSTREAM.labels(service, "error").inc()
        raise Unavailable(service)
    DOWNSTREAM.labels(service, "ok").inc()
    return resp


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
            r = call(name, "GET", "/health")
            services[name] = "up" if r.status_code == 200 else "down"
        except Unavailable:
            services[name] = "down"
    overall = "ok" if all(v == "up" for v in services.values()) else "degraded"
    return {"status": overall, "services": services}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/checkout", status_code=201)
def checkout(
    payload: CheckoutRequest,
    idempotency_key: str | None = Header(default=None),
) -> dict:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    items = [i.model_dump() for i in payload.items]

    # 1. create the order (its id is the reservation key)
    try:
        order_resp = call(
            "order",
            "POST",
            "/orders",
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
