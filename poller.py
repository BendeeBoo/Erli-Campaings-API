"""
Background inbox poller.
Runs in a daemon thread — polls ERLI /inbox every POLL_INTERVAL seconds.
New orders/status-changes are saved to SQLite immediately.
"""

import threading
import time
import logging

from erli_api import get_inbox, mark_inbox_read, get_order
from db import upsert_order, get_sync_state, set_sync_state, get_order_ids_since

log = logging.getLogger("poller")

POLL_INTERVAL    = 60     # seconds between inbox checks
REFRESH_INTERVAL = 1800   # seconds between full status refreshes (30 min)
REFRESH_DAYS     = 60     # how far back to re-check order statuses
_status = {
    "last_poll":    "never",
    "last_event":   "—",
    "events_total": 0,
    "running":      False,
    "error":        None,
}
_lock = threading.Lock()


def get_status() -> dict:
    with _lock:
        return dict(_status)


def _update(**kw):
    with _lock:
        _status.update(kw)


def _process_events(events: list[dict]) -> int:
    """Process inbox events, return count of new/updated orders."""
    processed = 0
    for event in events:
        etype   = event.get("type") or event.get("eventType") or ""
        payload = event.get("payload") or event.get("data") or {}

        order_id = None
        if etype in ("orderCreated", "orderStatusChanged"):
            order_id = payload.get("orderId") or payload.get("id")
        elif isinstance(payload, dict) and payload.get("orderId"):
            order_id = payload["orderId"]

        if order_id:
            order = get_order(order_id)
            if order:
                upsert_order(order)
                processed += 1
                log.info(f"Saved order {order_id} ({etype})")

    return processed


def poll_once() -> dict:
    """
    Single polling cycle. Returns result summary.
    Can be called manually from the web UI (refresh button).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S")

    try:
        events = get_inbox()
        _update(last_poll=now, error=None)

        if not events:
            log.debug("Inbox empty")
            return {"events": 0, "orders": 0}

        count = _process_events(events)

        # Mark events as read using the id of the last event
        last_id = None
        for e in events:
            eid = e.get("id") or e.get("messageId")
            if eid is not None:
                last_id = eid

        if last_id is not None:
            mark_inbox_read(last_id)
            set_sync_state("last_inbox_id", str(last_id))

        total = _status["events_total"] + len(events)
        last_ev = events[-1].get("type") or "unknown"
        _update(events_total=total, last_event=f"{last_ev} @ {now}")

        log.info(f"Polled inbox: {len(events)} events, {count} orders saved")
        return {"events": len(events), "orders": count}

    except Exception as exc:
        log.error(f"Polling error: {exc}")
        _update(error=str(exc), last_poll=now)
        return {"events": 0, "orders": 0, "error": str(exc)}


def refresh_statuses(days: int = REFRESH_DAYS) -> dict:
    """
    Re-fetch every order created in the last `days` days from the API
    and update its status in the DB. Returns summary.
    """
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)) \
        .strftime("%Y-%m-%dT00:00:00Z")

    ids = get_order_ids_since(cutoff)
    checked, updated, errors = 0, 0, 0

    from db import get_order_row
    for oid in ids:
        try:
            order = get_order(oid)
            if not order:
                errors += 1
                continue
            row = get_order_row(oid)
            old = (row["status"], row["seller_status"]) if row else (None, None)
            new = (order.get("status"), order.get("sellerStatus") or "")
            upsert_order(order)
            checked += 1
            if old != new:
                updated += 1
                log.info(f"Status change {oid}: {old} → {new}")
        except Exception as exc:
            errors += 1
            log.warning(f"Refresh failed for {oid}: {exc}")

    log.info(f"Status refresh: {checked} checked, {updated} changed, {errors} errors")
    return {"checked": checked, "updated": updated, "errors": errors}


def _loop():
    _update(running=True)
    log.info("Inbox poller started")
    last_refresh = 0.0
    while True:
        poll_once()
        if time.time() - last_refresh >= REFRESH_INTERVAL:
            try:
                refresh_statuses()
            except Exception as exc:
                log.error(f"Auto-refresh error: {exc}")
            last_refresh = time.time()
        time.sleep(POLL_INTERVAL)


def start():
    """Start the background polling thread (daemon — dies with main process)."""
    t = threading.Thread(target=_loop, daemon=True, name="inbox-poller")
    t.start()
    log.info(f"Poller thread started (interval={POLL_INTERVAL}s)")
