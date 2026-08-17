import logging
import threading
import webbrowser
import os
from datetime import datetime, timezone

import pandas as pd
from flask import (Flask, render_template, request, jsonify,
                   redirect, url_for, session)
from werkzeug.exceptions import HTTPException

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

log = logging.getLogger("app")

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
        "returned":  raw_stats.get("returned") or 0,
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

    db.upsert_order(order, source="manual")
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
    # An explicit empty ?file= means "show nothing" (used after a report is deleted)
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
        files_meta=db.list_xlsx_meta(),
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
        dates = pd.to_datetime(df["Data"])
        db.set_xlsx_index(f.filename, sorted(rpt.order_ids_in_xlsx(df)),
                          str(dates.min())[:10], str(dates.max())[:10])
        result = rpt.import_orders_from_xlsx(df)
        return jsonify({"ok": True, "filename": f.filename, "orders": result})
    except Exception as e:
        log.exception("xlsx upload failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/orders/fetch-missing", methods=["POST"])
def fetch_missing_orders():
    """Pull orders a report mentions but the DB does not have yet."""
    name = ((request.get_json(silent=True) or {}).get("file") or "").strip()
    if not name or name not in set(db.list_xlsx()):
        return jsonify({"error": "Report not found"}), 404

    missing = _report_order_ids(name) - db.get_all_order_ids()
    fetched, errors = 0, 0
    for oid in sorted(missing):
        try:
            order = get_order(oid)
            if order:
                db.upsert_order(order, source="xlsx")
                fetched += 1
            else:
                errors += 1
        except Exception:
            log.exception(f"Fetching order {oid} failed")
            errors += 1

    return jsonify({"ok": True, "requested": len(missing),
                    "fetched": fetched, "errors": errors})


@app.route("/api/poller")
def api_poller():
    return jsonify(poller.get_status())


# ── Report (xlsx) deletion ───────────────────────────────────────────────────

def _index_report(name: str) -> dict:
    """
    Order ids + period of a stored report, read from the cache written at
    upload time. Reports uploaded before the cache existed are indexed once,
    on first use. Unreadable file → empty index.
    """
    cached = db.get_xlsx_index(name)
    if cached is not None:
        return cached

    content = db.get_xlsx(name)
    if not content:
        return {"order_ids": [], "from": "", "to": ""}
    try:
        df = rpt.load_xlsx(content)
        ids = sorted(rpt.order_ids_in_xlsx(df))
        dates = pd.to_datetime(df["Data"])
        index = {"order_ids": ids,
                 "from": str(dates.min())[:10], "to": str(dates.max())[:10]}
    except Exception:
        log.exception(f"Cannot index report {name}")
        return {"order_ids": [], "from": "", "to": ""}

    db.set_xlsx_index(name, index["order_ids"], index["from"], index["to"])
    return index


def _report_order_ids(name: str) -> set[str]:
    return set(_index_report(name)["order_ids"])


def _plan_deletion(names: list[str]) -> dict:
    """
    Work out what deleting the given reports would remove.

    An order is only removed when it came from an xlsx import *and* no report
    that survives the deletion still refers to it.
    """
    known = set(db.list_xlsx())
    targets = [n for n in names if n in known]
    survivors = [n for n in known if n not in set(targets)]

    doomed_ids: set[str] = set()
    per_file = []
    for name in targets:
        index = _index_report(name)
        doomed_ids |= set(index["order_ids"])
        per_file.append({
            "name":   name,
            "from":   index["from"],
            "to":     index["to"],
            "orders": len(index["order_ids"]),
        })

    still_referenced: set[str] = set()
    for name in survivors:
        still_referenced |= _report_order_ids(name)

    sources = db.get_order_sources(sorted(doomed_ids))
    deletable, kept_shared, kept_manual = [], 0, 0
    for oid in doomed_ids:
        if oid not in sources:          # never made it into the DB
            continue
        if oid in still_referenced:
            kept_shared += 1
        elif sources[oid] != "xlsx":
            kept_manual += 1
        else:
            deletable.append(oid)

    return {
        "files":        per_file,
        "missing":      [n for n in names if n not in known],
        "orders_delete": sorted(deletable),
        "orders_kept_shared": kept_shared,
        "orders_kept_manual": kept_manual,
    }


@app.errorhandler(Exception)
def _json_errors(exc):
    """
    API routes must fail as JSON. An HTML error page made the client report
    "Ошибка сети", hiding the real cause. Also logs the traceback so it shows
    up in the Render logs.
    """
    if isinstance(exc, HTTPException):
        return exc
    log.exception(f"Unhandled error on {request.method} {request.path}")
    if request.path.startswith("/api/"):
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    raise exc


@app.route("/api/reports/preview-delete", methods=["POST"])
def preview_delete_reports():
    """What would be removed — shown in the confirmation dialog."""
    names = (request.get_json(silent=True) or {}).get("files") or []
    if not names:
        return jsonify({"error": "No files selected"}), 400

    plan = _plan_deletion(names)
    if not plan["files"]:
        return jsonify({"error": "Files not found"}), 404

    return jsonify({
        "files":              plan["files"],
        "orders_delete":      len(plan["orders_delete"]),
        "orders_kept_shared": plan["orders_kept_shared"],
        "orders_kept_manual": plan["orders_kept_manual"],
    })


@app.route("/api/reports/delete", methods=["POST"])
def delete_reports():
    data  = request.get_json(silent=True) or {}
    names = data.get("files") or []
    also_orders = bool(data.get("delete_orders"))
    if not names:
        return jsonify({"error": "No files selected"}), 400

    # Recomputed server-side — the client is never trusted with the order list
    plan = _plan_deletion(names)
    if not plan["files"]:
        return jsonify({"error": "Files not found"}), 404

    deleted_orders = db.delete_orders(plan["orders_delete"]) if also_orders else 0

    deleted_files, disk_warnings = [], []
    for entry in plan["files"]:
        name = entry["name"]
        if db.delete_xlsx(name):
            deleted_files.append(name)
        # Also drop the local copy, otherwise _import_local_uploads() would
        # resurrect the report on the next restart.
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as exc:
                disk_warnings.append(f"{name}: {exc}")

    return jsonify({
        "ok":             True,
        "deleted_files":  deleted_files,
        "deleted_orders": deleted_orders,
        "warnings":       disk_warnings,
        "remaining":      db.list_xlsx(),
    })


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


def _backfill_order_sources():
    """
    One-time migration: orders stored before the `source` column existed.
    Anything mentioned in a stored report counts as an xlsx import; the rest
    was added by hand or arrived through the inbox, so it is protected from
    report deletion.
    """
    unknown = db.get_orders_without_source()
    if not unknown:
        return

    from_xlsx: set[str] = set()
    for name in db.list_xlsx():
        from_xlsx |= _report_order_ids(name)

    for oid in unknown:
        db.set_order_source(oid, "xlsx" if oid in from_xlsx else "manual")
    logging.getLogger("startup").info(
        f"Backfilled source for {len(unknown)} orders "
        f"({len(set(unknown) & from_xlsx)} from reports)"
    )


def _startup():
    db.init_db()
    _import_local_uploads()
    _backfill_order_sources()
    poller.start()


_startup()  # runs on import — works under gunicorn and local `python app.py`


if __name__ == "__main__":
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    print("\n  ERLI Monitor →  http://127.0.0.1:5000\n")
    app.run(debug=False, port=5000)
