import threading

from .models import (
    Reservation,
    ReservationStatus,
    ReserveItem,
    ReserveRequest,
    StockView,
)


class InsufficientStock(Exception):
    def __init__(self, shortages: list[dict]) -> None:
        super().__init__("insufficient stock")
        self.shortages = shortages


class InventoryStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self._stock: dict[tuple[str, str], dict[str, int]] = {}
        self._reservations: dict[str, Reservation] = {}

    def _view(self, sku: str, region: str) -> StockView:
        rec = self._stock[(sku, region)]
        return StockView(
            sku=sku,
            region=region,
            on_hand=rec["on_hand"],
            reserved=rec["reserved"],
            available=rec["on_hand"] - rec["reserved"],
        )

    def set_stock(self, sku: str, region: str, on_hand: int) -> StockView:
        with self._lock:
            rec = self._stock.setdefault((sku, region), {"on_hand": 0, "reserved": 0})
            if on_hand < rec["reserved"]:
                raise ValueError("on_hand cannot be below reserved quantity")
            rec["on_hand"] = on_hand
            return self._view(sku, region)

    def get_stock(self, sku: str, region: str) -> StockView | None:
        with self._lock:
            if (sku, region) not in self._stock:
                return None
            return self._view(sku, region)

    def reserve(self, req: ReserveRequest) -> tuple[Reservation, bool]:
        """Atomic all-or-nothing reservation. Returns (reservation, created)."""
        with self._lock:
            existing = self._reservations.get(req.order_id)
            if existing is not None:
                return existing, False

            wanted: dict[str, int] = {}
            for item in req.items:
                wanted[item.sku] = wanted.get(item.sku, 0) + item.qty

            shortages = []
            for sku, qty in wanted.items():
                rec = self._stock.get((sku, req.region))
                available = rec["on_hand"] - rec["reserved"] if rec else 0
                if qty > available:
                    shortages.append(
                        {"sku": sku, "requested": qty, "available": available}
                    )
            if shortages:
                raise InsufficientStock(shortages)

            for sku, qty in wanted.items():
                self._stock[(sku, req.region)]["reserved"] += qty

            reservation = Reservation(
                order_id=req.order_id,
                region=req.region,
                items=[ReserveItem(sku=s, qty=q) for s, q in wanted.items()],
                status=ReservationStatus.RESERVED,
            )
            self._reservations[req.order_id] = reservation
            return reservation, True

    def release(self, order_id: str) -> Reservation | None:
        with self._lock:
            reservation = self._reservations.get(order_id)
            if reservation is None:
                return None
            if reservation.status != ReservationStatus.RESERVED:
                raise ValueError("reservation already released")
            for item in reservation.items:
                self._stock[(item.sku, reservation.region)]["reserved"] -= item.qty
            reservation.status = ReservationStatus.RELEASED
            return reservation

    def ping(self) -> bool:
        return True

    def stats(self) -> dict:
        with self._lock:
            active = sum(
                1 for r in self._reservations.values() if r.status == ReservationStatus.RESERVED
            )
            return {
                "active_reservations": active,
                "released_reservations": len(self._reservations) - active,
                "stock_records": len(self._stock),
            }
