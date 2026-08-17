"""
ERLI Orders Viewer
Usage:
  python orders.py                  # all orders
  python orders.py --status purchased
  python orders.py --status cancelled
  python orders.py --limit 20
  python orders.py --after "2024-01-01"
  python orders.py --json           # raw JSON output
"""

import json
import sys
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Force UTF-8 output on Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Key comes from config.json (gitignored) or the ERLI_API_KEY env var —
# never hardcode it here, this file is in git.
from erli_api import API_KEY, BASE_URL

STATUS_LABELS = {
    "purchased": "ОПЛАЧЕН",
    "pending":   "ОЖИДАЕТ",
    "cancelled": "ОТМЕНЁН",
}

STATUS_COLORS = {
    "purchased": "\033[32m",   # green
    "pending":   "\033[33m",   # yellow
    "cancelled": "\033[31m",   # red
}
RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"


def api_post(path: str, body: dict, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


def fetch_orders(status: str | None, after_date: str | None, limit: int) -> list[dict]:
    payload: dict = {}
    if status:
        payload["status"] = status
    if after_date:
        payload["updatedFrom"] = after_date
    params = {"limit": limit} if limit else {}

    result = api_post("/orders/_search", payload, params)
    if isinstance(result, list):
        return result
    return result.get("value", [])


def fmt_price(grosz: int | None) -> str:
    if grosz is None:
        return "—"
    return f"{grosz / 100:.2f} zł"


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y  %H:%M")
    except Exception:
        return iso


def print_order(order: dict) -> None:
    oid    = order.get("id", "?")
    status = order.get("status", "")
    color  = STATUS_COLORS.get(status, "")
    label  = STATUS_LABELS.get(status, status.upper())

    addr   = order.get("user", {}).get("deliveryAddress", {})
    name   = f"{addr.get('firstName', '')} {addr.get('lastName', '')}".strip()
    city   = addr.get("city", "")

    items  = order.get("items", [])
    total  = fmt_price(order.get("totalPrice"))
    date   = fmt_date(order.get("purchasedAt") or order.get("created"))
    seller_status = order.get("sellerStatus", "")

    print(f"{BOLD}Заказ #{oid}{RESET}  {color}{label}{RESET}  {DIM}{date}{RESET}")
    print(f"  Покупатель : {name}, {city}")
    print(f"  Статус у продавца: {seller_status or '—'}")
    print(f"  Товары ({len(items)}):")
    for item in items:
        qty   = item.get("quantity", 1)
        iname = item.get("name", "—")
        sku   = item.get("sku") or ""
        price = fmt_price(item.get("unitPrice"))
        sku_str = f"  [{sku}]" if sku else ""
        print(f"    • {qty}x  {iname}{sku_str}  — {price}/шт.")
    print(f"  {'─'*40}")
    print(f"  Итого: {BOLD}{total}{RESET}")
    print()


def print_summary(orders: list[dict]) -> None:
    total_val = sum(
        o.get("totalPrice", 0) or 0
        for o in orders
        if o.get("status") != "cancelled"
    )
    by_status: dict[str, int] = {}
    for o in orders:
        s = o.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1

    print(f"{BOLD}{'═'*50}{RESET}")
    print(f"  Всего заказов : {len(orders)}")
    for s, cnt in by_status.items():
        label = STATUS_LABELS.get(s, s)
        color = STATUS_COLORS.get(s, "")
        print(f"    {color}{label}{RESET}: {cnt}")
    print(f"  Выручка (без отменённых): {BOLD}{fmt_price(total_val)}{RESET}")
    print(f"{BOLD}{'═'*50}{RESET}")


def main():
    parser = argparse.ArgumentParser(description="ERLI Orders Viewer")
    parser.add_argument("--status", choices=["purchased", "pending", "cancelled"],
                        help="Фильтр по статусу")
    parser.add_argument("--after", metavar="YYYY-MM-DD",
                        help="Заказы обновлённые после даты")
    parser.add_argument("--limit", type=int, default=50,
                        help="Максимум заказов (по умолчанию 50)")
    parser.add_argument("--json", dest="raw_json", action="store_true",
                        help="Вывод сырого JSON")
    args = parser.parse_args()

    orders = fetch_orders(args.status, args.after, args.limit)

    if args.raw_json:
        print(json.dumps(orders, ensure_ascii=False, indent=2))
        return

    if not orders:
        print("Заказов не найдено.")
        return

    for order in orders:
        print_order(order)

    print_summary(orders)


if __name__ == "__main__":
    main()
