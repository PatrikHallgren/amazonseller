"""Load a validated snapshot into the local store. The file replaces snapshot-owned rows."""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .core import PACIFIC, iso, now
from .schema import validate_snapshot


def _day(value):
    return date.fromisoformat(value)


def _cover(store, source, start_day, end_day):
    start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=PACIFIC)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=PACIFIC) + timedelta(days=1)
    store.cover(source, start, end)


def _contiguous(days):
    ordered = sorted(set(days))
    return all((b - a).days == 1 for a, b in zip(ordered, ordered[1:]))


def _cents(amount):
    if amount is None:
        return None
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _apply(store, data, at):
    store.put("meta", "sales_grain", "daily")
    store.put("meta", "data_source", "snapshot")
    store.put("meta", "verified", {"at": data["generated_at"], "source": "snapshot"})
    store.put("meta", "snapshot_generated_at", data["generated_at"])
    store.put("meta", "health_issues", [])

    if "daily" in data:
        store.db.execute("DELETE FROM daily")
        store.db.execute("DELETE FROM coverage WHERE source='sales'")
        for row in data["daily"]:
            store.db.execute(
                "INSERT OR REPLACE INTO daily(date,sku,asin,units,orders,cents,returns) VALUES(?,?,?,?,?,?,?)",
                (row["date"], row["sku"], row.get("asin") or "", int(row["units"]),
                 None if row.get("orders") is None else int(row["orders"]),
                 _cents(row.get("sales")),
                 None if row.get("returns") is None else int(row["returns"])))
        days = [_day(row["date"]) for row in data["daily"]]
        sales = data.get("sources", {}).get("sales", {})
        if days and sales.get("complete", True) and _contiguous(days):
            _cover(store, "sales", min(days), max(days))
        else:
            for day in sorted(set(days)):
                _cover(store, "sales", day, day)
    if "account_daily" in data:
        store.db.execute("DELETE FROM account_daily")
        for row in data["account_daily"]:
            store.db.execute(
                "INSERT OR REPLACE INTO account_daily(date,units,orders,cents) VALUES(?,?,?,?)",
                (row["date"], None if row.get("units") is None else int(row["units"]),
                 None if row.get("orders") is None else int(row["orders"]), _cents(row.get("sales"))))

    catalog = {row["sku"]: row for row in data.get("catalog", [])}
    inventory = {row["sku"]: row for row in data.get("inventory", [])}
    houses = {}
    for row in data.get("warehouse", []):
        sku = row.get("product_sku")
        if not sku:
            continue
        current = houses.setdefault(sku, {"cases": 0, "units": None, "skus": [], "units_per_case": None})
        if row.get("cases_on_hand") is not None:
            current["cases"] += int(row["cases_on_hand"])
        if row.get("units_on_hand") is not None:
            current["units"] = (current["units"] or 0) + int(row["units_on_hand"])
        if row.get("units_per_case"):
            current["units_per_case"] = int(row["units_per_case"])
        current["skus"].append(row["sku"])
    daily_skus = {row["sku"] for row in data.get("daily", [])}
    inventory_known = bool(inventory) or bool(data.get("sources", {}).get("inventory", {}).get("available"))
    skus = set(catalog) | set(inventory) | set(houses) | daily_skus
    inventory_as_of = data.get("sources", {}).get("inventory", {}).get("as_of")
    for sku in sorted(skus):
        info = catalog.get(sku, {})
        stock = inventory.get(sku, {})
        house = houses.get(sku)
        present = sku in inventory
        restock = stock.get("restock") if present else None
        recommendation = None
        if isinstance(restock, dict) and restock.get("quantity") is not None:
            recommendation = {
                "quantity": int(restock["quantity"]),
                "date": restock.get("ship_date") or "",
                "days": stock.get("days_of_supply"),
                "updated": data["generated_at"],
            }
        if inventory_known:
            active = present or house is not None
        else:
            active = True
        fields = dict(
            name=info.get("name") or stock.get("name") or sku,
            asin=stock.get("asin") or info.get("asin") or "",
            fba=True,
            active=active,
            available=stock.get("available") if present else None,
            reserved=stock.get("reserved") if present else None,
            unsellable=stock.get("unfulfillable") if present else None,
            inbound_working=stock.get("inbound_working") if present else None,
            inbound_shipped=stock.get("inbound_shipped") if present else None,
            inbound_receiving=stock.get("inbound_receiving") if present else None,
            fc_transfer=stock.get("fc_transfer") if present else None,
            inventory_at=inventory_as_of if present else None,
            units_per_case=int(house["units_per_case"]) if house and house.get("units_per_case") else info.get("units_per_case"),
            warehouse_sku=", ".join(house["skus"]) if house else info.get("warehouse_sku"),
            warehouse_cases=house["cases"] if house else None,
            warehouse_units=house["units"] if house else None,
            amazon_days_of_supply=stock.get("days_of_supply") if present else None,
            amazon_recommendation=recommendation,
            listing_known=True,
            buyable=True,
            stranded=False,
            issues=[],
            excess=0,
            stockout_at=data["generated_at"] if present and stock.get("available") == 0 else None,
        )
        store.product(sku, **fields)
    for old in store.all("product"):
        if old["sku"] not in skus:
            store.product(old["sku"], active=False)

    if "shipments" in data:
        store.db.execute("DELETE FROM objects WHERE kind='shipment'")
        for row in data["shipments"]:
            items = []
            for item in row.get("items", []):
                shipped, received = item.get("shipped"), item.get("received")
                remaining = None
                if shipped is not None and received is not None:
                    remaining = max(0, int(shipped) - int(received))
                items.append({"sku": item["sku"], "shipped": shipped, "received": received, "remaining": remaining})
            shipment_id = row["id"]
            store.put("shipment", shipment_id, {
                "id": shipment_id,
                "name": row.get("name") or shipment_id,
                "status": row.get("status") or "UNKNOWN",
                "eta": row.get("eta"),
                "updated": row.get("updated") or data["generated_at"],
                "url": row.get("url") or "https://sellercentral.amazon.com/fba/inbound-shipment/summary/" + shipment_id,
                "items": items,
                "units_expected": row.get("units_expected"),
                "units_located": row.get("units_located"),
                "destination": row.get("destination") or "",
                "delivery_window": row.get("delivery_window") or "",
            })

    if "returns" in data:
        store.db.execute("DELETE FROM returns")
        store.db.execute("DELETE FROM coverage WHERE source='returns'")
        for index, row in enumerate(data["returns"]):
            identity = "|".join([row["date"], row["sku"], row.get("reason") or "", str(row["units"]), str(index)])
            key = hashlib.sha256(identity.encode()).hexdigest()
            moment = datetime(_day(row["date"]).year, _day(row["date"]).month, _day(row["date"]).day, 12, tzinfo=PACIFIC)
            store.db.execute(
                "INSERT OR REPLACE INTO returns VALUES(?,?,?,?,?)",
                (key, row["sku"], iso(moment), int(row["units"]), row.get("reason") or "Unspecified"))
        days = [_day(row["date"]) for row in data["returns"]]
        if days:
            _cover(store, "returns", min(days), max(days))

    if "daily" in data or "account_daily" in data:
        provisional = sorted({row["date"] for row in data.get("daily", []) if row.get("provisional")}
                             | {row["date"] for row in data.get("account_daily", []) if row.get("provisional")})
        store.put("meta", "provisional_days", provisional)
    dates = sorted({row[0] for row in store.db.execute("SELECT date FROM daily")}
                   | {row[0] for row in store.db.execute("SELECT date FROM account_daily")})
    store.put("meta", "history", {
        "start": dates[0] if dates else data.get("sources", {}).get("sales", {}).get("start"),
        "end": dates[-1] if dates else data.get("sources", {}).get("sales", {}).get("end"),
        "days": len(dates),
        "windows": len(dates),
        "complete": len(dates),
    })
    for name in ("sales", "inventory", "shipments", "warehouse", "returns"):
        source = data.get("sources", {}).get(name, {})
        report = source.get("report")
        if source.get("available") is False or not source.get("as_of"):
            store.source(name, state="unavailable", error=None, success=None, report=report, fetched_at=iso(at))
        else:
            store.source(name, state="ready", error=None, success=source["as_of"], report=report, fetched_at=iso(at))
    # The panel's existing sales age reads `orders` when a snapshot source is not present yet.
    sales = store.get("source", "sales", {})
    if sales.get("success"):
        store.source("orders", state=sales.get("state"), error=None, success=sales.get("success"), fetched_at=iso(at))


def ingest_snapshot(store, data, at=None):
    at = at or now()
    validate_snapshot(data)
    store.db.execute("BEGIN IMMEDIATE")
    try:
        _apply(store, data, at)
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    return data
