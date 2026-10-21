"""Deterministic synthetic data, isolated from the live database."""
import random
from datetime import timedelta

from .core import INTERVALS, PACIFIC, iso


def seed(store, at):
    day = at.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    if store.get("meta", "demo_day") == day.date().isoformat() and store.get("product", "TRAIL-24", {}).get("warehouse_units") is not None:
        for source in (*INTERVALS, "sales", "warehouse"):
            store.source(source, success=iso(at), state="ready", error=None)
        store.cover("orders", day, at)
        return
    rng = random.Random(17)
    catalog = [
        ("TRAIL-24", "Trail bottle · 24 oz", "B0DEMO0001", 18, 3, 0, 0, 120, 0, 2899, 12, 2400),
        ("DESK-OAK", "Oak desktop organizer", "B0DEMO0002", 164, 12, 2, 40, 0, 32, 4499, 6, 2400),
        ("CABLE-3", "Cable keeper · 3 pack", "B0DEMO0003", 420, 9, 4, 0, 200, 0, 1299, 24, 2400),
        ("LAMP-MINI", "Mini reading lamp", "B0DEMO0004", 0, 0, 2, 0, 60, 0, 3299, 4, 8),
        ("POUCH-GRN", "Travel pouch · forest", "B0DEMO0005", 76, 2, 0, 0, 0, 0, 1999, 10, 800),
        ("NEW-SAMPLE", "New product · awaiting review", "B0DEMO0006", 25, 0, 0, 0, 0, 0, 2499, 8, 80),
    ]
    start = day.replace(year=day.year-1, month=1, day=1)
    store.db.execute("DELETE FROM orders")
    store.db.execute("DELETE FROM returns")
    for index, (sku, name, asin, available, reserved, unsellable, working, shipped, receiving, price, case_size, warehouse_units) in enumerate(catalog):
        store.product(sku, name=name, asin=asin, fba=True, active=True, available=available, reserved=reserved,
                      unsellable=unsellable, inbound_working=working, inbound_shipped=shipped, inbound_receiving=receiving,
                      units_per_case=case_size, warehouse_units=warehouse_units, warehouse_cases=warehouse_units // case_size,
                      warehouse_sku=sku + "-CS", inventory_at=iso(at), listing_known=True, buyable=index != 3,
                      issues=["Main image does not meet requirements"] if index == 3 else [],
                      stranded=index == 3, excess=80 if index == 2 else 0,
                      amazon_recommendation=dict(quantity=140 if index == 0 else 0, date=(day+timedelta(days=3)).date().isoformat(), days=25, updated=iso(at)))
        cursor = start
        while cursor <= day:
            qty = rng.randint(1, 8) if index < 4 else rng.randint(0, 2) if index == 4 else 0
            purchase = cursor+timedelta(hours=8)
            if qty and purchase < at:
                store.order(f"demo-{cursor.date()}-{index}", sku, iso(purchase), iso(purchase), "SHIPPED", qty, qty*price)
            if index == 1 and (day-cursor).days in (1, 2, 4, 5, 40):
                store.db.execute("INSERT OR REPLACE INTO returns VALUES(?,?,?,?,?)",
                                 (f"demo-return-{cursor.date()}", sku, iso(purchase), 1, "Defective"))
            cursor += timedelta(days=1)
    store.order("demo-pending", "TRAIL-24", iso(at-timedelta(minutes=10)), iso(at), "PENDING", 2, None)
    for idx, (sku, name, status, shipped, received) in enumerate([
        ("TRAIL-24", "September bottles", "IN_TRANSIT", 120, 0),
        ("DESK-OAK", "Organizer replenishment", "RECEIVING", 80, 48),
        ("LAMP-MINI", "Reading lamps", "ERROR", 60, 0),
    ]):
        store.put("shipment", str(idx), dict(id=str(idx), name=name, status=status,
                    eta=iso(day+timedelta(days=4)) if idx == 0 else None, updated=iso(at),
                    url="https://sellercentral.amazon.com/fba/inbound-shipment/summary", items=[dict(sku=sku, shipped=shipped, received=received, remaining=shipped-received)]))
    store.cover("orders", start, at)
    store.cover("returns", start, day)
    store.put("meta", "demo_day", day.date().isoformat())
    store.put("meta", "history", dict(start=iso(start), complete=20, windows=20))
    for source in (*INTERVALS, "sales", "warehouse"):
        store.source(source, success=iso(at), state="ready", error=None)
