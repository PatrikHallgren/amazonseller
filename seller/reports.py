"""Tolerant parsers for Seller Central exports and the Sumner 3PL report.

Quantities in the Smart Warehousing export are cases. PLX-20-BX is PotLifter
(20 units, seller SKU 856455001068, ASIN B002ECFFJ8). PRLX-8-BX is ProLifter
(8 units). The Sumner "Qty per case" column is often 1; the mapping value wins.
Set ProLifter's numeric seller SKU in the mapping file.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path

from .core import PACIFIC, dt
from .schema import ACTION_TYPES, SnapshotError

# Warehouse SKU → Seller Central product. Override with --mapping.
# PotLifter's seller SKU and ASIN are the live listing. ProLifter's seller SKU
# is also numeric; leave it blank here so a placeholder cannot join the wrong row.
DEFAULT_MAPPING = {
    "PLX-20-BX": {"sku": "856455001068", "name": "PotLifter", "units_per_case": 20, "asin": "B002ECFFJ8"},
    "PRLX-8-BX": {"sku": "", "name": "ProLifter", "units_per_case": 8, "asin": ""},
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
    "fc transfer": "fc_transfer", "reserved fc transfer": "fc_transfer",
    "afn fc transfer quantity": "fc_transfer",
    "fc processing": "fc_processing", "customer order": "customer_order",
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
    "quantity shipped": "shipped", "units shipped": "shipped", "shipped": "shipped",
    "quantity received": "received", "received": "received",
    "units expected": "units_expected", "units located": "units_located", "units received": "units_located",
    "last updated": "updated", "created": "created", "date created": "created",
    "destination": "destination", "ship to": "destination",
    "delivery window": "delivery_window",
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


# Amazon writes these when a cell has no value. Comparison is case-insensitive.
_MISSING = {"", "--", "—", "-", "n/a", "na", "n.a", "n.a.", "null", "none"}
_DAY_IN_NAME = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


def _token(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _blank(value):
    return _token(value) in _MISSING


def _note(warnings, message):
    if warnings is not None and message not in warnings:
        warnings.append(message)


def parse_int(value, warnings=None, label="A quantity"):
    if _blank(value):
        if _token(value) not in ("",) and label != "A quantity":
            _note(warnings, f"{label} was {str(value).strip()!r} and was left empty")
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    text = text.replace(">", "").replace("+", "").strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        _note(warnings, f"{label} {str(value).strip()!r} is not a number and was left empty")
        return None
    if not number.is_finite():
        _note(warnings, f"{label} {str(value).strip()!r} is not a number and was left empty")
        return None
    return int(number.to_integral_value(rounding=ROUND_HALF_UP))


def parse_cents(value, warnings=None, label="Ordered Product Sales"):
    if _blank(value):
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("USD", "").strip()
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        number = Decimal(text)
    except InvalidOperation:
        _note(warnings, f"{label} {str(value).strip()!r} is not a dollar amount and was left empty")
        return None
    if not number.is_finite():
        _note(warnings, f"{label} {str(value).strip()!r} is not a dollar amount and was left empty")
        return None
    cents = int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


def cents_to_amount(cents):
    if cents is None:
        return None
    whole, part = divmod(abs(int(cents)), 100)
    text = f"{whole}.{part:02d}"
    return float(text) if int(cents) >= 0 else float("-" + text)


def parse_day(value, warnings=None, label="A report date"):
    """Return a date, or None when the cell is missing or unreadable.

    none, n/a, na, -, and blank are missing (any capitalization). An unreadable
    cell is reported once and does not abort the build.
    """
    if value is None or str(value).strip() == "":
        return None
    if _blank(value):
        _note(warnings, f"{label} was {str(value).strip()!r} and was left empty")
        return None
    text = str(value).strip()
    if "T" in text or text.endswith("Z") or (len(text) > 10 and "+" in text[10:]):
        try:
            return dt(text).astimezone(PACIFIC).date()
        except ValueError:
            _note(warnings, f"{label} {text!r} could not be read and was left empty")
            return None
    head = text.split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d-%b-%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text if fmt == "%b %d, %Y" else head, fmt).date()
        except ValueError:
            continue
    _note(warnings, f"{label} {text!r} could not be read and was left empty")
    return None


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


def dates_in_name(name):
    """YYYY-MM-DD dates embedded in a filename, in the order they appear."""
    return [datetime.strptime(match, "%Y-%m-%d").date() for match in _DAY_IN_NAME.findall(Path(name).name)]


def inventory_report_kind(text):
    """Tell Manage FBA Inventory apart from the Restock and Inventory Health exports."""
    try:
        header = normalize_header(table_body(text).splitlines()[0])
    except SnapshotError:
        return "other"
    if "afn fulfillable quantity" in header or "afn warehouse quantity" in header:
        return "manage"
    if "fc transfer" in header or "recommended replenishment qty" in header or "recommended ship date" in header:
        return "restock"
    if "snapshot date" in header:
        return "health"
    return "other"


def parse_sales(text, forced_date=None, range_end=None, warnings=None):
    """Return (rows, kind).

    kind is 'child' or 'account' when each row has a day, and 'range' when the
    file is a multi-day total with no Date column. A range is not daily history.
    """
    warnings = warnings if warnings is not None else []
    rows = read_table(text, SALES_ALIASES)
    if not rows:
        raise SnapshotError("Sales report has no data rows")
    per_item = any(row.get("sku") or row.get("child_asin") or row.get("asin") for row in rows)
    dated = any(str(row.get("date") or "").strip() for row in rows)
    if not per_item and not any("units" in row or "sales" in row or "orders" in row for row in rows):
        raise SnapshotError("Sales report is missing Units Ordered, Ordered Product Sales, or Total Order Items")
    if per_item and not any("units" in row for row in rows):
        raise SnapshotError("Child Item sales report is missing a Units Ordered column")
    range_only = (not dated) and (range_end is not None or forced_date is None)
    parsed = []
    for row in rows:
        if per_item:
            sku = (row.get("sku") or "").strip()
            asin = (row.get("child_asin") or row.get("asin") or "").strip()
            if not sku and not asin:
                continue
        else:
            sku, asin = "", ""
        if dated:
            day = parse_day(row.get("date"), warnings, "Sales date")
            if day is None:
                continue
        elif range_only:
            day = None
        else:
            day = forced_date
        item = _sales_metrics(row, day, warnings)
        if item is None:
            continue
        if per_item:
            item.update(sku=sku, asin=asin, name=(row.get("title") or "").strip())
        if range_only:
            item["start"] = forced_date.isoformat() if forced_date else ""
            item["end"] = range_end.isoformat() if range_end else ""
        parsed.append(item)
    if not parsed:
        raise SnapshotError("Sales report has no usable rows")
    if range_only:
        return parsed, "range"
    return parsed, "child" if per_item else "account"


def _sales_metrics(row, day, warnings):
    if "units" in row:
        units = parse_int(row.get("units"), warnings, "Units Ordered")
        if units is None and not _blank(row.get("units")):
            return None
        units = 0 if units is None else units
    else:
        units = 0
    orders = parse_int(row.get("orders"), warnings, "Total Order Items") if "orders" in row else None
    cents = parse_cents(row.get("sales"), warnings) if "sales" in row else None
    return {"date": day, "units": units, "orders": orders, "sales": cents_to_amount(cents), "cents": cents}


def parse_inventory(text, warnings=None):
    warnings = warnings if warnings is not None else []
    rows = read_table(text, INVENTORY_ALIASES)
    if not rows:
        raise SnapshotError("Inventory report has no data rows")
    kind = inventory_report_kind(text)
    parsed = []
    quantities = ("available", "reserved", "unfulfillable", "inbound_working", "inbound_shipped",
                  "inbound_receiving", "fc_transfer", "fc_processing", "customer_order")
    for row in rows:
        sku = (row.get("sku") or "").strip()
        if not sku:
            continue
        condition = (row.get("condition") or "").strip().lower()
        if condition and condition not in ("new", "newitem", "new item"):
            continue
        item = {"sku": sku, "asin": (row.get("asin") or "").strip(), "name": (row.get("name") or "").strip(), "_report": kind}
        for key in quantities:
            if key in row:
                item[key] = parse_int(row.get(key), warnings, key.replace("_", " "))
        if "days_of_supply" in row:
            item["days_of_supply"] = parse_int(row.get("days_of_supply"), warnings, "Days of supply")
        if row.get("snapshot_date"):
            day = parse_day(row.get("snapshot_date"), warnings, "Inventory snapshot date")
            if day:
                item["snapshot_date"] = day
        if "restock_qty" in row or "restock_date" in row:
            ship_day = parse_day(row.get("restock_date"), warnings, "Recommended ship date") if row.get("restock_date") else None
            item["restock"] = {
                "quantity": parse_int(row.get("restock_qty"), warnings, "Recommended replenishment qty") if "restock_qty" in row else None,
                "ship_date": ship_day.isoformat() if ship_day else "",
            }
        parsed.append(item)
    if not parsed:
        raise SnapshotError("Inventory report has no New-condition SKU rows")
    return parsed


def parse_shipments(text, warnings=None):
    warnings = warnings if warnings is not None else []
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
        if row.get("destination"):
            current["destination"] = row["destination"].strip()
        if row.get("delivery_window"):
            current["delivery_window"] = row["delivery_window"].strip()
        updated = parse_day(row.get("updated") or row.get("created"), warnings, "Shipment date")
        if updated and (current["updated"] is None or updated > current["updated"]):
            current["updated"] = updated
        for key, label in (("units_expected", "Units expected"), ("units_located", "Units located")):
            if key in row:
                value = parse_int(row.get(key), warnings, label)
                if value is not None:
                    current[key] = value
        # "SKUs" is a count of SKUs on the shipment, not a seller SKU. It is not aliased.
        sku = (row.get("sku") or "").strip()
        if not sku:
            continue
        current["items"].append({
            "sku": sku,
            "asin": (row.get("asin") or "").strip(),
            "shipped": parse_int(row.get("shipped"), warnings, "Quantity shipped") if "shipped" in row else None,
            "received": parse_int(row.get("received"), warnings, "Quantity received") if "received" in row else None,
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
            mapped_units = parse_int(spec.get("units_per_case"), warnings, f"{sku} units per case")
        column_units = parse_int(row.get("units_per_case"), warnings, f"{sku} Qty per case") if "units_per_case" in row else None
        # The portal's Qty per case is often 1. The mapping is the real case size.
        if mapped_units:
            units_per_case = mapped_units
            if column_units is not None and column_units != mapped_units:
                warnings.append(f"{sku} Qty per case is {column_units}; using the configured {mapped_units} units per case")
        else:
            units_per_case = column_units
        cases = parse_int(row.get("cases"), warnings, f"{sku} Qty on hand") if "cases" in row else None
        product = mapping_sku(spec) if spec else None
        if cases is not None and not units_per_case:
            warnings.append(f"{sku} is not in the case-size mapping; cases were kept and units were not converted")
        elif spec and not product:
            warnings.append(
                f"{sku} has no Seller Central SKU in the mapping. Units use the configured case size. "
                "Set sku to the numeric seller SKU (PotLifter is 856455001068, ASIN B002ECFFJ8).")
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


class _HTMLTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._rows = None
        self._cells = None
        self._capture = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self._rows = []
        elif tag == "tr" and self._rows is not None:
            self._cells = []
        elif tag in ("td", "th") and self._cells is not None:
            self._capture = True
            self._buf = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._capture:
            self._cells.append(re.sub(r"\s+", " ", "".join(self._buf)).strip())
            self._capture = False
        elif tag == "tr" and self._cells:
            if any(cell.strip() for cell in self._cells):
                self._rows.append(self._cells)
            self._cells = None
        elif tag == "table" and self._rows is not None:
            if self._rows:
                self.tables.append(self._rows)
            self._rows = None

    def handle_data(self, data):
        if self._capture:
            self._buf.append(data)


def html_to_csv(text):
    parser = _HTMLTable()
    parser.feed(text)
    parser.close()
    if not parser.tables:
        raise SnapshotError("This .xls file looks like HTML but has no table. Export CSV from Smart Warehousing.")

    def score(table):
        best = 0
        for cells in table[:8]:
            header = " ".join(normalize_header(cell) for cell in cells)
            best = max(best, sum(token in header for token in ("sku", "qty on hand", "quantity on hand", "units ordered", "asin", "shipment")))
        return best

    table = max(parser.tables, key=lambda item: (score(item), len(item)))
    buffer = io.StringIO()
    csv.writer(buffer).writerows(table)
    return buffer.getvalue()


_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_XLSX = b"PK\x03\x04"
_EXCEL_MESSAGE = (
    "This file is a binary Excel workbook. Smart Warehousing .xls downloads are read when they are an HTML table or plain text. "
    "Export CSV, or in Excel use Save As and choose Web Page, then run the builder again."
)


def read_export(path):
    """Read a CSV, TSV, JSON, HTML-as-.xls, or text export. Reject binary workbooks clearly."""
    file = Path(path)
    if not file.is_file():
        raise SnapshotError("File not found: " + str(file))
    try:
        raw = file.read_bytes()
    except OSError:
        raise SnapshotError("Could not read " + str(file)) from None
    if raw.startswith(_OLE) or raw.startswith(_XLSX):
        raise SnapshotError(_EXCEL_MESSAGE)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = raw.decode("utf-16")
        except UnicodeError:
            raise SnapshotError(f"{file.name} could not be decoded. Export CSV from Smart Warehousing.") from None
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("cp1252")
            except UnicodeDecodeError:
                raise SnapshotError(
                    f"{file.name} is not CSV, TSV, JSON, or an HTML table. Export CSV from Smart Warehousing."
                ) from None
    head = text.lstrip("\ufeff").lstrip()[:800].lower()
    if head.startswith("<") or "<table" in head or "<html" in head:
        return html_to_csv(text)
    return text


ACTION_URLS = {
    "message": "https://sellercentral.amazon.com/messaging/inbox",
    "support": "https://sellercentral.amazon.com/cu/case-lobby",
    "account_health": "https://sellercentral.amazon.com/performance/account/health",
    "policy": "https://sellercentral.amazon.com/performance/account/health",
    "performance": "https://sellercentral.amazon.com/performance/notifications",
    "listing": "https://sellercentral.amazon.com/inventory",
    "compliance": "https://sellercentral.amazon.com/performance/account/health",
    "action_required": "https://sellercentral.amazon.com/home",
}


def parse_actions(text, warnings=None):
    """Read the morning action-item file. JSON list, {\"actions\": [...]}, or CSV."""
    warnings = warnings if warnings is not None else []
    stripped = text.lstrip("\ufeff").lstrip()
    if not stripped:
        return []
    if stripped[0] in "[{":
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            raise SnapshotError("Action items file is not valid JSON") from None
        if isinstance(payload, dict):
            rows = payload.get("actions", [])
        else:
            rows = payload
        if not isinstance(rows, list):
            raise SnapshotError("Action items JSON must be a list or an object with an actions list")
    else:
        rows = list(csv.DictReader(io.StringIO(stripped)))
        if not rows:
            raise SnapshotError("Action items CSV has no data rows")
        headers = {str(key).strip().lower() for key in rows[0] if key is not None}
        if "type" not in headers or "title" not in headers:
            raise SnapshotError("Action items CSV needs columns type, title, detail, date, url, needs_response_by")
    parsed = []
    allowed = ", ".join(sorted(ACTION_TYPES))
    for index, raw in enumerate(rows, start=1):
        if not isinstance(raw, dict):
            raise SnapshotError(f"Action item {index} must be an object")
        item = {}
        for key, value in raw.items():
            if key is None:
                continue
            item[str(key).strip().lower()] = "" if value is None else str(value).strip()
        if not any(item.values()):
            continue
        kind = item.get("type") or ""
        title = item.get("title") or ""
        if kind not in ACTION_TYPES:
            raise SnapshotError(f"Action item {index} type must be one of {allowed}")
        if not title:
            raise SnapshotError(f"Action item {index} needs a title")
        url = item.get("url") or ACTION_URLS[kind]
        if not url.startswith("https://sellercentral.amazon.com/"):
            raise SnapshotError(f"Action item {index} url must start with https://sellercentral.amazon.com/")
        identity = item.get("id") or hashlib.sha256("|".join(
            [kind, title, item.get("date") or "", url]).encode()).hexdigest()[:16]
        row = {"id": identity, "type": kind, "title": title, "url": url}
        if item.get("detail"):
            row["detail"] = item["detail"]
        if item.get("date"):
            row["date"] = item["date"]
        if item.get("needs_response_by"):
            row["needs_response_by"] = item["needs_response_by"]
        parsed.append(row)
    return parsed
