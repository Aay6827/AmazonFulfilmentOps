import os

import httpx

TIMEOUT = float(os.getenv("DOWNSTREAM_TIMEOUT", "2.0"))

URLS = {
    "order": os.getenv("ORDER_URL", "http://localhost:45000"),
    "inventory": os.getenv("INVENTORY_URL", "http://localhost:45001"),
    "promise": os.getenv("PROMISE_URL", "http://localhost:45002"),
    "returns": os.getenv("RETURNS_URL", "http://localhost:45003"),
}


def build(transports: dict | None = None) -> dict[str, httpx.Client]:
    """One shared client per downstream. Tests pass mock transports."""
    transports = transports or {}
    return {
        name: httpx.Client(
            base_url=url, timeout=TIMEOUT, transport=transports.get(name)
        )
        for name, url in URLS.items()
    }
