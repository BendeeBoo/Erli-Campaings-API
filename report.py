"""
Report engine: joins xlsx ad data with orders from DB.
"""

import io
import json
import pandas as pd

import db
from db import upsert_order
from erli_api import get_order

GREENHOUSE_KEYWORDS = ["szklarnia", "tunel", "domek"]
ACCESSORY_KEYWORDS  = ["okno boczne", "okno do szklarni", "taśma", "tasma",
                        "ramka", "kotwy", "agrotkanina", "agrowłóknina",
                        "fundament", "śruba", "sruba", "grunt", "folia"]


# sellerStatus values meaning "the money is gone". The order still reads
# status='purchased' — the buyer paid — but it was (or is being) sent back,
# so it must not count towards ad revenue. The API spells these
# inconsistently, hence the lowercase comparison.
RETURNED_SELLER_STATUSES = {"returned", "returningtosender", "returnedtosender"}


def is_returned(seller_status: str | None) -> bool:
    return (seller_status or "").strip().lower() in RETURNED_SELLER_STATUSES


def is_greenhouse(name: str) -> bool:
    n = (name or "").lower()
    # Accessory check takes priority
    if any(kw in n for kw in ACCESSORY_KEYWORDS):
        return False
    return any(kw in n for kw in GREENHOUSE_KEYWORDS)


def _item_id(item: dict) -> int | None:
    """Order line-item id, comparable with the xlsx 'ID produktu' column."""
    try:
        return int(item.get("id"))
    except (TypeError, ValueError):
        return None


def parse_order_ids(cell) -> list[str]:
    if pd.isna(cell) or not str(cell).strip():
        return []
    return [x.strip() for x in str(cell).split(",") if x.strip()]


def load_xlsx(source) -> pd.DataFrame:
    """source: file path, bytes, or file-like object."""
    if isinstance(source, bytes):
        source = io.BytesIO(source)
    df = pd.read_excel(source, header=0)
    df.columns = [c.strip() for c in df.columns]
    return df


def order_ids_in_xlsx(df: pd.DataFrame) -> set[str]:
    """Every order id mentioned anywhere in the report."""
    all_ids: set[str] = set()
    for cell in df["Lista zamowien"].dropna():
        all_ids.update(parse_order_ids(cell))
    return all_ids


def import_orders_from_xlsx(df: pd.DataFrame) -> dict:
    """Fetch all order IDs found in xlsx and save to DB. Returns summary."""
    all_ids = order_ids_in_xlsx(df)

    fetched, skipped, errors = 0, 0, 0
    existing = db.get_all_order_ids()

    for oid in all_ids:
        if oid in existing:
            skipped += 1
            continue
        try:
            order = get_order(oid)
            if order:
                upsert_order(order, source="xlsx")
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
    for raw_str in db.get_all_raws():
        try:
            raw = json.loads(raw_str or "{}")
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
    db_orders = db.get_all_orders()

    # Apply date filter
    df = df.copy()
    df["Data"] = pd.to_datetime(df["Data"])
    if date_from:
        df = df[df["Data"] >= pd.to_datetime(date_from)]
    if date_to:
        df = df[df["Data"] <= pd.to_datetime(date_to)]

    name_map = _product_name_map()

    # Order ids the xlsx refers to but the DB has never seen. They would
    # silently drag every figure down, so the UI warns about them.
    missing_orders: set[str] = set()

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
        returned           = 0
        returned_revenue   = 0.0
        accessory_revenue  = 0.0
        no_match           = 0

        pid = int(product_id)
        # Pre-fill name from DB map if available
        product_name = name_map.get(pid, "")

        for oid in order_ids:
            order = db_orders.get(oid)
            if not order:
                missing_orders.add(oid)
                continue

            status = order.get("status", "")
            if status == "cancelled":
                cancelled += 1
                continue
            if status == "pending":
                pending += 1
                continue

            # Only the line item this ad actually sold. An order listed under
            # two products used to have its whole basket counted for each of
            # them, turning one greenhouse into two.
            raw = json.loads(order.get("raw") or "{}")
            matched = [it for it in raw.get("items", []) if _item_id(it) == pid]
            if not matched:
                no_match += 1
                continue

            item_revenue = 0.0
            item_units   = 0
            item_is_gh   = False
            for item in matched:
                qty = item.get("quantity", 1)
                item_revenue += ((item.get("unitPrice") or 0) / 100) * qty
                if is_greenhouse(item.get("name", "")):
                    item_is_gh  = True
                    item_units += qty
                if not product_name:
                    product_name = item.get("name", "")

            if is_returned(order.get("seller_status")):
                returned += 1
                if item_is_gh:
                    returned_revenue += item_revenue
                continue

            if item_is_gh:
                greenhouse_revenue += item_revenue
                units_sold         += item_units
            else:
                accessory_revenue  += item_revenue

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
            "orders_returned":    returned,
            "orders_no_match":    no_match,
            "orders_purchased":   len(order_ids) - cancelled - pending - returned,
            "units_sold":         units_sold,
            "greenhouse_revenue": round(greenhouse_revenue, 2),
            "returned_revenue":   round(returned_revenue, 2),
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
    total_returned   = sum(r["orders_returned"] for r in rows)
    total_ret_revenue = sum(r["returned_revenue"] for r in rows)
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
            "orders_returned":    total_returned,
            "returned_revenue":   round(total_ret_revenue, 2),
            "roas":               total_roas,
            "cost_per_sale":      total_cps,
        },
        "missing_orders": sorted(missing_orders),
        "period": {
            "from": str(df["Data"].min())[:10] if len(df) else "",
            "to":   str(df["Data"].max())[:10] if len(df) else "",
        },
    }
