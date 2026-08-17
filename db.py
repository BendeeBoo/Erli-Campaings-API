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
        raw           TEXT,
        source        TEXT
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


def _migrate(conn):
    """Add columns missing in databases created by older versions."""
    if IS_PG:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS source TEXT")
            for col in ("order_ids TEXT", "period_from TEXT", "period_to TEXT"):
                cur.execute(f"ALTER TABLE xlsx_files ADD COLUMN IF NOT EXISTS {col}")
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN source TEXT")
    xcols = {r[1] for r in conn.execute("PRAGMA table_info(xlsx_files)").fetchall()}
    for col in ("order_ids", "period_from", "period_to"):
        if col not in xcols:
            conn.execute(f"ALTER TABLE xlsx_files ADD COLUMN {col} TEXT")


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
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def upsert_order(order: dict, source: str = "xlsx"):
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
             total, created_at, updated_at, raw, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            status        = excluded.status,
            seller_status = excluded.seller_status,
            updated_at    = excluded.updated_at,
            raw           = excluded.raw,
            -- keep the original source: an order added by hand must not be
            -- downgraded to 'xlsx' just because a report later mentions it
            source        = COALESCE(orders.source, excluded.source)
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
        source,
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


# Kept in sync with report.RETURNED_SELLER_STATUSES — a paid order that was
# sent back earns no money, so it must not show up as revenue.
_RETURNED_SQL = "LOWER(COALESCE(seller_status, '')) IN " \
                "('returned', 'returningtosender', 'returnedtosender')"


def get_stats():
    rows = query(f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE status = 'purchased'
                             AND NOT {_RETURNED_SQL}) as purchased,
            COUNT(*) FILTER (WHERE status = 'cancelled') as cancelled,
            COUNT(*) FILTER (WHERE status = 'pending')   as pending,
            COUNT(*) FILTER (WHERE {_RETURNED_SQL})      as returned,
            COALESCE(SUM(total) FILTER (WHERE status = 'purchased'
                                        AND NOT {_RETURNED_SQL}), 0) as revenue
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


def delete_orders(ids: list[str]) -> int:
    """Delete the given orders by id. Returns count actually deleted."""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    return execute(f"DELETE FROM orders WHERE id IN ({placeholders})", tuple(ids))


def get_order_sources(ids: list[str]) -> dict[str, str]:
    """id → source for the given orders. Missing ids are simply absent."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    return {
        r["id"]: (r["source"] or "xlsx")
        for r in query(
            f"SELECT id, source FROM orders WHERE id IN ({placeholders})", tuple(ids)
        )
    }


def set_order_source(order_id: str, source: str) -> int:
    return execute("UPDATE orders SET source = ? WHERE id = ?", (source, order_id))


def get_orders_without_source() -> list[str]:
    return [r["id"] for r in query("SELECT id FROM orders WHERE source IS NULL")]


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


def list_xlsx_meta() -> list[dict]:
    """Same order as list_xlsx(), with upload timestamp for the UI."""
    return query(
        "SELECT name, uploaded_at FROM xlsx_files ORDER BY name DESC"
    )


def set_xlsx_index(name: str, order_ids: list[str], period_from: str, period_to: str):
    """
    Cache what a report contains, so deleting one never has to parse xlsx.
    Parsing every stored file on each delete was slow and memory-hungry.
    """
    execute(
        "UPDATE xlsx_files SET order_ids = ?, period_from = ?, period_to = ? "
        "WHERE name = ?",
        (json.dumps(sorted(set(order_ids))), period_from, period_to, name),
    )


def get_xlsx_index(name: str) -> dict | None:
    """Cached report contents, or None when it has not been indexed yet."""
    rows = query(
        "SELECT order_ids, period_from, period_to FROM xlsx_files WHERE name = ?",
        (name,),
    )
    if not rows or rows[0]["order_ids"] is None:
        return None
    r = rows[0]
    return {
        "order_ids": json.loads(r["order_ids"]),
        "from":      r["period_from"] or "",
        "to":        r["period_to"] or "",
    }


def delete_xlsx(name: str) -> bool:
    return execute("DELETE FROM xlsx_files WHERE name = ?", (name,)) > 0


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
