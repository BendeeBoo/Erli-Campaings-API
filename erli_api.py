import json
import os
import urllib.request
import urllib.error


def _load_api_key() -> str:
    """API key comes from config.json (gitignored) or the ERLI_API_KEY env var."""
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            key = json.load(f).get("api_key", "")
            if key:
                return key
    key = os.environ.get("ERLI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "API key not found: create config.json (see config.example.json) "
            "or set the ERLI_API_KEY environment variable"
        )
    return key


API_KEY  = _load_api_key()
BASE_URL = "https://erli.pl/svc/shop-api"
HEADERS  = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type":  "application/json",
    "Accept":        "application/json",
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


def _request(method: str, path: str, body: dict | None = None, params: dict | None = None):
    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, headers=HEADERS, method=method)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_inbox() -> list[dict]:
    """Fetch up to 500 unread inbox events."""
    try:
        result = _request("GET", "/inbox")
        return result if isinstance(result, list) else result.get("value", [])
    except urllib.error.HTTPError:
        return []


def mark_inbox_read(last_id: int | str):
    """Mark inbox events as read up to last_id."""
    # Try known patterns - we'll discover the right one on first real event
    for method, path, body in [
        ("DELETE", f"/inbox/{last_id}", None),
        ("DELETE", "/inbox", {"lastId": last_id}),
        ("POST",   "/inbox/read", {"lastId": last_id}),
        ("PATCH",  "/inbox", {"lastId": last_id}),
    ]:
        try:
            _request(method, path, body)
            return True
        except urllib.error.HTTPError as e:
            if e.code not in (404, 405):
                raise
    return False


def get_order(order_id: str) -> dict | None:
    """Fetch a single order by ID."""
    try:
        return _request("GET", f"/orders/{order_id}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


SEARCH_PAGE_SIZE = 200   # API maximum for pagination.limit


def search_orders_since(field: str, since_iso: str):
    """
    Yield pages of orders whose `field` ('created' or 'updated') is on or
    after `since_iso`, newest first.

    An empty search body makes the API start from the very first order (2021
    for this shop), which is what once looked like a "stuck cursor". It needs
    an explicit filter + sort; paging goes through each order's own `cursor`
    value passed back as pagination.after.
    """
    after = None
    while True:
        pagination = {"sortField": field, "order": "DESC",
                      "limit": SEARCH_PAGE_SIZE}
        if after:
            pagination["after"] = after
        page = _request("POST", "/orders/_search", {
            "filter": {"field": field, "operator": ">=", "value": since_iso},
            "pagination": pagination,
        })
        if isinstance(page, dict):
            page = page.get("value", [])
        if not page:
            return
        yield page
        if len(page) < SEARCH_PAGE_SIZE:
            return
        after = page[-1].get("cursor")
        if not after:
            return
