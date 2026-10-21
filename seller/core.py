"""Storage and deterministic business calculations, independent of the network/UI."""
from __future__ import annotations

import calendar
import json
import math
import os
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc
MARKET = "ATVPDKIKX0DER"
DEFAULTS = dict(mode="live", period="day", metric="units", selected=None, pinned=[], lead_days=30,
                safety_days=14, interval_days=30, reviewed=False, overrides={},
                notifications=True, return_min=3, return_multiplier=2,
                source="github", github_owner="PatrikHallgren", github_repo="amazonseller-data",
                github_path="snapshot.json", github_branch="main", snapshot_file="",
                factory_lead_days=180, stale_hours=36)
INTERVALS = dict(orders=300, inventory=600, listings=900, shipments=1800, shipment_dates=1800,
                 returns=14400, restock=14400, health=14400, stranded=14400, excess=14400)


def now():
    return datetime.now(UTC)


def iso(value):
    return value.astimezone(UTC).isoformat()


def dt(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=PACIFIC) if result.tzinfo is None else result


def paths(root=None):
    if root:
        base = Path(root)
        return base / "config", base / "data"
    return (Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "amazon-seller",
            Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "amazon-seller")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_config(config_dir):
    file = config_dir / "settings.json"
    return DEFAULTS | (json.loads(file.read_text()) if file.exists() else {})


