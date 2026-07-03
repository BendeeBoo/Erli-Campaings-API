"""
Report engine: joins xlsx ad data with orders from DB.
"""

import re
import json
import pandas as pd
from pathlib import Path

from db import upsert_order, get_conn
from erli_api import get_order

GREENHOUSE_KEYWORDS = ["szklarnia", "tunel", "domek"]
ACCESSORY_KEYWORDS  = ["okno boczne", "okno do szklarni", "taśma", "tasma",
                        "ramka", "kotwy", "agrotkanina", "agrowłóknina",
                        "fundament", "śruba", "sruba", "grunt", "folia"]


def is_greenhouse(name: str) -> bool:
    n = (name or "").lower()
    # Accessory check takes priority
    if any(kw in n for kw in ACCESSORY_KEYWORDS):
        return False
    return any(kw in n for kw in GREENHOUSE_KEYWORDS)


def parse_order_ids(cell) -> list[str]:
    if pd.isna(cell) or not str(cell).strip():
        return []
    return [x.strip() for x in str(cell).split(",") if x.strip()]


def load_xlsx(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, header=0)
    df.columns = [c.strip() for c in df.columns]
    return df


def import_orders_from_xlsx(df: pd.DataFrame) -> dict:
    """Fetch all order IDs found in xlsx and save to DB. Returns summary."""
    all_ids = set()
    for cell in df["Lista zamowien"].dropna():
        all_ids.update(parse_order_ids(cell))

    fetched, skipped, errors = 0, 0, 0
    with get_conn() as conn:
        existing = {
            r[0] for r in conn.execute("SELECT id FROM orders").fetchall()
        }

    for oid in all_ids:
        if oid in existing:
            skipped += 1
            continue
        try:
            order = get_order(oid)
            if order:
                upsert_order(order)
                fetched += 1
            else:
                errors += 1
        except Exception:
            errors += 1

    return {"total": len(all_ids), "fetched": fetched,
            "skipped": skipped, "errors": errors}


def _product_name_map() -> dict[int, str]:
    """Build item_id → product_name map from all orders in DB."""
    mapping = {}
    with get_conn() as conn:
        for row in conn.execute("SELECT raw FROM orders").fetchall():
            try:
                raw = json.loads(row["raw"] or "{}")
                for item in raw.get("items", []):
                    iid = item.get("id")
                    name = item.get("name", "")
                    if iid and name and iid not in mapping:
                        mapping[int(iid)] = name
            except Exception:
                pass
    return mapping


def build_report(df: pd.DataFrame,
                 date_from: str | None = None,
                 date_to:   str | None = None) -> dict:
    """
    Build the analytics report from xlsx + DB orders.
    Returns aggregated metrics per product + campaign totals.
    """
    # Load all orders from DB into a dict
    with get_conn() as conn:
        db_orders = {
            r["id"]: dict(r)
            for r in conn.execute("SELECT * FROM orders").fetchall()
        }

    # Apply date filter
    df = df.copy()
    df["Data"] = pd.to_datetime(df["Data"])
    if date_from:
        df = df[df["Data"] >= pd.to_datetime(date_from)]
    if date_to:
        df = df[df["Data"] <= pd.to_datetime(date_to)]

    name_map = _product_name_map()

    # Aggregate xlsx by product
    rows = []
    for product_id, grp in df.groupby("ID produktu"):
        impressions  = int(grp["Wyswietlenia"].sum())
        clicks       = int(grp["Klikniecia"].sum())
        cost         = float(grp["Twoj koszt netto"].sum())

        # Collect orders for this product
        order_ids = []
        for cell in grp["Lista zamowien"].dropna():
            order_ids.extend(parse_order_ids(cell))
        order_ids = list(set(order_ids))

        # Analyse orders
        greenhouse_revenue = 0.0
        units_sold         = 0
        cancelled          = 0
        pending            = 0
        accessory_revenue  = 0.0

        # Pre-fill name from DB map if available
        product_name = name_map.get(int(product_id), "")

        for oid in order_ids:
            order = db_orders.get(oid)
            if not order:
                continue

            status = order.get("status", "")
            if status == "cancelled":
                cancelled += 1
                continue
            if status == "pending":
                pending += 1
                continue

            # status == purchased — count revenue
            raw = json.loads(order.get("raw") or "{}")
            for item in raw.get("items", []):
                item_name = item.get("name", "")
                qty       = item.get("quantity", 1)
                price_pln = (item.get("unitPrice") or 0) / 100

                if is_greenhouse(item_name):
                    greenhouse_revenue += price_pln * qty
                    units_sold         += qty
                    if not product_name:
                        product_name = item_name
                else:
                    accessory_revenue += price_pln * qty

        total_purchased_revenue = greenhouse_revenue + accessory_revenue
        roas = (greenhouse_revenue / cost) if (cost > 0 and units_sold > 0) else None
        cost_per_sale = (cost / units_sold) if units_sold > 0 else None

        rows.append({
            "product_id":         int(product_id),
            "product_name":       product_name or f"ID {product_id}",
            "is_greenhouse":      is_greenhouse(product_name) if product_name else None,
            "impressions":        impressions,
            "clicks":             clicks,
            "cost":               round(cost, 2),
            "orders_total":       len(order_ids),
            "orders_cancelled":   cancelled,
            "orders_pending":     pending,
            "orders_purchased":   len(order_ids) - cancelled - pending,
            "units_sold":         units_sold,
            "greenhouse_revenue": round(greenhouse_revenue, 2),
            "accessory_revenue":  round(accessory_revenue, 2),
            "roas":               round(roas, 2) if roas is not None else None,
            "cost_per_sale":      round(cost_per_sale, 2) if cost_per_sale is not None else None,
        })

    rows.sort(key=lambda r: r["cost"], reverse=True)

    # Campaign totals (greenhouses only)
    total_cost       = sum(r["cost"] for r in rows)
    total_impr       = sum(r["impressions"] for r in rows)
    total_clicks     = sum(r["clicks"] for r in rows)
    total_units      = sum(r["units_sold"] for r in rows)
    total_gh_revenue = sum(r["greenhouse_revenue"] for r in rows)
    total_roas       = round(total_gh_revenue / total_cost, 2) if total_cost > 0 else None
    total_cps        = round(total_cost / total_units, 2) if total_units > 0 else None

    return {
        "rows":    rows,
        "totals": {
            "cost":               round(total_cost, 2),
            "impressions":        total_impr,
            "clicks":             total_clicks,
            "units_sold":         total_units,
            "greenhouse_revenue": round(total_gh_revenue, 2),
            "roas":               total_roas,
            "cost_per_sale":      total_cps,
        },
        "period": {
            "from": str(df["Data"].min())[:10] if len(df) else "",
            "to":   str(df["Data"].max())[:10] if len(df) else "",
        },
    }
