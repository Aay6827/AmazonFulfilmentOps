import time
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .models import Return, ReturnCreate
from .store import (
    DuplicateReturn,
    InvalidTransition,
    ReturnStore,
    ReturnWindowExpired,
)

app = FastAPI(title="returns-service", version="0.1.0")
store = ReturnStore()

REQUESTS = Counter(
    "returns_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
LATENCY = Histogram("returns_http_request_seconds", "HTTP request latency", ["path"])
CREATED = Counter("returns_created_total", "Returns created", ["reason"])
TRANSITIONS = Counter("returns_transitions_total", "State transitions", ["action"])
REFUNDED_PAISE = Counter("returns_refunded_paise_total", "Total refunded in paise")


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
    return {"status": "ok", "service": "returns-service"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/returns", response_model=Return, status_code=201)
def create_return(payload: ReturnCreate) -> Return:
    try:
        ret = store.create(payload)
    except ReturnWindowExpired as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except DuplicateReturn as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    CREATED.labels(ret.reason.value).inc()
    return ret


@app.get("/returns", response_model=list[Return])
def list_returns(order_id: str | None = None) -> list[Return]:
    return store.list(order_id)


@app.get("/returns/{return_id}", response_model=Return)
def get_return(return_id: str) -> Return:
    ret = store.get(return_id)
    if ret is None:
        raise HTTPException(status_code=404, detail="return not found")
    return ret


@app.post("/returns/{return_id}/{action}", response_model=Return)
def transition(
    return_id: str, action: Literal["approve", "reject", "receive", "refund"]
) -> Return:
    try:
        ret = store.transition(return_id, action)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if ret is None:
        raise HTTPException(status_code=404, detail="return not found")
    TRANSITIONS.labels(action).inc()
    if action == "refund":
        REFUNDED_PAISE.inc(ret.refund_paise)
    return ret