def validate_settings(changes):
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError("Unknown setting: " + key)
        if key == "mode" and value not in ("live", "demo"):
            raise ValueError("Mode must be live or demo")
        if key == "metric" and value not in ("units", "orders", "revenue"):
            raise ValueError("Invalid metric")
        if key == "period" and value not in ("day", "week", "month", "year"):
            raise ValueError("Invalid period")
        if key in ("selected", "pinned") and not (key == "selected" and value is None):
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise ValueError("Product selection must be an array of SKUs")
        if key == "source" and value not in ("github", "file", "spapi"):
            raise ValueError("Source must be github, file, or spapi")
        if key in ("github_owner", "github_repo", "github_branch", "github_path"):
            if not isinstance(value, str) or not value or len(value) > 200 or any(char.isspace() for char in value):
                raise ValueError("Invalid GitHub source setting")
            parts = value.split("/") if key == "github_path" else [value]
            if any(part in ("", ".", "..") or not all(char.isalnum() or char in "_.-" for char in part) for part in parts):
                raise ValueError("Invalid GitHub source setting")
        if key == "snapshot_file" and (not isinstance(value, str) or len(value) > 500 or "\n" in value or "\x00" in value):
            raise ValueError("Invalid snapshot file path")
        if key in ("lead_days", "safety_days", "interval_days", "return_min", "return_multiplier", "factory_lead_days", "stale_hours"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 730:
                raise ValueError("Planning values must be numbers between 0 and 730")
        if key in ("notifications", "reviewed") and not isinstance(value, bool):
            raise ValueError("Expected true or false")
        if key == "overrides":
            if not isinstance(value, dict):
                raise ValueError("Overrides must be an object")
            for settings in value.values():
                if not isinstance(settings, dict) or set(settings) - {"lead_days", "safety_days", "interval_days", "reviewed"}:
                    raise ValueError("Invalid product override")
                validate_settings(settings)


class Store:
    def __init__(self, file):
        file = Path(file)
        file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(file, timeout=15)
        os.chmod(file, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS objects(kind TEXT, id TEXT, data TEXT, PRIMARY KEY(kind,id));
          CREATE TABLE IF NOT EXISTS orders(order_id TEXT,sku TEXT,purchased TEXT,updated TEXT,
            status TEXT,quantity INTEGER,cents INTEGER, PRIMARY KEY(order_id,sku));
          CREATE INDEX IF NOT EXISTS order_dates ON orders(purchased);
          CREATE TABLE IF NOT EXISTS returns(id TEXT PRIMARY KEY,sku TEXT,occurred TEXT,quantity INTEGER,reason TEXT);
          CREATE INDEX IF NOT EXISTS return_dates ON returns(occurred);
          CREATE INDEX IF NOT EXISTS returns_sku_date ON returns(sku,occurred);
          CREATE INDEX IF NOT EXISTS orders_sku_date ON orders(sku,purchased);
          CREATE TABLE IF NOT EXISTS coverage(source TEXT,start TEXT,end TEXT,PRIMARY KEY(source,start,end));
          CREATE TABLE IF NOT EXISTS alerts(id TEXT PRIMARY KEY,data TEXT,active INTEGER,first_seen TEXT,
            acknowledged INTEGER DEFAULT 0,snoozed_until TEXT,notified INTEGER DEFAULT 0,severity TEXT);
          CREATE TABLE IF NOT EXISTS daily(
            date TEXT NOT NULL, sku TEXT NOT NULL, asin TEXT, units INTEGER NOT NULL,
            orders INTEGER, cents INTEGER, returns INTEGER, PRIMARY KEY(date, sku));
          CREATE INDEX IF NOT EXISTS daily_sku ON daily(sku, date);
          CREATE TABLE IF NOT EXISTS account_daily(
            date TEXT PRIMARY KEY, units INTEGER, orders INTEGER, cents INTEGER);
          PRAGMA user_version=1;
        """)

    def put(self, kind, key, value):
        self.db.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)", (kind, key, json.dumps(value)))

    def get(self, kind, key, default=None):
        row = self.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM objects WHERE kind=? ORDER BY id", (kind,))]

    def product(self, sku, **fields):
        old = self.get("product", sku, dict(sku=sku, name=sku, asin="", active=False, fba=False))
        self.put("product", sku, old | fields)

    def source(self, name, **fields):
        self.put("source", name, self.get("source", name, {"name": name}) | fields)

    def cover(self, source, start, end):
        self.db.execute("INSERT OR IGNORE INTO coverage VALUES(?,?,?)", (source, iso(start), iso(end)))

    def covered(self, source, start, end):
        cursor = start.astimezone(UTC)
        for row in self.db.execute("SELECT start,end FROM coverage WHERE source=? ORDER BY start", (source,)):
            a, b = dt(row[0]), dt(row[1])
            if a > cursor:
                break
            if b >= cursor:
                cursor = b
        return cursor >= end.astimezone(UTC)

    def order(self, order_id, sku, purchased, updated, status, quantity, cents):
        # Canonical order+SKU identity lets reports and newer API item IDs reconcile.
        self.db.execute("""INSERT INTO orders VALUES(?,?,?,?,?,?,?) ON CONFLICT(order_id,sku)
          DO UPDATE SET purchased=excluded.purchased,updated=excluded.updated,status=excluded.status,
          quantity=excluded.quantity,cents=excluded.cents WHERE excluded.updated>=orders.updated""",
          (order_id, sku, iso(dt(purchased)), iso(dt(updated)), status.upper().replace("CANCELED", "CANCELLED"), quantity, cents))


def period_ranges(period, at):
    end = at.astimezone(PACIFIC)
    midnight = end.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return midnight, end, midnight - timedelta(days=1), end - timedelta(days=1)
    if period == "week":
        start = midnight - timedelta(days=(midnight.weekday() + 1) % 7)
        return start, end, start - timedelta(days=7), end - timedelta(days=7)
    if period == "month":
        start = midnight.replace(day=1)
        previous = (start - timedelta(days=1)).replace(day=1)
        previous_end = end.replace(year=previous.year, month=previous.month,
                                   day=min(end.day, calendar.monthrange(previous.year, previous.month)[1]))
        return start, end, previous, previous_end
    start = midnight.replace(month=1, day=1)
    previous = start.replace(year=start.year - 1)
    previous_end = end.replace(year=end.year - 1, day=min(end.day, calendar.monthrange(end.year - 1, end.month)[1]))
    return start, end, previous, previous_end


def pacific_bounds(start, end):
    """Calendar dates covered by a Pacific range. A noon end includes that date."""
    start_day = start.astimezone(PACIFIC).date()
    end_local = end.astimezone(PACIFIC)
    end_day = end_local.date()
    if (end_local.hour, end_local.minute, end_local.second, end_local.microsecond) != (0, 0, 0, 0):
        end_day += timedelta(days=1)
    return start_day, end_day


def _account_complete(store, start_day, end_day):
    have = {row[0] for row in store.db.execute(
        "SELECT date FROM account_daily WHERE date>=? AND date<?", (start_day.isoformat(), end_day.isoformat()))}
    if not have:
        return False
    cursor = start_day
    while cursor < end_day:
        if cursor.isoformat() not in have:
            return False
        cursor += timedelta(days=1)
    return True


def summarize(store, selected, start, end):
    if store.get("meta", "sales_grain") == "daily":
        return summarize_daily(store, selected, start, end)
    orders = set()
    units = pending = cents = returned = 0
    revenue_complete = True
    for r in store.db.execute("SELECT * FROM orders WHERE purchased>=? AND purchased<?", (iso(start), iso(end))):
        if r["sku"] not in selected:
            continue
        status = r["status"].replace("_", "")
        if status in ("PENDING", "PENDINGAVAILABILITY"):
            pending += r["quantity"]
        elif status in ("UNSHIPPED", "PARTIALLYSHIPPED", "SHIPPED", "SHIPPING"):
            units += r["quantity"]
            orders.add(r["order_id"])
            if r["cents"] is None:
                revenue_complete = False
            else:
                cents += r["cents"]
    for r in store.db.execute("SELECT sku,quantity FROM returns WHERE occurred>=? AND occurred<?", (iso(start), iso(end))):
        if r["sku"] in selected:
            returned += r["quantity"]
    return dict(units=units, orders=len(orders), pending=pending, revenue=cents / 100,
                revenue_complete=revenue_complete, returns=returned,
                sales_complete=store.covered("orders", start, end),
                returns_complete=store.covered("returns", start, end), scope="sku")


def _touches_provisional(store, start_day, end_day):
    for day in store.get("meta", "provisional_days", []) or []:
        try:
            current = datetime.fromisoformat(day).date()
        except (TypeError, ValueError):
            continue
        if start_day <= current < end_day:
            return True
    return False


def summarize_daily(store, selected, start, end):
    """Per-SKU daily rows from a snapshot. Orders are Total Order Items, not distinct checkouts."""
    start_day, end_day = pacific_bounds(start, end)
    has_sku = store.db.execute("SELECT 1 FROM daily LIMIT 1").fetchone()
    units = orders = cents = 0
    revenue_complete = orders_known = True
    if has_sku:
        for row in store.db.execute(
                "SELECT sku, units, orders, cents FROM daily WHERE date>=? AND date<?",
                (start_day.isoformat(), end_day.isoformat())):
            if row["sku"] not in selected:
                continue
            units += row["units"] or 0
            if row["orders"] is None:
                orders_known = False
            else:
                orders += row["orders"]
            if row["cents"] is None:
                revenue_complete = False
            else:
                cents += row["cents"]
        scope = "sku"
        sales_complete = store.covered("sales", start, end)
    else:
        rows = store.db.execute(
            "SELECT units, orders, cents FROM account_daily WHERE date>=? AND date<?",
            (start_day.isoformat(), end_day.isoformat())).fetchall()
        if not rows:
            scope, sales_complete, orders_known = "sku", False, False
        else:
            scope = "account"
            sales_complete = _account_complete(store, start_day, end_day)
            for row in rows:
                units += row["units"] or 0
                if row["orders"] is None:
                    orders_known = False
                else:
                    orders += row["orders"]
                if row["cents"] is None:
                    revenue_complete = False
                else:
                    cents += row["cents"]
    returned = 0
    for row in store.db.execute("SELECT sku, quantity FROM returns WHERE occurred>=? AND occurred<?", (iso(start), iso(end))):
        if not has_sku or row["sku"] in selected:
            returned += row["quantity"]
    # The newest By Date day stays provisional until a later snapshot replaces it.
    if _touches_provisional(store, start_day, end_day):
        sales_complete = False
    return dict(units=units, orders=orders if orders_known else None, pending=0, revenue=cents / 100,
                revenue_complete=revenue_complete, returns=returned, sales_complete=sales_complete,
                returns_complete=store.covered("returns", start, end), scope=scope)


def case_plan(suggested, units_per_case, warehouse_units):
    """Nearest whole case, at least one when any units are needed, then capped by 3PL cases."""
    if suggested is None:
        return None, None
    suggested = int(suggested)
    if not units_per_case or int(units_per_case) <= 0:
        if warehouse_units is None:
            return suggested, None
        return max(0, min(suggested, int(warehouse_units))), None
    case_size = int(units_per_case)
    if suggested <= 0:
        cases = 0
    else:
        cases = max(1, (suggested + case_size // 2) // case_size)
    if warehouse_units is not None:
        cases = min(cases, max(0, int(warehouse_units) // case_size))
    return cases * case_size, cases


def inventory_estimate(product, units, days, config):
    settings = config | config.get("overrides", {}).get(product["sku"], {})
    daily = units / days if days else None
    available = product.get("available")
    coverage = available / daily if available is not None and daily else None
    target = math.ceil(daily * (settings["lead_days"] + settings["safety_days"] + settings["interval_days"])) if daily else None
    # FC transfer is already inside Amazon's network. Inbound working is still at the 3PL until Amazon marks it shipped.
    fc_transfer = product.get("fc_transfer") or 0
    inbound = (product.get("inbound_shipped") or 0) + (product.get("inbound_receiving") or 0)
    suggestion = max(0, math.ceil(target - available - inbound - fc_transfer)) if target is not None and available is not None else None
    warehouse_units = product.get("warehouse_units")
    units_per_case = product.get("units_per_case")
    # A product missing from the 3PL file still has an FBA-side US total. Unknown 3PL is not a zero for the factory alert.
    total_us = None if available is None else available + inbound + fc_transfer + (0 if warehouse_units is None else warehouse_units)
    warehouse_cases = product.get("warehouse_cases")
    if warehouse_cases is None and warehouse_units is not None and units_per_case:
        warehouse_cases = int(warehouse_units) // int(units_per_case)
    suggested_units, suggested_cases = case_plan(suggestion, units_per_case, warehouse_units)
    factory_days = settings.get("factory_lead_days", 180)
    factory_cover = round(total_us / daily, 1) if total_us is not None and daily else None
    weeks = round(total_us / (daily * 7), 1) if total_us is not None and daily else None
    if suggestion is None:
        restock_note = "Replenishment needs sellable quantity and recent sales"
    elif suggested_cases is not None and suggestion > 0 and suggested_cases == 0:
        restock_note = "No full cases left at Smart Warehousing. Reorder from Shenzhen."
    elif suggested_cases is not None:
        restock_note = (f"Send {suggested_cases} cases ({suggested_units} units) from Sumner. "
                        f"Estimate was {suggestion} units, rounded to {int(units_per_case)}-unit cases and capped by 3PL stock.")
    else:
        restock_note = f"Suggested additional {suggestion} units."
    if warehouse_units is None:
        factory_note = "Factory reorder needs the Smart Warehousing snapshot"
    elif factory_cover is None:
        factory_note = "Factory cover needs recent sales"
    elif factory_cover < factory_days:
        factory_note = f"Reorder from Shenzhen · {factory_cover / 30:.1f} months of US stock · lead time {int(factory_days)} days"
    else:
        factory_note = f"Shenzhen lead covered · {factory_cover / 30:.1f} months of US stock · lead time {int(factory_days)} days"
    return dict(daily_demand=round(daily, 3) if daily is not None else None,
                days_left=round(coverage, 1) if coverage is not None else None,
                target=target, suggested=suggestion, suggested_units=suggested_units, suggested_cases=suggested_cases,
                history_days=days, total_us=total_us, weeks_cover=weeks, warehouse_cases=warehouse_cases,
                factory_cover_days=factory_cover, restock_note=restock_note, factory_note=factory_note,
                reorder_threshold=math.ceil(daily * (settings["lead_days"] + settings["safety_days"])) if daily else None,
                assumptions=dict(lead_days=settings["lead_days"], safety_days=settings["safety_days"],
                                 interval_days=settings["interval_days"], reviewed=settings["reviewed"],
                                 factory_lead_days=factory_days),
                forecast_note="Insufficient history" if not days else "No recent sales" if not units else
                "Limited history" if days < 30 else "Recent stockout may understate demand" if product.get("stockout_at") else "30-day sales estimate")


def stock_summary(products):
    chosen = [product for product in products if product.get("selected")]
    def sum_known(key):
        values = [product.get(key) for product in chosen if product.get(key) is not None]
        if not values:
            return None
        return sum(values)
    total_us, fba, warehouse = sum_known("total_us"), sum_known("available"), sum_known("warehouse_units")
    partial = bool(chosen) and any(product.get("warehouse_units") is None for product in chosen)
    cases = None if warehouse is None else sum(product.get("warehouse_cases") or 0 for product in chosen if product.get("warehouse_units") is not None)
    demand = sum(product.get("daily_demand") or 0 for product in chosen if product.get("total_us") is not None)
    weeks = round(total_us / (demand * 7), 1) if total_us is not None and demand else None
    return dict(fba_available=fba, warehouse_units=warehouse, warehouse_cases=cases, total_us=total_us,
                weeks_cover=weeks, warehouse_partial=partial)


def product_link(sku):
    from urllib.parse import quote
    return "https://sellercentral.amazon.com/myinventory/inventory?searchTerm=" + quote(sku, safe="")


def snapshot(store, config, at=None):
    at = at or now()
    products = store.all("product")
    selected = set(config["selected"] if config["selected"] is not None else [p["sku"] for p in products if p.get("active") and p.get("fba")])
    day_end = at.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    coverage = "sales" if store.get("meta", "sales_grain") == "daily" else "orders"
    # Use only contiguous, completely fetched days; do not turn gaps into zero demand.
    days = 0
    for n in range(1, 31):
        if not store.covered(coverage, day_end - timedelta(days=n), day_end):
            break
        days = n
    daily_units = defaultdict(lambda: defaultdict(int))
    if coverage == "sales":
        window = ((day_end - timedelta(days=30)).date().isoformat(), day_end.date().isoformat())
        for row in store.db.execute("SELECT sku, date, units FROM daily WHERE date>=? AND date<?", window):
            daily_units[row["sku"]][datetime.fromisoformat(row["date"]).date()] += row["units"] or 0
    else:
        for r in store.db.execute("SELECT sku,purchased,status,quantity FROM orders WHERE purchased>=? AND purchased<?", (iso(day_end-timedelta(days=30)), iso(day_end))):
            if r["status"].replace("_", "") in ("SHIPPED", "UNSHIPPED", "PARTIALLYSHIPPED", "SHIPPING"):
                daily_units[r["sku"]][dt(r["purchased"]).astimezone(PACIFIC).date()] += r["quantity"]
    reasons = defaultdict(list)
    for r in store.db.execute("SELECT sku,reason,sum(quantity) FROM returns WHERE occurred>=? GROUP BY sku,reason ORDER BY sum(quantity) DESC", (iso(day_end-timedelta(days=30)),)):
        reasons[r[0]].append(dict(reason=r[1], units=r[2]))
    provisional_days = set(store.get("meta", "provisional_days", []) or [])
    for p in products:
        sku = p["sku"]
        p["selected"] = sku in selected
        p["pinned"] = sku in config["pinned"]
        p["url"] = product_link(sku)
        units = sum(daily_units[sku][(day_end-timedelta(days=i)).date()] for i in range(1, days+1))
        p.update(inventory_estimate(p, units, days, config))
        p["trend"] = [daily_units[sku][(day_end-timedelta(days=i)).date()] for i in range(14, 0, -1)]
        p["trend_days"] = [
            dict(date=(day_end-timedelta(days=i)).date().isoformat(),
                 units=p["trend"][14-i],
                 complete=store.covered(coverage, day_end-timedelta(days=i), day_end-timedelta(days=i-1))
                 and (day_end-timedelta(days=i)).date().isoformat() not in provisional_days)
            for i in range(14, 0, -1)]
        p["trend_complete"] = store.covered(coverage, day_end - timedelta(days=14), day_end)
        p["return_reasons"] = reasons[sku]
    products.sort(key=lambda p: (not p["pinned"], p.get("days_left") is None, p.get("days_left") or 0, p["name"]))
    periods = {}
    for period in ("day", "week", "month", "year"):
        a, b, c, d = period_ranges(period, at)
        current, previous = summarize(store, selected, a, b), summarize(store, selected, c, d)
        periods[period] = dict(current=current, previous=previous,
                               dates=[x.isoformat() for x in (a, b, c, d)],
                               delta=current["units"]-previous["units"],
                               percentage=round(100*(current["units"]-previous["units"])/previous["units"], 1) if previous["units"] else None)
    sources = {s["name"]: s for s in store.all("source")}
    snapshot_mode = store.get("meta", "data_source") == "snapshot"
    limit = float(config.get("stale_hours") or 36) * 3600
    for name, source in sources.items():
        if snapshot_mode:
            success = source.get("success")
            try:
                age = (at - dt(success)).total_seconds() if success else None
            except (TypeError, ValueError):
                age = None
            # Age is the report's as_of, not the last poll. A failed poll keeps this timestamp.
            source["stale"] = age is None or age > limit
        else:
            source["stale"] = bool(source.get("error")) or not source.get("success") or (at-dt(source["success"])).total_seconds() > INTERVALS.get(name, 14400)*2
    if snapshot_mode:
        generated_at = store.get("meta", "snapshot_generated_at")
        try:
            generated_stale = not generated_at or (at - dt(generated_at)).total_seconds() > limit
        except (TypeError, ValueError):
            generated_stale = True
        stale = generated_stale or any(sources.get(name, {}).get("stale", True) for name in ("sales", "inventory"))
        if (sources.get("warehouse") or {}).get("success"):
            stale = stale or bool(sources["warehouse"].get("stale"))
    else:
        generated_at = iso(at)
        stale = any(sources.get(name, {}).get("stale", True) for name in ("orders", "inventory"))
    snapshot_error = (sources.get("snapshot") or {}).get("error")
    alerts = []
    for r in store.db.execute("SELECT * FROM alerts WHERE active=1 ORDER BY first_seen DESC"):
        alert = json.loads(r["data"])
        if alert.get("sku") and alert["sku"] not in selected:
            continue
        alerts.append(alert | dict(id=r["id"], first_seen=r["first_seen"], acknowledged=bool(r["acknowledged"]), snoozed_until=r["snoozed_until"]))
    alerts.sort(key=lambda a: (a["severity"] != "urgent", a["acknowledged"]))
    shipments = []
    for shipment in store.all("shipment"):
        items = shipment.get("items") or []
        if items:
            matched = [item for item in items if item.get("sku") in selected]
            if not matched:
                continue
            shipment = dict(shipment, items=matched)
        shipments.append(shipment)
    return dict(version=1, generated_at=iso(at), mode=config["mode"], settings=config,
                products=products, selected_count=len(selected), periods=periods, alerts=alerts,
                shipments=shipments, sources=sources, stale=stale, stock=stock_summary(products),
                data_generated_at=generated_at, data_fetched_at=(sources.get("snapshot") or {}).get("fetched_at"),
                source_error=snapshot_error, history=store.get("meta", "history", {}),
                customer_messages="Not monitored — open Seller Central inbox",
                setup_required=not bool(store.get("meta", "verified")))


def _has_sales_history(store, sku):
    if store.db.execute("SELECT 1 FROM daily WHERE sku=? AND units>0 LIMIT 1", (sku,)).fetchone():
        return True
    row = store.db.execute(
        "SELECT 1 FROM orders WHERE sku=? AND quantity>0 AND replace(status, '_', '')!='CANCELLED' LIMIT 1",
        (sku,)).fetchone()
    return row is not None


def detect_alerts(store, view, at=None):
    at = at or now()
    found = {}
    def add(key, title, detail, sku="", severity="warning", url=None):
        found[key] = dict(id=key, title=title, detail=detail, sku=sku, severity=severity,
                          url=url or product_link(sku))
    for p in view["products"]:
        sku = p["sku"]
        # A new listing with no sales is not a stockout. Zero everywhere and no history stays quiet.
        if _has_sales_history(store, sku) and (p.get("available") == 0 or (p["days_left"] is not None and p["days_left"] <= 7)):
            add("stock:"+sku, "Stockout risk · " + p["name"], f"{p.get('available')} sellable · {p['days_left'] if p['days_left'] is not None else 'unknown'} days left", sku, "urgent")
        elif _has_sales_history(store, sku) and p["days_left"] is not None and p["days_left"] < p["assumptions"]["lead_days"] + p["assumptions"]["safety_days"]:
            if p.get("suggested_cases") is not None:
                detail = f"{p['days_left']} days left · send {p['suggested_cases']} cases ({p['suggested_units']} units) from the 3PL"
            else:
                detail = f"{p['days_left']} days left · estimate {p['suggested']} additional units"
            add("stock:"+sku, "Replenish soon · " + p["name"], detail, sku)
        lead = p.get("assumptions", {}).get("factory_lead_days") or 180
        cover_days = p.get("factory_cover_days")
        if p.get("warehouse_units") is not None and cover_days is not None and cover_days < lead:
            severity = "urgent" if cover_days < lead / 2 else "warning"
            add("factory:"+sku, "Reorder from factory · " + p["name"],
                f"{p.get('total_us')} units in the US, about {round(cover_days / 30, 1)} months of sales. Shenzhen lead time is {int(lead)} days.",
                sku, severity)
        if p.get("stranded") or (p.get("listing_known") and not p.get("buyable")) or p.get("issues"):
            add("listing:"+sku, "Listing needs attention · " + p["name"], " · ".join(p.get("issues", [])) or "Listing is stranded or not buyable", sku)
        day = at.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
        if store.covered("returns", day-timedelta(days=14), day):
            a = store.db.execute("SELECT coalesce(sum(quantity),0) FROM returns WHERE sku=? AND occurred>=? AND occurred<?", (sku,iso(day-timedelta(days=7)),iso(day))).fetchone()[0]
            b = store.db.execute("SELECT coalesce(sum(quantity),0) FROM returns WHERE sku=? AND occurred>=? AND occurred<?", (sku,iso(day-timedelta(days=14)),iso(day-timedelta(days=7)))).fetchone()[0]
            if a >= view["settings"]["return_min"] and a > b*view["settings"]["return_multiplier"]:
                add("returns:"+sku, "Returns increased · " + p["name"], f"{a} received units in 7 days; previous 7 days: {b}", sku)
    for s in store.all("shipment"):
        for item in s.get("items", []):
            issue = s["status"] in ("ERROR", "CANCELLED") or s.get("exception")
            remaining = item.get("remaining")
            mismatch = s["status"] == "CLOSED" and remaining is not None and remaining > 0
            late = bool(s.get("eta") and dt(s["eta"]) < at and s["status"] in ("WORKING", "READY_TO_SHIP", "SHIPPED", "IN_TRANSIT") and remaining)
            if issue or mismatch or late:
                add("shipment:"+s["id"]+":"+item["sku"], "Shipment needs attention", s["name"]+" · "+("Receiving discrepancy" if mismatch else "Past reported arrival window" if late else s["status"]), item["sku"], url=s["url"])
    for issue in store.get("meta", "health_issues", []):
        add("account:"+issue["key"], "Account health · " + issue["title"], issue["detail"], severity="urgent", url="https://sellercentral.amazon.com/performance/dashboard")
    old = {r["id"]: r for r in store.db.execute("SELECT * FROM alerts")}
    for key, a in found.items():
        prev = old.get(key)
        reset = not prev or not prev["active"] or (prev["severity"] != "urgent" and a["severity"] == "urgent")
        if not prev:
            store.db.execute("INSERT INTO alerts(id,data,active,first_seen,severity) VALUES(?,?,1,?,?)", (key, json.dumps(a), iso(at), a["severity"]))
        else:
            store.db.execute("UPDATE alerts SET data=?,active=1,severity=?,notified=?,acknowledged=?,snoozed_until=?,first_seen=? WHERE id=?",
                             (json.dumps(a), a["severity"], 0 if reset else prev["notified"], 0 if reset else prev["acknowledged"], None if reset else prev["snoozed_until"], iso(at) if reset else prev["first_seen"], key))
    for key in old.keys()-found.keys():
        store.db.execute("UPDATE alerts SET active=0 WHERE id=?", (key,))
