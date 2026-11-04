import time
from datetime import datetime

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from .logic import compute_promise
from .models import PromiseRequest, PromiseResponse

app = FastAPI(title="promise-service", version="0.1.0")

REQUESTS = Counter(
    "promise_http_requests_total", "HTTP requests", ["method", "path", "status"]
)
LATENCY = Histogram("promise_http_request_seconds", "HTTP request latency", ["path"])
PROMISES = Counter(
    "promise_computed_total", "Promises computed", ["service_level", "backordered"]
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
    return {"status": "ok", "service": "promise-service"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/promise", response_model=PromiseResponse)
def promise(req: PromiseRequest) -> PromiseResponse:
    result = compute_promise(
        region=req.region,
        service_level=req.service_level,
        all_in_stock=req.all_in_stock,
        order_time=req.order_time or datetime.now(),
    )
    PROMISES.labels(req.service_level, str(result["backordered"]).lower()).inc()
    return PromiseResponse(**result)
