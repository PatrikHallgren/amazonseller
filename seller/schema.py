"""Versioned daily snapshot contract. See SCHEMA.md.

An assistant publishes this document. The panel only reads it.
"""
from __future__ import annotations

import math
import re
from datetime import datetime

SCHEMA_VERSION = 1
STALE_HOURS = 36
SNAPSHOT_POLL_SECONDS = 3600
SNAPSHOT_RETRY_SECONDS = 300
MAX_SNAPSHOT_BYTES = 20_000_000

DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")
SELLER = {
    "name": "PALE Trading Co.",
    "marketplace": "US",
    "currency": "USD",
    "timezone": "America/Los_Angeles",
    "warehouse": "Smart Warehousing, Sumner, WA",
    "factory": "Shenzhen",
}


class SnapshotError(ValueError):
    """The snapshot or a source export cannot be used. Cached data stays."""


def parse_timestamp(value, path):
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(path + " must be an ISO-8601 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise SnapshotError(path + " must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise SnapshotError(path + " must include a timezone")
    return parsed


def _number(value, path, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, float) and not math.isfinite(value):
        raise SnapshotError(path + " must be a number")
    if integer and not float(value).is_integer():
        raise SnapshotError(path + " must be an integer")
    return int(value) if integer else value


def _text(value, path, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise SnapshotError(path + " must be a string" + (" and not empty" if required else ""))
    return value.strip() if required else value


def _rows(data, key, check, required=False):
    if key not in data:
        if required:
            raise SnapshotError(key + " is required")
        return
    rows = data[key]
    if not isinstance(rows, list):
        raise SnapshotError(key + " must be an array")
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SnapshotError(f"{key}[{index}] must be an object")
        check(row, f"{key}[{index}]")


def _date(value, path):
    text = _text(value, path, required=True)
    if not DATE.fullmatch(text):
        raise SnapshotError(path + " must be YYYY-MM-DD")
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise SnapshotError(path + " must be a real calendar date") from None
    return text


def _daily(row, path):
    _date(row.get("date"), path + ".date")
    _text(row.get("sku"), path + ".sku", required=True)
    if "units" not in row:
        raise SnapshotError(path + ".units is required")
    _number(row["units"], path + ".units", integer=True)
    for key in ("orders", "returns"):
        if row.get(key) is not None:
            _number(row[key], path + "." + key, integer=True)
    if row.get("sales") is not None:
        _number(row["sales"], path + ".sales")
    _text(row.get("asin"), path + ".asin")
    if "provisional" in row and not isinstance(row.get("provisional"), bool):
        raise SnapshotError(path + ".provisional must be true or false")


def _account(row, path):
    _date(row.get("date"), path + ".date")
    for key in ("units", "orders"):
        if row.get(key) is not None:
            _number(row[key], path + "." + key, integer=True)
    if row.get("sales") is not None:
        _number(row["sales"], path + ".sales")
    if "provisional" in row and not isinstance(row.get("provisional"), bool):
        raise SnapshotError(path + ".provisional must be true or false")


def _inventory(row, path):
    _text(row.get("sku"), path + ".sku", required=True)
    for key in ("available", "reserved", "unfulfillable", "inbound_working", "inbound_shipped",
                "inbound_receiving", "fc_transfer", "days_of_supply"):
        if row.get(key) is not None:
            _number(row[key], path + "." + key, integer=True)
    restock = row.get("restock")
    if restock is not None:
        if not isinstance(restock, dict):
            raise SnapshotError(path + ".restock must be an object")
        if restock.get("quantity") is not None:
            _number(restock["quantity"], path + ".restock.quantity", integer=True)
        _text(restock.get("ship_date"), path + ".restock.ship_date")


def _shipment(row, path):
    _text(row.get("id"), path + ".id", required=True)
    _text(row.get("status"), path + ".status")
    _text(row.get("name"), path + ".name")
    items = row.get("items", [])
    if not isinstance(items, list):
        raise SnapshotError(path + ".items must be an array")
    for index, item in enumerate(items):
        where = f"{path}.items[{index}]"
        if not isinstance(item, dict):
            raise SnapshotError(where + " must be an object")
        _text(item.get("sku"), where + ".sku", required=True)
        for key in ("shipped", "received"):
            if item.get(key) is not None:
                _number(item[key], where + "." + key, integer=True)
    for key in ("units_expected", "units_located"):
        if row.get(key) is not None:
            _number(row[key], path + "." + key, integer=True)
    _text(row.get("destination"), path + ".destination")
    _text(row.get("delivery_window"), path + ".delivery_window")


def _warehouse(row, path):
    _text(row.get("sku"), path + ".sku", required=True)
    for key in ("cases_on_hand", "units_per_case", "units_on_hand", "cases_per_pallet"):
        if row.get(key) is not None:
            _number(row[key], path + "." + key, integer=True)
    _text(row.get("product_sku"), path + ".product_sku")
    _text(row.get("description"), path + ".description")


def _catalog(row, path):
    _text(row.get("sku"), path + ".sku", required=True)
    _text(row.get("name"), path + ".name")
    _text(row.get("asin"), path + ".asin")
    _text(row.get("warehouse_sku"), path + ".warehouse_sku")
    if row.get("units_per_case") is not None:
        _number(row["units_per_case"], path + ".units_per_case", integer=True)


def _return(row, path):
    _date(row.get("date"), path + ".date")
    _text(row.get("sku"), path + ".sku", required=True)
    if "units" not in row or row["units"] is None:
        raise SnapshotError(path + ".units is required")
    _number(row["units"], path + ".units", integer=True)
    _text(row.get("reason"), path + ".reason")


def validate_snapshot(data):
    """Raise SnapshotError when the document cannot refresh the panel."""
    if not isinstance(data, dict):
        raise SnapshotError("Snapshot must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"Unsupported schema_version {data.get('schema_version')!r}; expected {SCHEMA_VERSION}")
    parse_timestamp(data.get("generated_at"), "generated_at")
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        raise SnapshotError("sources must be an object")
    for name, source in sources.items():
        if not isinstance(name, str) or not isinstance(source, dict):
            raise SnapshotError("Each source must be an object")
        if source.get("as_of") is not None:
            parse_timestamp(source.get("as_of"), f"sources.{name}.as_of")
        if "available" in source and not isinstance(source["available"], bool):
            raise SnapshotError(f"sources.{name}.available must be true or false")
    _rows(data, "daily", _daily)
    _rows(data, "account_daily", _account)
    _rows(data, "inventory", _inventory)
    _rows(data, "shipments", _shipment)
    _rows(data, "warehouse", _warehouse)
    _rows(data, "catalog", _catalog)
    _rows(data, "returns", _return)
    return data


def empty_snapshot(generated_at):
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "seller": dict(SELLER),
        "sources": {
            "sales": {"available": False, "report": "Detail Page Sales and Traffic"},
            "inventory": {"available": False, "report": "FBA Manage Inventory"},
            "shipments": {"available": False, "report": "FBA shipments"},
            "warehouse": {"available": False, "report": "Smart Warehousing inventory", "location": SELLER["warehouse"]},
            "returns": {"available": False, "report": "FBA customer returns"},
        },
        "catalog": [],
        "daily": [],
        "account_daily": [],
        "inventory": [],
        "shipments": [],
        "warehouse": [],
        "returns": [],
    }
