"""Tolerant parsers for Seller Central exports and the Sumner 3PL report.

Quantities in the Smart Warehousing export are cases. PLX-20-BX is PotLifter
(20 units) and PRLX-8-BX is ProLifter (8 units) unless a mapping file overrides them.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .core import PACIFIC, dt
from .schema import SnapshotError

# Warehouse SKU → Seller Central product. Override with --mapping when the
# Amazon seller SKU differs from these labels.
DEFAULT_MAPPING = {
    "PLX-20-BX": {"sku": "POTLIFTER", "name": "PotLifter", "units_per_case": 20, "asin": ""},
    "PRLX-8-BX": {"sku": "PROLIFTER", "name": "ProLifter", "units_per_case": 8, "asin": ""},
}

SALES_ALIASES = {
    "child asin": "child_asin", "asin": "asin", "parent asin": "parent_asin",
    "title": "title", "product name": "title", "product title": "title",
    "sku": "sku", "seller sku": "sku", "merchant sku": "sku", "date": "date",
    "units ordered": "units", "units ordered total": "units", "ordered units": "units",
    "ordered product sales": "sales", "ordered product sales total": "sales",
    "total order items": "orders", "total order items total": "orders",
}
INVENTORY_ALIASES = {
    "sku": "sku", "seller sku": "sku", "merchant sku": "sku", "fnsku": "fnsku",
    "asin": "asin", "product name": "name", "title": "name", "item name": "name",
    "condition": "condition", "snapshot date": "snapshot_date", "date": "snapshot_date",
    "afn fulfillable quantity": "available", "available": "available", "fulfillable quantity": "available",
    "afn reserved quantity": "reserved", "reserved": "reserved", "reserved quantity": "reserved",
    "afn unsellable quantity": "unfulfillable", "unfulfillable": "unfulfillable",
    "unfulfillable quantity": "unfulfillable", "unsellable": "unfulfillable", "unsellable quantity": "unfulfillable",
    "afn inbound working quantity": "inbound_working", "inbound working": "inbound_working", "working": "inbound_working",
    "afn inbound shipped quantity": "inbound_shipped", "inbound shipped": "inbound_shipped", "shipped": "inbound_shipped",
    "afn inbound receiving quantity": "inbound_receiving", "inbound receiving": "inbound_receiving",
    "inbound received": "inbound_receiving", "receiving": "inbound_receiving",
    "days of supply": "days_of_supply", "total days of supply": "days_of_supply",
    "total days of supply including units from open shipments": "days_of_supply",
    "days of supply at amazon fulfillment network": "days_of_supply_fba",
    "recommended replenishment qty": "restock_qty", "recommended ship in quantity": "restock_qty",
    "recommended ship date": "restock_date",
}
SHIPMENT_ALIASES = {
    "shipment id": "id", "shipment name": "name", "name": "name",
    "shipment status": "status", "status": "status",
    "sku": "sku", "seller sku": "sku", "merchant sku": "sku", "asin": "asin",
    "quantity shipped": "shipped", "units expected": "shipped", "units shipped": "shipped", "shipped": "shipped",
    "quantity received": "received", "units received": "received", "units located": "received", "received": "received",
    "last updated": "updated", "created": "created", "date created": "created",
}
WAREHOUSE_ALIASES = {
    "sku": "sku", "item": "sku", "description": "description", "item description": "description",
    "qty on hand": "cases", "quantity on hand": "cases", "qty onhand": "cases", "on hand": "cases",
    "cases on hand": "cases", "cases": "cases", "qty": "cases",
    "qty per case": "units_per_case", "quantity per case": "units_per_case", "units per case": "units_per_case",
    "cases per pallet": "cases_per_pallet", "weight": "weight", "case upc": "case_upc", "upc": "case_upc",
}
RETURN_ALIASES = {
    "return date": "date", "date": "date", "sku": "sku", "seller sku": "sku", "merchant sku": "sku",
    "asin": "asin", "quantity": "units", "qty": "units", "reason": "reason",
}

_HEADER_HINTS = ("asin", "sku", "units ordered", "qty on hand", "quantity on hand", "shipment id", "fulfillable", "merchant sku")


def normalize_header(name):
    text = str(name).replace("\ufeff", "").replace("_", " ").replace("-", " ")
    text = re.sub(r"[()%:/#]", " ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def _blank(value):
    return value is None or str(value).strip() in ("", "--", "—", "-", "N/A", "n/a", "null", "None")


def parse_int(value):
    if _blank(value):
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    text = text.replace(">", "").replace("+", "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise SnapshotError("A quantity column has a value that is not a number") from None
    if not number.is_finite():
        raise SnapshotError("A quantity column has a value that is not a number")
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


def parse_cents(value):
    if _blank(value):
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("USD", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise SnapshotError("Ordered Product Sales has a value that is not a dollar amount") from None
    if not number.is_finite():
        raise SnapshotError("Ordered Product Sales has a value that is not a dollar amount")
    cents = int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def cents_to_amount(cents):
    if cents is None:
        return None
    whole, part = divmod(abs(int(cents)), 100)
    text = f"{whole}.{part:02d}"
    return float(text) if int(cents) >= 0 else float("-" + text)


def parse_day(value):
    if _blank(value):
        return None
    text = str(value).strip()
    if "T" in text or text.endswith("Z") or "+" in text[10:]:
        try:
            return dt(text).astimezone(PACIFIC).date()
        except ValueError:
            raise SnapshotError("A report date could not be read") from None
    head = text.split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text if fmt == "%b %d, %Y" else head, fmt).date()
        except ValueError:
            continue
    raise SnapshotError("A report date could not be read")


def load_mapping(path=None):
    mapping = {key.upper(): dict(value) for key, value in DEFAULT_MAPPING.items()}
    if not path:
        return mapping
    try:
        custom = json.loads(open(path, encoding="utf-8-sig").read())
    except (OSError, json.JSONDecodeError):
        raise SnapshotError("Mapping file must be JSON: warehouse SKU → {sku, name, units_per_case}") from None
    if not isinstance(custom, dict):
        raise SnapshotError("Mapping file must be a JSON object of warehouse SKU → product")
    for key, value in custom.items():
        if not isinstance(value, dict) or not mapping_sku(value):
            raise SnapshotError("Each mapping entry needs a seller sku")
        mapping[str(key).strip().upper()] = value
    return mapping


def mapping_sku(spec):
    if not isinstance(spec, dict):
        return None
    for key in ("sku", "seller_sku", "product_sku"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def table_body(text):
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    lines = str(text).lstrip("\ufeff").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    start = 0
    for index, line in enumerate(lines[:25]):
        lowered = line.lower()
        if any(token in lowered for token in _HEADER_HINTS):
            start = index
            break
    body = "\n".join(lines[start:]).strip()
    if not body:
        raise SnapshotError("Report is empty")
    return body


def read_table(text, aliases):
    body = table_body(text)
    header = body.splitlines()[0]
    delimiter = "\t" if header.count("\t") > header.count(",") else ","
    reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
    if not reader.fieldnames:
        raise SnapshotError("Report is missing a header row")
    rows = []
    for raw in reader:
        item = {}
        for key, value in raw.items():
            if not key:
                continue
            alias = aliases.get(normalize_header(key))
            if alias and alias not in item:
                item[alias] = "" if value is None else str(value).strip()
        if any(str(value).strip() for value in item.values()):
            rows.append(item)
    return rows


def _norm_status(value):
    text = (value or "").strip().upper().replace("-", " ").replace("_", " ")
    text = re.sub(r"\s+", "_", text.strip())
    return {"CANCELED": "CANCELLED", "INTRANSIT": "IN_TRANSIT", "READYTOSHIP": "READY_TO_SHIP",
            "CHECKEDIN": "CHECKED_IN"}.get(text, text or "UNKNOWN")


def parse_sales(text, forced_date=None):
    """Return (rows, kind) with kind 'child' or 'account'."""
    rows = read_table(text, SALES_ALIASES)
    if not rows:
        raise SnapshotError("Sales report has no data rows")
    per_item = any(row.get("sku") or row.get("child_asin") or row.get("asin") for row in rows)
    if not per_item:
        if not any("units" in row or "sales" in row or "orders" in row for row in rows):
            raise SnapshotError("Sales report is missing Units Ordered, Ordered Product Sales, or Total Order Items")
        parsed = []
        for row in rows:
            day = parse_day(row.get("date")) if row.get("date") else forced_date
            if day is None:
                raise SnapshotError("By Date sales report has no Date column. Pass YYYY-MM-DD=file.csv for a one-day download.")
            parsed.append(_sales_metrics(row, day))
        return parsed, "account"
    if not any("units" in row for row in rows):
        raise SnapshotError("Child Item sales report is missing a Units Ordered column")
    parsed = []
    for row in rows:
        sku = (row.get("sku") or "").strip()
        asin = (row.get("child_asin") or row.get("asin") or "").strip()
        if not sku and not asin:
            continue
        day = parse_day(row.get("date")) if row.get("date") else forced_date
        if day is None:
            raise SnapshotError(
                "Sales report has no Date column. Download one day and pass YYYY-MM-DD=file.csv, "
                "or export Detail Page Sales and Traffic with a Date column.")
        item = _sales_metrics(row, day)
        item.update(sku=sku, asin=asin, name=(row.get("title") or "").strip())
        parsed.append(item)
    if not parsed:
        raise SnapshotError("Sales report has no child ASIN or SKU rows")
    return parsed, "child"


def _sales_metrics(row, day):
    units = parse_int(row.get("units")) if "units" in row else 0
    orders = parse_int(row.get("orders")) if "orders" in row else None
    cents = parse_cents(row.get("sales")) if "sales" in row else None
    return {"date": day, "units": 0 if units is None else units, "orders": orders, "sales": cents_to_amount(cents), "cents": cents}


def parse_inventory(text):
    rows = read_table(text, INVENTORY_ALIASES)
    if not rows:
        raise SnapshotError("Inventory report has no data rows")
    parsed = []
    for row in rows:
        sku = (row.get("sku") or "").strip()
        if not sku:
            continue
        condition = (row.get("condition") or "").strip().lower()
        if condition and condition not in ("new", "newitem", "new item"):
            continue
        item = {"sku": sku, "asin": (row.get("asin") or "").strip(), "name": (row.get("name") or "").strip()}
        for key in ("available", "reserved", "unfulfillable", "inbound_working", "inbound_shipped", "inbound_receiving"):
            if key in row:
                item[key] = parse_int(row.get(key))
        if "days_of_supply" in row:
            item["days_of_supply"] = parse_int(row.get("days_of_supply"))
        if row.get("snapshot_date"):
            item["snapshot_date"] = parse_day(row.get("snapshot_date"))
        if "restock_qty" in row or "restock_date" in row:
            ship = row.get("restock_date") or ""
            ship_day = parse_day(ship) if ship and not _blank(ship) else None
            item["restock"] = {
                "quantity": parse_int(row.get("restock_qty")) if "restock_qty" in row else None,
                "ship_date": ship_day.isoformat() if ship_day else "",
            }
        parsed.append(item)
    if not parsed:
        raise SnapshotError("Inventory report has no New-condition SKU rows")
    return parsed


def parse_shipments(text):
    rows = read_table(text, SHIPMENT_ALIASES)
    if not rows:
        raise SnapshotError("Shipments report has no data rows")
    grouped = {}
    for row in rows:
        shipment_id = (row.get("id") or "").strip()
        if not shipment_id:
            continue
        current = grouped.setdefault(shipment_id, {
            "id": shipment_id,
            "name": (row.get("name") or shipment_id).strip() or shipment_id,
            "status": _norm_status(row.get("status")),
            "updated": None,
            "items": [],
        })
        if row.get("status"):
            current["status"] = _norm_status(row.get("status"))
        if row.get("name"):
            current["name"] = row["name"].strip() or current["name"]
        updated = parse_day(row.get("updated") or row.get("created"))
        if updated and (current["updated"] is None or updated > current["updated"]):
            current["updated"] = updated
        sku = (row.get("sku") or "").strip()
        if not sku:
            continue
        current["items"].append({
            "sku": sku,
            "asin": (row.get("asin") or "").strip(),
            "shipped": parse_int(row.get("shipped")) if "shipped" in row else None,
            "received": parse_int(row.get("received")) if "received" in row else None,
        })
    if not grouped:
        raise SnapshotError("Shipments report has no shipment id column")
    result = []
    for shipment in grouped.values():
        if shipment["updated"]:
            shipment["updated"] = shipment["updated"].isoformat()
        else:
            shipment.pop("updated", None)
        result.append(shipment)
    return result


def parse_returns(text):
    rows = read_table(text, RETURN_ALIASES)
    parsed = []
    for row in rows:
        sku = (row.get("sku") or "").strip()
        day = parse_day(row.get("date")) if row.get("date") else None
        if not sku or day is None:
            continue
        units = parse_int(row.get("units")) if "units" in row else None
        if units is None:
            continue
        # customer-comments and buyer fields are never read.
        parsed.append({"date": day.isoformat(), "sku": sku, "asin": (row.get("asin") or "").strip(),
                       "units": units, "reason": (row.get("reason") or "").strip() or "Unspecified"})
    return parsed


def parse_warehouse(text, mapping):
    stripped = text.decode("utf-8-sig").lstrip() if isinstance(text, bytes) else str(text).lstrip("\ufeff").lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            raise SnapshotError("Warehouse JSON could not be read") from None
        raw_rows = payload if isinstance(payload, list) else None
        if isinstance(payload, dict):
            raw_rows = payload.get("inventory") or payload.get("items") or payload.get("rows")
            if raw_rows is None and "sku" in {normalize_header(key) for key in payload}:
                raw_rows = [payload]
        if not isinstance(raw_rows, list):
            raise SnapshotError("Warehouse JSON must be an array of items")
        rows = []
        for raw in raw_rows:
            if not isinstance(raw, dict):
                raise SnapshotError("Warehouse JSON items must be objects")
            item = {}
            for key, value in raw.items():
                alias = WAREHOUSE_ALIASES.get(normalize_header(str(key)))
                if alias and alias not in item:
                    item[alias] = value
            rows.append(item)
    else:
        rows = read_table(text, WAREHOUSE_ALIASES)
    if not rows:
        raise SnapshotError("Warehouse report has no data rows")
    parsed = []
    warnings = []
    for row in rows:
        sku = str(row.get("sku") or "").strip()
        if not sku:
            continue
        spec = mapping.get(sku.upper())
        mapped_units = None
        if spec and spec.get("units_per_case") not in (None, ""):
            mapped_units = parse_int(spec.get("units_per_case"))
        column_units = parse_int(row.get("units_per_case")) if "units_per_case" in row else None
        units_per_case = mapped_units or column_units
        cases = parse_int(row.get("cases")) if "cases" in row else None
        product = mapping_sku(spec) if spec else None
        if cases is not None and not units_per_case:
            warnings.append(f"{sku} is not in the case-size mapping; cases were kept and units were not converted")
        parsed.append({
            "sku": sku,
            "description": str(row.get("description") or (spec or {}).get("name") or "").strip(),
            "cases_on_hand": cases,
            "units_per_case": units_per_case,
            "units_on_hand": None if cases is None or not units_per_case else cases * int(units_per_case),
            "cases_per_pallet": parse_int(row.get("cases_per_pallet")) if "cases_per_pallet" in row else None,
            "weight": None if _blank(row.get("weight")) else str(row.get("weight")).strip(),
            "case_upc": None if _blank(row.get("case_upc")) else str(row.get("case_upc")).strip(),
            "product_sku": product,
            "product_name": (spec or {}).get("name") if spec else None,
            "product_asin": ((spec or {}).get("asin") or "").strip() if spec else "",
            "mapped": bool(product),
        })
    if not parsed:
        raise SnapshotError("Warehouse report has no SKU rows")
    return parsed, warnings
