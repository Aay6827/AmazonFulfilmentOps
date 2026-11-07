from datetime import UTC, datetime
from uuid import uuid4

from .models import Order, OrderCreate, OrderStatus


class OrderStore:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._orders: dict[str, Order] = {}
        self._idempotency: dict[str, str] = {}

    def create(self, payload: OrderCreate, key: str | None) -> tuple[Order, bool]:
        """Return (order, created). created is False on an idempotent replay."""
        if key and key in self._idempotency:
            return self._orders[self._idempotency[key]], False
        order = Order(
            id=f"ORD-{uuid4().hex[:8].upper()}",
            customer_id=payload.customer_id,
            region=payload.region,
            items=payload.items,
            status=OrderStatus.CREATED,
            created_at=datetime.now(UTC),
        )
        self._orders[order.id] = order
        if key:
            self._idempotency[key] = order.id
        return order, True

    def get(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    def cancel(self, order_id: str) -> Order | None:
        order = self._orders.get(order_id)
        if order is None:
            return None
        if order.status != OrderStatus.CREATED:
            raise ValueError("order cannot be cancelled")
        order.status = OrderStatus.CANCELLED
        return order

    def ping(self) -> bool:
        return True

    def stats(self) -> dict:
        live = sum(1 for o in self._orders.values() if o.status == OrderStatus.CREATED)
        return {"live_orders": live, "cancelled_orders": len(self._orders) - live}
