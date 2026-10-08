"""
Background poller.
Runs in a daemon thread:
  - polls ERLI /inbox every POLL_INTERVAL seconds for instant events;
  - every SYNC_INTERVAL pulls every order updated since the last sync, which
    also catches whatever the inbox missed.
"""

import threading
import time
import logging
from datetime import datetime, timezone, timedelta

from erli_api import get_inbox, mark_inbox_read, get_order, search_orders_since
from db import upsert_order, upsert_orders, get_sync_state, set_sync_state

log = logging.getLogger("poller")

POLL_INTERVAL     = 60                      # seconds between inbox checks
SYNC_INTERVAL     = 600                     # seconds between change syncs
SYNC_OVERLAP      = timedelta(minutes=10)   # re-read a little, never miss an edge
FIRST_SYNC_WINDOW = timedelta(days=1)       # first auto-sync with no checkpoint
_status = {
    "last_poll":    "never",
    "last_event":   "—",
    "events_total": 0,
    "last_sync":    "never",
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
                upsert_order(order, source="inbox")
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


def _api_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync_orders(field: str, since: datetime) -> dict:
    """
    Pull every order whose `field` ('created' or 'updated') is on or after
    `since` and save it. One API request per 200 orders, one DB connection
    per page — the old refresh made a request and two connections per order,
    which on Render ran into gunicorn's timeout.
    """
    totals = {"checked": 0, "new": 0, "updated": 0, "pages": 0}
    for page in search_orders_since(field, _api_time(since)):
        # source='sync' keeps these orders out of report deletion: they are
        # real shop orders, not something a report brought in
        r = upsert_orders(page, source="sync")
        totals["checked"] += r["saved"]
        totals["new"]     += r["new"]
        totals["updated"] += r["changed"]
        totals["pages"]   += 1
    log.info(f"Sync by {field} since {_api_time(since)}: {totals}")
    return totals


def sync_changes() -> dict:
    """Pull everything changed since the previous run (auto-sync)."""
    started = datetime.now(timezone.utc)
    last = get_sync_state("last_sync_updated")
    since = datetime.fromisoformat(last) if last else started - FIRST_SYNC_WINDOW
    result = sync_orders("updated", since - SYNC_OVERLAP)
    set_sync_state("last_sync_updated", started.isoformat())
    _update(last_sync=started.strftime("%d.%m.%Y %H:%M:%S"))
    return result


def _loop():
    _update(running=True)
    log.info("Inbox poller started")
    last_sync = 0.0
    while True:
        poll_once()
        if time.time() - last_sync >= SYNC_INTERVAL:
            try:
                sync_changes()
            except Exception as exc:
                log.error(f"Auto-sync error: {exc}")
                _update(error=f"sync: {exc}")
            last_sync = time.time()
        time.sleep(POLL_INTERVAL)


def start():
    """Start the background polling thread (daemon — dies with main process)."""
    t = threading.Thread(target=_loop, daemon=True, name="inbox-poller")
    t.start()
    log.info(f"Poller thread started (interval={POLL_INTERVAL}s)")
