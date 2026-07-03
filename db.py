import sqlite3
import json
from pathlib import Path

DB_PATH = Path(__file__).parent / "erli.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id          TEXT PRIMARY KEY,
                status      TEXT,
                seller_status TEXT,
                buyer_name  TEXT,
                city        TEXT,
                products    TEXT,
                skus        TEXT,
                total       INTEGER,
                created_at  TEXT,
                updated_at  TEXT,
                raw         TEXT
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key   TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
        """)


def upsert_order(order: dict):
    items = order.get("items", [])
    products = "; ".join(
        f"{i.get('quantity', 1)}x {i.get('name', '?')}" for i in items
    )
    skus = "; ".join(i.get("sku") or "" for i in items if i.get("sku"))
    addr = order.get("user", {}).get("deliveryAddress", {})
    buyer = f"{addr.get('firstName', '')} {addr.get('lastName', '')}".strip()

    with get_conn() as conn:
        conn.execute("""
            INSERT INTO orders
                (id, status, seller_status, buyer_name, city, products, skus,
                 total, created_at, updated_at, raw)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status        = excluded.status,
                seller_status = excluded.seller_status,
                updated_at    = excluded.updated_at,
                raw           = excluded.raw
        """, (
            order.get("id"),
            order.get("status"),
            order.get("sellerStatus") or "",
            buyer,
            addr.get("city", ""),
            products,
            skus,
            order.get("totalPrice") or 0,
            order.get("created"),
            order.get("updated"),
            json.dumps(order, ensure_ascii=False),
        ))


def get_orders(status=None, after=None, limit=200):
    sql = "SELECT * FROM orders WHERE 1=1"
    params = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if after:
        sql += " AND created_at >= ?"
        params.append(after)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_stats():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(status = 'purchased') as purchased,
                SUM(status = 'cancelled') as cancelled,
                SUM(status = 'pending')   as pending,
                SUM(CASE WHEN status = 'purchased' THEN total ELSE 0 END) as revenue
            FROM orders
        """).fetchone()
        return dict(rows)


def delete_order(order_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        return cur.rowcount > 0


def delete_orders_before(date_iso: str) -> int:
    """Delete all orders created before the given ISO date. Returns count."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM orders WHERE created_at < ?", (date_iso,)
        )
        return cur.rowcount


def get_order_ids_since(date_iso: str) -> list[str]:
    """IDs of orders created on/after the given ISO date (for status refresh)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM orders WHERE created_at >= ?", (date_iso,)
        ).fetchall()
        return [r["id"] for r in rows]


def get_sync_state(key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_sync_state(key: str, value: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sync_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
