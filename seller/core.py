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
                notifications=True, return_min=3, return_multiplier=2)
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
        if key in ("lead_days", "safety_days", "interval_days", "return_min", "return_multiplier"):
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


def summarize(store, selected, start, end):
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
                returns_complete=store.covered("returns", start, end))


def inventory_estimate(product, units, days, config):
    settings = config | config.get("overrides", {}).get(product["sku"], {})
    daily = units / days if days else None
    available = product.get("available")
    coverage = available / daily if available is not None and daily else None
    target = math.ceil(daily * (settings["lead_days"] + settings["safety_days"] + settings["interval_days"])) if daily else None
    inbound = product.get("inbound_shipped", 0) + product.get("inbound_receiving", 0)
    suggestion = max(0, math.ceil(target - available - inbound)) if target is not None and available is not None else None
    return dict(daily_demand=round(daily, 3) if daily is not None else None,
                days_left=round(coverage, 1) if coverage is not None else None,
                target=target, suggested=suggestion, history_days=days,
                reorder_threshold=math.ceil(daily * (settings["lead_days"] + settings["safety_days"])) if daily else None,
                assumptions=dict(lead_days=settings["lead_days"], safety_days=settings["safety_days"],
                                 interval_days=settings["interval_days"], reviewed=settings["reviewed"]),
                forecast_note="Insufficient history" if not days else "No recent sales" if not units else
                "Limited history" if days < 30 else "Recent stockout may understate demand" if product.get("stockout_at") else "30-day sales estimate")


def product_link(sku):
    from urllib.parse import quote
    return "https://sellercentral.amazon.com/myinventory/inventory?searchTerm=" + quote(sku, safe="")


def snapshot(store, config, at=None):
    at = at or now()
    products = store.all("product")
    selected = set(config["selected"] if config["selected"] is not None else [p["sku"] for p in products if p.get("active") and p.get("fba")])
    day_end = at.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
    # Use only contiguous, completely fetched days; do not turn gaps into zero demand.
    days = 0
    for n in range(1, 31):
        if not store.covered("orders", day_end - timedelta(days=n), day_end):
            break
        days = n
    daily_units = defaultdict(lambda: defaultdict(int))
    for r in store.db.execute("SELECT sku,purchased,status,quantity FROM orders WHERE purchased>=? AND purchased<?", (iso(day_end-timedelta(days=30)), iso(day_end))):
        if r["status"].replace("_", "") in ("SHIPPED", "UNSHIPPED", "PARTIALLYSHIPPED", "SHIPPING"):
            daily_units[r["sku"]][dt(r["purchased"]).astimezone(PACIFIC).date()] += r["quantity"]
    reasons = defaultdict(list)
    for r in store.db.execute("SELECT sku,reason,sum(quantity) FROM returns WHERE occurred>=? GROUP BY sku,reason ORDER BY sum(quantity) DESC", (iso(day_end-timedelta(days=30)),)):
        reasons[r[0]].append(dict(reason=r[1], units=r[2]))
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
                 complete=store.covered("orders", day_end-timedelta(days=i), day_end-timedelta(days=i-1)))
            for i in range(14, 0, -1)]
        p["trend_complete"] = store.covered("orders", day_end - timedelta(days=14), day_end)
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
    for name, s in sources.items():
        s["stale"] = bool(s.get("error")) or not s.get("success") or (at-dt(s["success"])).total_seconds() > INTERVALS.get(name, 14400)*2
    alerts = []
    for r in store.db.execute("SELECT * FROM alerts WHERE active=1 ORDER BY first_seen DESC"):
        alert = json.loads(r["data"])
        if alert.get("sku") and alert["sku"] not in selected:
            continue
        alerts.append(alert | dict(id=r["id"], first_seen=r["first_seen"], acknowledged=bool(r["acknowledged"]), snoozed_until=r["snoozed_until"]))
    alerts.sort(key=lambda a: (a["severity"] != "urgent", a["acknowledged"]))
    shipments = [s for s in store.all("shipment") if any(i["sku"] in selected for i in s.get("items", []))]
    for s in shipments:
        s["items"] = [i for i in s["items"] if i["sku"] in selected]
    return dict(version=1, generated_at=iso(at), mode=config["mode"], settings=config,
                products=products, selected_count=len(selected), periods=periods, alerts=alerts,
                shipments=shipments, sources=sources,
                stale=any(sources.get(x, {}).get("stale", True) for x in ("orders", "inventory")),
                history=store.get("meta", "history", {}),
                customer_messages="Not monitored — open Seller Central inbox",
                setup_required=not bool(store.get("meta", "verified")))


def detect_alerts(store, view, at=None):
    at = at or now()
    found = {}
    def add(key, title, detail, sku="", severity="warning", url=None):
        found[key] = dict(id=key, title=title, detail=detail, sku=sku, severity=severity,
                          url=url or product_link(sku))
    for p in view["products"]:
        sku = p["sku"]
        if p.get("available") == 0 or (p["days_left"] is not None and p["days_left"] <= 7):
            add("stock:"+sku, "Stockout risk · " + p["name"], f"{p.get('available')} sellable · {p['days_left'] if p['days_left'] is not None else 'unknown'} days left", sku, "urgent")
        elif p["days_left"] is not None and p["days_left"] < p["assumptions"]["lead_days"] + p["assumptions"]["safety_days"]:
            add("stock:"+sku, "Replenish soon · " + p["name"], f"{p['days_left']} days left · estimate {p['suggested']} additional units", sku)
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
