import logging
import threading
import webbrowser
import os
from datetime import datetime, timezone

from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session)

import db
import poller
import report as rpt
from erli_api import get_order

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-12s %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024  # 32 MB
app.secret_key = os.environ.get("SECRET_KEY", "erli-local-dev-secret")

# Password protection: set APP_PASSWORD env var on the server to enable.
# Locally (no env var) the app stays open as before.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.before_request
def require_login():
    if not APP_PASSWORD:
        return
    if request.endpoint in ("login", "static"):
        return
    if not session.get("authed"):
        return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password", "") == APP_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = "Неверный пароль"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    files = db.list_xlsx()
    selected  = request.args.get("file", files[0] if files else None)
    date_from = request.args.get("from", "")
    date_to   = request.args.get("to",   "")
    report_data = None
    file_min = file_max = ""
    error = None

    if selected:
        content = db.get_xlsx(selected)
        if content:
            try:
                df = rpt.load_xlsx(content)
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

    content = f.read()
    now = datetime.now(timezone.utc).isoformat()
    db.save_xlsx(f.filename, content, now)

    try:
        df = rpt.load_xlsx(content)
        result = rpt.import_orders_from_xlsx(df)
        return jsonify({"ok": True, "filename": f.filename, "orders": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/poller")
def api_poller():
    return jsonify(poller.get_status())


# ── Startup ──────────────────────────────────────────────────────────────────

def _import_local_uploads():
    """One-time migration: move xlsx files from uploads/ into the DB."""
    if not os.path.isdir(UPLOAD_DIR):
        return
    existing = set(db.list_xlsx())
    for fname in os.listdir(UPLOAD_DIR):
        if fname.endswith(".xlsx") and fname not in existing:
            path = os.path.join(UPLOAD_DIR, fname)
            with open(path, "rb") as fh:
                db.save_xlsx(fname, fh.read(),
                             datetime.now(timezone.utc).isoformat())
            logging.getLogger("startup").info(f"Imported {fname} into DB")


def _startup():
    db.init_db()
    _import_local_uploads()
    poller.start()


_startup()  # runs on import — works under gunicorn and local `python app.py`


if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    print("\n  ERLI Monitor →  http://127.0.0.1:5000\n")
    app.run(debug=False, port=5000)
