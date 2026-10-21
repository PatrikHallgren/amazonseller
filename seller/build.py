"""Merge Seller Central and Smart Warehousing exports into snapshot.json."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .core import PACIFIC, iso, now
from .reports import load_mapping, mapping_sku, parse_inventory, parse_returns, parse_sales, parse_shipments, parse_warehouse
from .schema import SCHEMA_VERSION, SELLER, empty_snapshot, validate_snapshot, SnapshotError


def _end_of_day(day):
    start = datetime(day.year, day.month, day.day, tzinfo=PACIFIC) + timedelta(days=1)
    return iso(start)


def _contiguous(days):
    ordered = sorted(set(days))
    return all((b - a).days == 1 for a, b in zip(ordered, ordered[1:]))


def _upsert(existing, rows, key):
    index = {key(row): dict(row) for row in existing or []}
    for row in rows:
        index[key(row)] = dict(row)
    return [index[name] for name in sorted(index)]


def _merge_inventory(existing, rows):
    index = {row["sku"]: dict(row) for row in existing or []}
    for row in rows:
        current = index.setdefault(row["sku"], {"sku": row["sku"]})
        for key, value in row.items():
            if value is not None and value != "":
                current[key] = value
        restock = current.get("restock")
        if isinstance(restock, dict) and restock.get("quantity") is None and not restock.get("ship_date"):
            current.pop("restock", None)
    return [index[sku] for sku in sorted(index)]


def _public_inventory(rows):
    published = []
    for row in rows:
        item = {"sku": row["sku"]}
        for key in ("asin", "name", "available", "reserved", "unfulfillable", "inbound_working",
                    "inbound_shipped", "inbound_receiving", "days_of_supply"):
            if row.get(key) not in (None, ""):
                item[key] = row[key]
        restock = row.get("restock")
        if isinstance(restock, dict) and (restock.get("quantity") is not None or restock.get("ship_date")):
            item["restock"] = {"quantity": restock.get("quantity"), "ship_date": restock.get("ship_date") or ""}
        published.append(item)
    return published


def _public_daily(rows):
    published = []
    for row in sorted(rows, key=lambda item: (item["date"].isoformat() if isinstance(item["date"], date) else item["date"], item["sku"])):
        day = row["date"].isoformat() if isinstance(row["date"], date) else row["date"]
        item = {"date": day, "sku": row["sku"], "units": int(row["units"])}
        if row.get("asin"):
            item["asin"] = row["asin"]
        if row.get("orders") is not None:
            item["orders"] = int(row["orders"])
        if row.get("sales") is not None:
            item["sales"] = row["sales"]
        published.append(item)
    return published


def _resolve_sales(rows, inventory, mapping):
    by_asin = {}
    for row in inventory:
        if row.get("asin") and row["asin"] not in by_asin:
            by_asin[row["asin"]] = row["sku"]
    for spec in mapping.values():
        asin = (spec.get("asin") or "").strip()
        sku = mapping_sku(spec)
        if asin and sku and asin not in by_asin:
            by_asin[asin] = sku
    resolved = []
    warnings = []
    seen = set()
    for row in rows:
        sku = (row.get("sku") or "").strip()
        asin = (row.get("asin") or "").strip()
        if not sku and asin:
            sku = by_asin.get(asin, "")
            if not sku and asin not in seen:
                warnings.append(f"Sales ASIN {asin} has no seller SKU; it is stored under the ASIN until inventory or mapping links it")
                seen.add(asin)
            sku = sku or asin
        if not sku:
            continue
        resolved.append(dict(row, sku=sku, asin=asin))
    return resolved, warnings


def _rebuild_catalog(data):
    by_sku = {row["sku"]: dict(row) for row in data.get("catalog", []) if row.get("sku")}
    for row in data.get("inventory", []):
        item = by_sku.setdefault(row["sku"], {"sku": row["sku"]})
        if row.get("name"):
            item["name"] = row["name"]
        if row.get("asin"):
            item["asin"] = row["asin"]
    for row in data.get("warehouse", []):
        sku = row.get("product_sku")
        if not sku:
            continue
        item = by_sku.setdefault(sku, {"sku": sku})
        item["warehouse_sku"] = row["sku"]
        if row.get("units_per_case"):
            item["units_per_case"] = int(row["units_per_case"])
        # Mapped product names stay short ("PotLifter") when the 3PL file is linked.
        if row.get("product_name"):
            item["name"] = row["product_name"]
        if row.get("product_asin") and not item.get("asin"):
            item["asin"] = row["product_asin"]
    for row in data.get("daily", []):
        item = by_sku.setdefault(row["sku"], {"sku": row["sku"]})
        if row.get("asin") and not item.get("asin"):
            item["asin"] = row["asin"]
    data["catalog"] = [by_sku[sku] for sku in sorted(by_sku)]
    for row in data.get("warehouse", []):
        row.pop("product_name", None)
        row.pop("product_asin", None)


def _source(report, as_of=None, available=True, **extra):
    value = {"available": available, "report": report}
    if as_of:
        value["as_of"] = as_of
    value.update(extra)
    return value


def build_snapshot(*, sales=None, inventory=None, shipments=None, warehouse=None, returns=None,
                   mapping=None, previous=None, generated_at=None):
    """Return (snapshot, warnings).

    `sales` is a list of (date or None, report text). None for an omitted input
    keeps that section from `previous`.
    """
    generated_at = generated_at or iso(now())
    if previous:
        validate_snapshot(previous)
        data = copy.deepcopy(previous)
        data["schema_version"] = SCHEMA_VERSION
        data["generated_at"] = generated_at
        data.setdefault("seller", dict(SELLER))
        data.setdefault("sources", {})
    else:
        data = empty_snapshot(generated_at)
    mapping = mapping if mapping is not None else load_mapping()
    warnings = []

    if inventory is not None:
        parsed = []
        for text in inventory:
            parsed.extend(parse_inventory(text))
        merged = _merge_inventory([], parsed)
        data["inventory"] = _public_inventory(merged)
        dates = [row["snapshot_date"] for row in merged if isinstance(row.get("snapshot_date"), date)]
        as_of = _end_of_day(max(dates)) if dates else generated_at
        data["sources"]["inventory"] = _source("FBA Manage Inventory / Inventory Health", as_of, available=bool(data["inventory"]))
    if sales is not None:
        child, account = [], []
        for forced, text in sales:
            rows, kind = parse_sales(text, forced)
            (account if kind == "account" else child).extend(rows)
        inventory_rows = data.get("inventory", [])
        # Inventory names live on the snapshot; resolution also uses ASIN.
        lookup_rows = []
        for row in inventory_rows:
            lookup_rows.append({"sku": row.get("sku"), "asin": row.get("asin") or ""})
        child, sales_warnings = _resolve_sales(child, lookup_rows, mapping)
        warnings.extend(sales_warnings)
        if child:
            normalized = []
            for row in child:
                normalized.append({
                    "date": row["date"], "sku": row["sku"], "asin": row.get("asin") or "",
                    "units": row["units"], "orders": row.get("orders"), "sales": row.get("sales"), "cents": row.get("cents"),
                })
            data["daily"] = _public_daily(_upsert(data.get("daily", []), normalized, lambda row: (
                row["date"].isoformat() if isinstance(row["date"], date) else row["date"], row["sku"])))
        if account:
            account_rows = [{"date": row["date"].isoformat(), "units": row["units"], "orders": row.get("orders"), "sales": row.get("sales")}
                            for row in account]
            for row in account_rows:
                if row.get("orders") is None:
                    row.pop("orders")
                if row.get("sales") is None:
                    row.pop("sales")
            data["account_daily"] = _upsert(data.get("account_daily", []), account_rows, lambda row: row["date"])
        child_days = [date.fromisoformat(row["date"]) for row in data.get("daily", [])]
        account_days = [date.fromisoformat(row["date"]) for row in data.get("account_daily", [])]
        series = child_days or account_days
        if series:
            data["sources"]["sales"] = _source(
                "Detail Page Sales and Traffic",
                _end_of_day(max(series)),
                available=True,
                start=min(series).isoformat(),
                end=max(series).isoformat(),
                complete=_contiguous(series),
            )
        else:
            data["sources"]["sales"] = _source("Detail Page Sales and Traffic", available=False)
    if shipments is not None:
        parsed = []
        for text in shipments:
            parsed.extend(parse_shipments(text))
        data["shipments"] = sorted(parsed, key=lambda row: row["id"])
        updated = [row["updated"] for row in data["shipments"] if row.get("updated")]
        as_of = _end_of_day(date.fromisoformat(max(updated))) if updated else generated_at
        data["sources"]["shipments"] = _source("FBA shipments", as_of, available=bool(data["shipments"]))
    if warehouse is not None:
        parsed = []
        for text in warehouse:
            rows, notes = parse_warehouse(text, mapping)
            parsed.extend(rows)
            warnings.extend(notes)
        cleaned = []
        for row in parsed:
            item = {key: row[key] for key in ("sku", "cases_on_hand", "units_per_case", "units_on_hand", "product_sku") if row.get(key) is not None}
            for key in ("description", "cases_per_pallet", "weight", "case_upc", "product_name", "product_asin"):
                if row.get(key) not in (None, ""):
                    item[key] = row[key]
            item["mapped"] = bool(row.get("mapped"))
            cleaned.append(item)
        data["warehouse"] = sorted(cleaned, key=lambda row: row["sku"])
        linked = [row["product_sku"] for row in data["warehouse"] if row.get("product_sku")]
        missing = sorted({row["product_sku"] for row in data["warehouse"] if row.get("product_sku")} - {row["sku"] for row in data.get("inventory", [])})
        if data.get("inventory") and missing:
            warnings.append("3PL mapping does not match an FBA SKU: " + ", ".join(missing) + ". Set sku in the mapping file to the Seller Central SKU.")
        data["sources"]["warehouse"] = _source(
            "Smart Warehousing inventory", generated_at, available=bool(data["warehouse"]), location=SELLER["warehouse"])
        if linked:
            data["sources"]["warehouse"]["products"] = sorted(set(linked))
    if returns is not None:
        parsed = []
        for text in returns:
            parsed.extend(parse_returns(text))
        data["returns"] = _upsert([], parsed, lambda row: (row["date"], row["sku"], row.get("reason") or "", row.get("units")))
        days = [date.fromisoformat(row["date"]) for row in data["returns"]]
        data["sources"]["returns"] = _source(
            "FBA customer returns", _end_of_day(max(days)) if days else None, available=bool(days))
    for source in data.get("sources", {}).values():
        if source.get("available") is False or source.get("as_of") in ("", None):
            source.pop("as_of", None)
    _rebuild_catalog(data)
    # Drop builder-only fields if a previous file carried them.
    for row in data.get("daily", []):
        row.pop("cents", None)
        row.pop("name", None)
    validate_snapshot(data)
    return data, warnings


def write_snapshot(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".snapshot-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def parse_sales_arg(value):
    if len(value) > 11 and value[10] == "=" and value[:10].count("-") == 2:
        try:
            day = date.fromisoformat(value[:10])
        except ValueError:
            raise SnapshotError("Sales date prefix must be YYYY-MM-DD=file.csv") from None
        return day, value[11:]
    return None, value


def _read(path):
    file = Path(path)
    if not file.is_file():
        raise SnapshotError("File not found: " + str(file))
    return file.read_text(encoding="utf-8-sig")


def run_build(args):
    mapping = load_mapping(args.mapping)
    previous = None
    if args.previous:
        try:
            previous = json.loads(Path(args.previous).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            raise SnapshotError("Previous snapshot could not be read") from None
    sales = None
    if args.sales:
        sales = []
        for value in args.sales:
            day, path = parse_sales_arg(value)
            sales.append((day, _read(path)))
    data, warnings = build_snapshot(
        sales=sales,
        inventory=[_read(path) for path in args.inventory] if args.inventory else None,
        shipments=[_read(path) for path in args.shipments] if args.shipments else None,
        warehouse=[_read(path) for path in args.warehouse] if args.warehouse else None,
        returns=[_read(path) for path in args.returns] if args.returns else None,
        mapping=mapping,
        previous=previous,
    )
    write_snapshot(args.output, data)
    print(
        f"Wrote {args.output} · {len(data['daily'])} daily rows · {len(data['inventory'])} FBA SKUs · "
        f"{len(data['warehouse'])} 3PL rows · {len(data['shipments'])} shipments")
    for warning in warnings:
        print("Warning: " + warning, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_snapshot", description="Build a daily Amazon Seller snapshot from exports")
    parser.add_argument("--sales", action="append", default=[], metavar="DATE=FILE",
                        help="Detail Page Sales and Traffic CSV. Prefix YYYY-MM-DD= when the file has no Date column")
    parser.add_argument("--inventory", action="append", default=[], metavar="FILE", help="FBA Manage Inventory or Inventory Health CSV")
    parser.add_argument("--shipments", action="append", default=[], metavar="FILE", help="FBA shipments list")
    parser.add_argument("--warehouse", action="append", default=[], metavar="FILE", help="Smart Warehousing CSV or JSON (quantities are cases)")
    parser.add_argument("--returns", action="append", default=[], metavar="FILE", help="Optional FBA customer returns CSV")
    parser.add_argument("--mapping", help="JSON object of 3PL SKU → {sku, name, units_per_case}")
    parser.add_argument("--previous", help="Existing snapshot.json; sales history is kept and re-downloaded days replace old rows")
    parser.add_argument("--output", required=True, help="Destination snapshot.json (not this public repo)")
    run_build(parser.parse_args(argv))


if __name__ == "__main__":
    try:
        main()
    except (SnapshotError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
