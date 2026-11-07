"""PostgreSQL-backed order store. Same interface as the in-memory OrderStore."""
import time
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .models import Order, OrderCreate, OrderItem, OrderStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    region TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS order_items (
    order_id TEXT NOT NULL REFERENCES orders (id),
    position INTEGER NOT NULL,
    sku TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty > 0),
    PRIMARY KEY (order_id, position)
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders (id)
);
"""


class PgOrderStore:
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
            conn.execute("TRUNCATE idempotency_keys, order_items, orders")

    @staticmethod
    def _load(conn, order_id: str) -> Order | None:
        head = conn.execute(
            "SELECT customer_id, region, status, created_at FROM orders WHERE id = %s",
            (order_id,),
        ).fetchone()
        if head is None:
            return None
        items = conn.execute(
            "SELECT sku, qty FROM order_items WHERE order_id = %s ORDER BY position",
            (order_id,),
        ).fetchall()
        return Order(
            id=order_id,
            customer_id=head["customer_id"],
            region=head["region"],
            items=[OrderItem(sku=i["sku"], qty=i["qty"]) for i in items],
            status=OrderStatus(head["status"]),
            created_at=head["created_at"],
        )

    def create(self, payload: OrderCreate, key: str | None) -> tuple[Order, bool]:
        """Return (order, created). created is False on an idempotent replay."""
        order_id = f"ORD-{uuid4().hex[:8].upper()}"
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO orders (id, customer_id, region, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    order_id,
                    payload.customer_id,
                    payload.region,
                    OrderStatus.CREATED.value,
                    datetime.now(UTC),
                ),
            )
            for position, item in enumerate(payload.items):
                conn.execute(
                    "INSERT INTO order_items (order_id, position, sku, qty) "
                    "VALUES (%s, %s, %s, %s)",
                    (order_id, position, item.sku, item.qty),
                )
            if key:
                # A concurrent request with the same key blocks here until the first
                # one commits, then loses the race and returns the winner's order.
                claimed = conn.execute(
                    "INSERT INTO idempotency_keys (key, order_id) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO NOTHING RETURNING key",
                    (key, order_id),
                ).fetchone()
                if claimed is None:
                    conn.rollback()
                    winner = conn.execute(
                        "SELECT order_id FROM idempotency_keys WHERE key = %s", (key,)
                    ).fetchone()
                    return self._load(conn, winner["order_id"]), False
            return self._load(conn, order_id), True

    def get(self, order_id: str) -> Order | None:
        with self.pool.connection() as conn:
            return self._load(conn, order_id)

    def cancel(self, order_id: str) -> Order | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT status FROM orders WHERE id = %s FOR UPDATE", (order_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != OrderStatus.CREATED.value:
                raise ValueError("order cannot be cancelled")
            conn.execute(
                "UPDATE orders SET status = %s WHERE id = %s",
                (OrderStatus.CANCELLED.value, order_id),
            )
            return self._load(conn, order_id)

    def stats(self) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT "
                "count(*) FILTER (WHERE status = 'CREATED') AS live, "
                "count(*) FILTER (WHERE status = 'CANCELLED') AS cancelled "
                "FROM orders"
            ).fetchone()
        return {"live_orders": row["live"], "cancelled_orders": row["cancelled"]}
