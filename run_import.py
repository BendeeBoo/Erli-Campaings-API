import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import db; db.init_db()
import report as rpt

XLSX = os.path.join(os.path.dirname(__file__), "uploads", "statystyki_2026-06-01_2026-06-21.xlsx")

df = rpt.load_xlsx(XLSX)
result = rpt.import_orders_from_xlsx(df)
print("Import:", result)

r = rpt.build_report(df)
t = r["totals"]
print()
print(f"Period      : {r['period']['from']} — {r['period']['to']}")
print(f"Impressions : {t['impressions']:,}")
print(f"Ad spend    : {t['cost']} zł")
print(f"GH revenue  : {t['greenhouse_revenue']} zł")
print(f"ROAS        : {t['roas']}x")
print(f"Units sold  : {t['units_sold']} шт.")
print(f"Cost/sale   : {t['cost_per_sale']} zł")
print()
print("Per-product (with orders):")
for row in r["rows"]:
    if row["orders_total"] > 0:
        tag = "GH " if row["is_greenhouse"] else "ACC"
        name = row["product_name"][:48]
        print(f"  [{tag}] {name:48s} cost={row['cost']:7.2f}  units={row['units_sold']}  roas={row['roas']}  cps={row['cost_per_sale']}")
