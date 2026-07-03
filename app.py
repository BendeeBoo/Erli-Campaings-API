import logging
import threading
import webbrowser
import os
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify, redirect, url_for

import db
import poller
import report as rpt
from erli_api import get_order, search_orders

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-12s %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB


# API sellerStatus → Polish label (as shown in the ERLI shop panel)
SELLER_STATUS_PL = {
    "":                  "Bez wartości",
    "cancelled":         "Anulowane",
    "canceled":          "Anulowane",
    "inProgress":        "W trakcie realizacji",
    "returned":          "Zwrócone",
    "created":           "Utworzone",
    "readyForPickup":    "Gotowe do odbioru",
    "readyToPickup":     "Gotowe do odbioru",
    "readyToProcess":    "Gotowe do realizacji",
    "pickedUp":          "Odebrane",
    "received":          "Odebrane",
    "returningToSender": "Wraca do nadawcy",
    "sent":              "Wysłane",
    "shipped":           "Wysłane",
    "unknown":           "Nieznany",
}


def fmt_price(grosz):
    if grosz is None:
        return "—"
    return f"{grosz / 100:.2f} zł"


def fmt_date(iso):
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso


def enrich(row: dict) -> dict:
    row["total_fmt"]  = fmt_price(row.get("total"))
    row["date_fmt"]   = fmt_date(row.get("created_at"))
    ss = row.get("seller_status") or ""
    row["seller_status_pl"] = SELLER_STATUS_PL.get(ss, ss or "Bez wartości")
    return row


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    status_filter = request.args.get("status", "")
    after_filter  = request.args.get("after", "")
    limit         = int(request.args.get("limit", 200))

    after_iso = (after_filter + "T00:00:00Z") if after_filter else None

    orders = [enrich(o) for o in db.get_orders(
        status=status_filter or None,
        after=after_iso,
        limit=limit,
    )]

    raw_stats = db.get_stats()
    stats = {
        "total":     raw_stats.get("total") or 0,
        "purchased": raw_stats.get("purchased") or 0,
        "cancelled": raw_stats.get("cancelled") or 0,
        "pending":   raw_stats.get("pending") or 0,
        "revenue":   fmt_price(raw_stats.get("revenue") or 0),
    }

    return render_template(
        "index.html",
        orders=orders,
        stats=stats,
        status_filter=status_filter,
        after_filter=after_filter,
        limit=limit,
        poller=poller.get_status(),
    )


@app.route("/api/poll", methods=["POST"])
def manual_poll():
    """Trigger a manual inbox poll (called from UI refresh button)."""
    result = poller.poll_once()
    return jsonify(result)


@app.route("/api/order/add", methods=["POST"])
def add_order_by_id():
    """Manually add an order by ID."""
    data     = request.get_json(silent=True) or {}
    order_id = (data.get("id") or "").strip()
    if not order_id:
        return jsonify({"error": "ID not provided"}), 400

    order = get_order(order_id)
    if not order:
        return jsonify({"error": f"Order {order_id} not found"}), 404

    db.upsert_order(order)
    return jsonify({"ok": True, "id": order_id, "status": order.get("status")})


@app.route("/api/orders/refresh", methods=["POST"])
def refresh_order_statuses():
    """Re-fetch recent orders from the API and update their statuses."""
    data = request.get_json(silent=True) or {}
    days = int(data.get("days", 60))
    result = poller.refresh_statuses(days=days)
    return jsonify(result)


@app.route("/api/order/delete", methods=["POST"])
def delete_order():
    data     = request.get_json(silent=True) or {}
    order_id = (data.get("id") or "").strip()
    if not order_id:
        return jsonify({"error": "ID not provided"}), 400
    if db.delete_order(order_id):
        return jsonify({"ok": True, "id": order_id})
    return jsonify({"error": f"Order {order_id} not found"}), 404


@app.route("/api/orders/delete-before", methods=["POST"])
def delete_orders_before():
    data   = request.get_json(silent=True) or {}
    before = (data.get("before") or "").strip()
    if not before:
        return jsonify({"error": "Date not provided"}), 400
    count = db.delete_orders_before(before + "T00:00:00")
    return jsonify({"ok": True, "deleted": count})


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats())


@app.route("/analytics")
def analytics():
    # List uploaded xlsx files
    files = sorted(
        [f for f in os.listdir(UPLOAD_DIR) if f.endswith(".xlsx")],
        reverse=True,
    )
    selected  = request.args.get("file", files[0] if files else None)
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to",   "")
    report_data = None
    file_min = file_max = ""
    error = None

    if selected:
        path = os.path.join(UPLOAD_DIR, selected)
        if os.path.exists(path):
            try:
                df = rpt.load_xlsx(path)
                import pandas as pd
                dates = pd.to_datetime(df["Data"])
                file_min = str(dates.min())[:10]
                file_max = str(dates.max())[:10]
                report_data = rpt.build_report(
                    df,
                    date_from=date_from or None,
                    date_to=date_to or None,
                )
            except Exception as e:
                error = str(e)

    return render_template(
        "analytics.html",
        files=files,
        selected=selected,
        date_from=date_from,
        date_to=date_to,
        file_min=file_min,
        file_max=file_max,
        report=report_data,
        error=error,
    )


@app.route("/api/upload-xlsx", methods=["POST"])
def upload_xlsx():
    f = request.files.get("file")
    if not f or not f.filename.endswith(".xlsx"):
        return jsonify({"error": "Need an .xlsx file"}), 400

    save_path = os.path.join(UPLOAD_DIR, f.filename)
    f.save(save_path)

    try:
        df = rpt.load_xlsx(save_path)
        result = rpt.import_orders_from_xlsx(df)
        return jsonify({"ok": True, "filename": f.filename, "orders": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poller")
def api_poller():
    return jsonify(poller.get_status())


# ── Startup ──────────────────────────────────────────────────────────────────

def seed_db():
    """
    On first launch: load orders from _search into DB as historical seed.
    (Returns 2021 data for this account — still useful as baseline.)
    """
    existing = db.get_stats().get("total") or 0
    if existing > 0:
        return
    logging.getLogger("seed").info("Seeding DB from /orders/_search …")
    orders = search_orders(limit=100)
    for o in orders:
        db.upsert_order(o)
    logging.getLogger("seed").info(f"Seeded {len(orders)} orders")


if __name__ == "__main__":
    db.init_db()
    seed_db()
    poller.start()

    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    print("\n  ERLI Monitor →  http://127.0.0.1:5000\n")
    app.run(debug=False, port=5000)
