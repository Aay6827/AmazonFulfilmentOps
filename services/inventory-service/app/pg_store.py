"""PostgreSQL-backed inventory store. Same interface as the in-memory InventoryStore."""
import time

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .models import (
    Reservation,
    ReservationStatus,
    ReserveItem,
    ReserveRequest,
    StockView,
)
from .store import InsufficientStock

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock (
    sku TEXT NOT NULL,
    region TEXT NOT NULL,
    on_hand INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    PRIMARY KEY (sku, region),
    CHECK (reserved <= on_hand)
);
CREATE TABLE IF NOT EXISTS reservations (
    order_id TEXT PRIMARY KEY,
    region TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reservation_items (
    order_id TEXT NOT NULL REFERENCES reservations (order_id),
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, sku)
);
"""


class PgStore:
    def __init__(self, dsn: str, connect_retries: int = 30) -> None:
        self.pool = ConnectionPool(
            dsn,
            min_size=1,
            max_size=10,
            timeout=2.0,
            kwargs={"row_factory": dict_row},
            check=ConnectionPool.check_connection,
            open=False,
        )
        self.pool.open()
        last_error = None
        for _ in range(connect_retries):
            try:
                with self.pool.connection(timeout=5) as conn:
                    conn.execute(SCHEMA)
                return
            except psycopg.Error as exc:  # the database may still be starting
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"database not reachable: {last_error}")

    def close(self) -> None:
        self.pool.close()

    def ping(self) -> bool:
        try:
            with self.pool.connection(timeout=1) as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False

    def reset(self) -> None:
        with self.pool.connection() as conn:
            conn.execute("TRUNCATE reservation_items, reservations, stock")

    def set_stock(self, sku: str, region: str, on_hand: int) -> StockView:
        with self.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO stock (sku, region, on_hand, reserved) VALUES (%s, %s, %s, 0) "
                "ON CONFLICT (sku, region) DO UPDATE SET on_hand = EXCLUDED.on_hand "
                "WHERE stock.reserved <= EXCLUDED.on_hand "
                "RETURNING on_hand, reserved",
                (sku, region, on_hand),
            ).fetchone()
        if row is None:
            raise ValueError("on_hand cannot be below reserved quantity")
        return StockView(
            sku=sku,
            region=region,
            on_hand=row["on_hand"],
            reserved=row["reserved"],
            available=row["on_hand"] - row["reserved"],
        )

    def get_stock(self, sku: str, region: str) -> StockView | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT on_hand, reserved FROM stock WHERE sku = %s AND region = %s",
                (sku, region),
            ).fetchone()
        if row is None:
            return None
        return StockView(
            sku=sku,
            region=region,
            on_hand=row["on_hand"],
            reserved=row["reserved"],
            available=row["on_hand"] - row["reserved"],
        )

    @staticmethod
    def _load(conn, order_id: str) -> Reservation | None:
        head = conn.execute(
            "SELECT region, status FROM reservations WHERE order_id = %s", (order_id,)
        ).fetchone()
        if head is None:
            return None
        items = conn.execute(
            "SELECT sku, qty FROM reservation_items WHERE order_id = %s ORDER BY sku",
            (order_id,),
        ).fetchall()
        return Reservation(
            order_id=order_id,
            region=head["region"],
            items=[ReserveItem(sku=i["sku"], qty=i["qty"]) for i in items],
            status=ReservationStatus(head["status"]),
        )

    def reserve(self, req: ReserveRequest) -> tuple[Reservation, bool]:
        """Atomic all-or-nothing reservation. Returns (reservation, created)."""
        wanted: dict[str, int] = {}
        for item in req.items:
            wanted[item.sku] = wanted.get(item.sku, 0) + item.qty

        with self.pool.connection() as conn:
            # Claiming the reservation row first makes retries and races idempotent:
            # a concurrent request for the same order waits here, then sees the winner.
            claimed = conn.execute(
                "INSERT INTO reservations (order_id, region, status) VALUES (%s, %s, %s) "
                "ON CONFLICT (order_id) DO NOTHING RETURNING order_id",
                (req.order_id, req.region, ReservationStatus.RESERVED.value),
            ).fetchone()
            if claimed is None:
                return self._load(conn, req.order_id), False

            # Lock stock rows in sorted order so concurrent orders cannot deadlock.
            available: dict[str, int] = {}
            for sku in sorted(wanted):
                row = conn.execute(
                    "SELECT on_hand, reserved FROM stock "
                    "WHERE sku = %s AND region = %s FOR UPDATE",
                    (sku, req.region),
                ).fetchone()
                available[sku] = row["on_hand"] - row["reserved"] if row else 0

            shortages = [
                {"sku": sku, "requested": qty, "available": available[sku]}
                for sku, qty in wanted.items()
                if qty > available[sku]
            ]
            if shortages:
                raise InsufficientStock(shortages)  # rolls back, claim row included

            for sku, qty in wanted.items():
                conn.execute(
                    "UPDATE stock SET reserved = reserved + %s WHERE sku = %s AND region = %s",
                    (qty, sku, req.region),
                )
                conn.execute(
                    "INSERT INTO reservation_items (order_id, sku, qty) VALUES (%s, %s, %s)",
                    (req.order_id, sku, qty),
                )
            return self._load(conn, req.order_id), True

    def release(self, order_id: str) -> Reservation | None:
        with self.pool.connection() as conn:
            head = conn.execute(
                "SELECT region, status FROM reservations WHERE order_id = %s FOR UPDATE",
                (order_id,),
            ).fetchone()
            if head is None:
                return None
            if head["status"] != ReservationStatus.RESERVED.value:
                raise ValueError("reservation already released")
            items = conn.execute(
                "SELECT sku, qty FROM reservation_items WHERE order_id = %s ORDER BY sku",
                (order_id,),
            ).fetchall()
            for item in items:
                conn.execute(
                    "UPDATE stock SET reserved = reserved - %s WHERE sku = %s AND region = %s",
                    (item["qty"], item["sku"], head["region"]),
                )
            conn.execute(
                "UPDATE reservations SET status = %s WHERE order_id = %s",
                (ReservationStatus.RELEASED.value, order_id),
            )
            return self._load(conn, order_id)

    def stats(self) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT count(*) FROM reservations WHERE status = 'RESERVED') AS active, "
                "(SELECT count(*) FROM reservations WHERE status = 'RELEASED') AS released, "
                "(SELECT count(*) FROM stock) AS stock_records"
            ).fetchone()
        return {
            "active_reservations": row["active"],
            "released_reservations": row["released"],
            "stock_records": row["stock_records"],
        }
