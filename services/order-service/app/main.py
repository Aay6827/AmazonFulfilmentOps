import os
import time

from fastapi import FastAPI, Header, HTTPException, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from .models import Order, OrderCreate
from .store import OrderStore

app = FastAPI(title="order-service", version="0.1.0")


def make_store():
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        from .pg_store import PgOrderStore

        return PgOrderStore(dsn)
    return OrderStore()


store = make_store()

DB_UP = Gauge("db_up", "1 if the database answers a ping, evaluated at scrape time")
DB_UP.set_function(lambda: 1 if store.ping() else 0)

REQUESTS = Counter(
    "order_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
LATENCY = Histogram("order_http_request_seconds", "HTTP request latency", ["path"])
ORDERS_CREATED = Counter("orders_created_total", "Orders created")
ORDERS_CANCELLED = Counter("orders_cancelled_total", "Orders cancelled")


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = route.path if route else request.url.path
    LATENCY.labels(path).observe(time.perf_counter() - start)
    REQUESTS.labels(request.method, path, response.status_code).inc()
    return response


@app.get("/health")
def health() -> dict:
    if not store.ping():
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "service": "order-service"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/stats")
def stats() -> dict:
    return store.stats()


@app.post("/orders", response_model=Order, status_code=201)
def create_order(
    payload: OrderCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None),
) -> Order:
    order, created = store.create(payload, idempotency_key)
    if created:
        ORDERS_CREATED.inc()
    else:
        response.status_code = 200
    return order


@app.get("/orders/{order_id}", response_model=Order)
def get_order(order_id: str) -> Order:
    order = store.get(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order


@app.post("/orders/{order_id}/cancel", response_model=Order)
def cancel_order(order_id: str) -> Order:
    try:
        order = store.cancel(order_id)
    except ValueError:
        raise HTTPException(status_code=409, detail="order cannot be cancelled")
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    ORDERS_CANCELLED.inc()
    return order
