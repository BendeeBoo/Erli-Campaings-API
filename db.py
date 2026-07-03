import json
import os
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "erli.db"

# On Render/cloud: set DATABASE_URL to a Postgres connection string (e.g. Neon).
# Locally without it the app keeps using the SQLite file.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if IS_PG:
    import psycopg2
    import psycopg2.extras


def get_conn():
    if IS_PG:
        return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _adapt(sql: str) -> str:
    return sql.replace("?", "%s") if IS_PG else sql


def query(sql: str, params=()) -> list[dict]:
    """Run a SELECT, return rows as list of dicts. Works on SQLite and Postgres."""
    conn = get_conn()
    try:
        if IS_PG:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(_adapt(sql), params)
                return [dict(r) for r in cur.fetchall()]
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def execute(sql: str, params=()) -> int:
    """Run an INSERT/UPDATE/DELETE, return affected row count."""
    conn = get_conn()
    try:
        if IS_PG:
            with conn.cursor() as cur:
                cur.execute(_adapt(sql), params)
                count = cur.rowcount
        else:
            count = conn.execute(sql, params).rowcount
        conn.commit()
        return count
    finally:
        conn.close()


BLOB_TYPE = "BYTEA" if IS_PG else "BLOB"

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS orders (
        id            TEXT PRIMARY KEY,
        status        TEXT,
        seller_status TEXT,
        buyer_name    TEXT,
        city          TEXT,
        products      TEXT,
        skus          TEXT,
        total         BIGINT,
        created_at    TEXT,
        updated_at    TEXT,
        raw           TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sync_state (
        key   TEXT PRIMARY KEY,
        value TEXT
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS xlsx_files (
        name        TEXT PRIMARY KEY,
        data        {BLOB_TYPE},
        uploaded_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status)",
]


def init_db():
    conn = get_conn()
    try:
        if IS_PG:
            with conn.cursor() as cur:
                for stmt in _SCHEMA:
                    cur.execute(stmt)
        else:
            for stmt in _SCHEMA:
                conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def upsert_order(order: dict):
    items = order.get("items", [])
    products = "; ".join(
        f"{i.get('quantity', 1)}x {i.get('name', '?')}" for i in items
    )
    skus = "; ".join(i.get("sku") or "" for i in items if i.get("sku"))
    addr = order.get("user", {}).get("deliveryAddress", {})
    buyer = f"{addr.get('firstName', '')} {addr.get('lastName', '')}".strip()

    execute("""
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
    return query(sql, params)


def get_stats():
    rows = query("""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'purchased') as purchased,
            COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled,
            COUNT(*) FILTER (WHERE status = 'pending')   as pending,
            COALESCE(SUM(total) FILTER (WHERE status = 'purchased'), 0) as revenue
        FROM orders
    """)
    return rows[0] if rows else {}


def get_all_order_ids() -> set[str]:
    return {r["id"] for r in query("SELECT id FROM orders")}


def get_all_raws() -> list[str]:
    return [r["raw"] for r in query("SELECT raw FROM orders")]


def get_all_orders() -> dict[str, dict]:
    return {r["id"]: r for r in query("SELECT * FROM orders")}


def get_order_row(order_id: str) -> dict | None:
    rows = query("SELECT * FROM orders WHERE id = ?", (order_id,))
    return rows[0] if rows else None


def delete_order(order_id: str) -> bool:
    return execute("DELETE FROM orders WHERE id = ?", (order_id,)) > 0


def delete_orders_before(date_iso: str) -> int:
    """Delete all orders created before the given ISO date. Returns count."""
    return execute("DELETE FROM orders WHERE created_at < ?", (date_iso,))


def get_order_ids_since(date_iso: str) -> list[str]:
    """IDs of orders created on/after the given ISO date (for status refresh)."""
    return [r["id"] for r in query(
        "SELECT id FROM orders WHERE created_at >= ?", (date_iso,)
    )]


# ── xlsx files (stored in DB so they survive redeploys on cloud hosting) ────

def save_xlsx(name: str, data: bytes, uploaded_at: str):
    execute("""
        INSERT INTO xlsx_files (name, data, uploaded_at)
        VALUES (?,?,?)
        ON CONFLICT(name) DO UPDATE SET
            data = excluded.data, uploaded_at = excluded.uploaded_at
    """, (name, data, uploaded_at))


def list_xlsx() -> list[str]:
    return [r["name"] for r in query(
        "SELECT name FROM xlsx_files ORDER BY name DESC"
    )]


def get_xlsx(name: str) -> bytes | None:
    rows = query("SELECT data FROM xlsx_files WHERE name = ?", (name,))
    if not rows:
        return None
    data = rows[0]["data"]
    return bytes(data) if data is not None else None


def get_sync_state(key: str, default=None):
    rows = query("SELECT value FROM sync_state WHERE key = ?", (key,))
    return rows[0]["value"] if rows else default


def set_sync_state(key: str, value: str):
    execute(
        "INSERT INTO sync_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )
