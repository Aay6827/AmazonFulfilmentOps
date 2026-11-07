import os
import time

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .models import Reservation, ReserveRequest, StockSet, StockView
from .store import InsufficientStock, InventoryStore

app = FastAPI(title="inventory-service", version="0.1.0")


def make_store():
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        from .pg_store import PgStore

        return PgStore(dsn)
    return InventoryStore()


store = make_store()

REQUESTS = Counter(
    "inventory_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
LATENCY = Histogram(
    "inventory_http_request_seconds", "HTTP request latency", ["path"]
)
RESERVATIONS = Counter(
    "inventory_reservations_total", "Reservation attempts", ["outcome"]
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


@app.get("/health")
def health() -> dict:
    if not store.ping():
        raise HTTPException(status_code=503, detail="database unavailable")
    return {"status": "ok", "service": "inventory-service"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/stats")
def stats() -> dict:
    return store.stats()


@app.put("/stock/{sku}", response_model=StockView)
def set_stock(sku: str, body: StockSet) -> StockView:
    try:
        return store.set_stock(sku, body.region, body.on_hand)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.get("/stock/{sku}", response_model=StockView)
def get_stock(sku: str, region: str) -> StockView:
    view = store.get_stock(sku, region)
    if view is None:
        raise HTTPException(status_code=404, detail="stock record not found")
    return view


@app.post("/reservations", response_model=Reservation, status_code=201)
def reserve(req: ReserveRequest, response: Response) -> Reservation:
    try:
        reservation, created = store.reserve(req)
    except InsufficientStock as exc:
        RESERVATIONS.labels("insufficient").inc()
        raise HTTPException(
            status_code=409,
            detail={"message": "insufficient stock", "shortages": exc.shortages},
        )
    if created:
        RESERVATIONS.labels("reserved").inc()
    else:
        RESERVATIONS.labels("replay").inc()
        response.status_code = 200
    return reservation


@app.post("/reservations/{order_id}/release", response_model=Reservation)
def release(order_id: str) -> Reservation:
    try:
        reservation = store.release(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if reservation is None:
        raise HTTPException(status_code=404, detail="reservation not found")
    RESERVATIONS.labels("released").inc()
    return reservation
