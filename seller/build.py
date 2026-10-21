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

from .core import PACIFIC, dt, iso, now
from .reports import (
    dates_in_name, inventory_report_kind, load_mapping, mapping_sku, parse_actions, parse_inventory, parse_returns,
    parse_sales, parse_shipments, parse_warehouse, read_export,
)
from .schema import SCHEMA_VERSION, SALES_HISTORY_DAYS, SELLER, empty_snapshot, validate_snapshot, SnapshotError


def _end_of_day(day):
    start = datetime(day.year, day.month, day.day, tzinfo=PACIFIC) + timedelta(days=1)
    return iso(start)


def _not_after(as_of, generated_at):
    """A report date of today must not become midnight tonight, which is still in the future."""
    if not as_of or not generated_at:
        return as_of
    try:
        if dt(as_of) > dt(generated_at):
            return generated_at
    except (TypeError, ValueError):
        return generated_at
    return as_of


def _trim_sales_history(data, warnings):
    """Keep at most about two years, ending on the newest sales day."""
    dated = []
    for row in list(data.get("daily", [])) + list(data.get("account_daily", [])):
        try:
            dated.append(date.fromisoformat(row["date"]))
        except (TypeError, ValueError, KeyError):
            continue
    if not dated:
        return
    latest = max(dated)
    earliest = latest - timedelta(days=SALES_HISTORY_DAYS - 1)
    if min(dated) >= earliest:
        return
    data["daily"] = [row for row in data.get("daily", []) if date.fromisoformat(row["date"]) >= earliest]
    data["account_daily"] = [row for row in data.get("account_daily", []) if date.fromisoformat(row["date"]) >= earliest]
    warnings.append(
        f"Sales history is kept for {SALES_HISTORY_DAYS} days through {latest.isoformat()} (about two years). Older rows were left out."
    )


def _contiguous(days):
    ordered = sorted(set(days))
    return all((b - a).days == 1 for a, b in zip(ordered, ordered[1:]))


def _upsert(existing, rows, key):
    index = {key(row): dict(row) for row in existing or []}
    for row in rows:
        index[key(row)] = dict(row)
    return [index[name] for name in sorted(index)]


# Manage FBA Inventory owns fulfillable quantity. Restock fills reserved, inbound,
# and FC transfer only when Manage did not include that column. File order does not matter.
_INVENTORY_RANK = {"manage": 3, "restock": 2, "health": 1, "other": 1}
_AVAILABLE_RANK = {"manage": 4, "health": 3, "other": 2, "restock": 1}
_FILL_FROM_RESTOCK = ("reserved", "unfulfillable", "inbound_working", "inbound_shipped", "inbound_receiving", "fc_transfer")


def _merge_inventory(groups):
    """groups is a list of (report kind, rows)."""
    index = {}
    for kind, rows in groups:
        for row in rows:
            current = index.setdefault(row["sku"], {"sku": row["sku"], "_from": {}})
            origin = current["_from"]
            for key in ("asin", "name", "snapshot_date"):
                if row.get(key) not in (None, "") and key not in origin:
                    current[key] = row[key]
                    origin[key] = kind
            if "available" in row and row.get("available") is not None:
                previous = origin.get("available")
                if previous is None or _AVAILABLE_RANK.get(kind, 0) >= _AVAILABLE_RANK.get(previous, 0):
                    current["available"] = row["available"]
                    origin["available"] = kind
            for key in _FILL_FROM_RESTOCK:
                if key not in row or row.get(key) is None:
                    continue
                previous = origin.get(key)
                if previous is None or _INVENTORY_RANK.get(kind, 0) >= _INVENTORY_RANK.get(previous, 0):
                    current[key] = row[key]
                    origin[key] = kind
            if "days_of_supply" in row and row.get("days_of_supply") is not None:
                # Health's snapshot figure wins, then Restock. Manage rarely has this column.
                rank = {"health": 3, "restock": 2, "manage": 1, "other": 1}
                previous = origin.get("days_of_supply")
                if previous is None or rank.get(kind, 0) >= rank.get(previous, 0):
                    current["days_of_supply"] = row["days_of_supply"]
                    origin["days_of_supply"] = kind
            if kind == "restock" and isinstance(row.get("restock"), dict):
                current["restock"] = row["restock"]
            elif isinstance(row.get("restock"), dict) and "restock" not in current:
                current["restock"] = row["restock"]
            if "reserved" not in origin:
                parts = [row.get(key) for key in ("fc_transfer", "fc_processing", "customer_order") if row.get(key) is not None]
                if parts and kind == "restock":
                    current["reserved"] = sum(parts)
                    origin["reserved"] = "restock"
    merged = []
    for sku in sorted(index):
        row = index[sku]
        row.pop("_from", None)
        restock = row.get("restock")
        if isinstance(restock, dict) and restock.get("quantity") is None and not restock.get("ship_date"):
            row.pop("restock", None)
        merged.append(row)
    return merged


def _public_inventory(rows):
    published = []
    for row in rows:
        item = {"sku": row["sku"]}
        for key in ("asin", "name", "available", "reserved", "unfulfillable", "inbound_working",
                    "inbound_shipped", "inbound_receiving", "fc_transfer", "days_of_supply"):
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
                warnings.append(f"Sales ASIN {asin} has no seller SKU; it is kept under the ASIN until inventory or mapping links it")
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


def _sales_tuple(item):
    if len(item) == 2:
        forced, text = item
        return forced, text, None, ""
    if len(item) == 3:
        forced, text, range_end = item
        return forced, text, range_end, ""
    forced, text, range_end, label = item[:4]
    return forced, text, range_end, label or ""


def _mark_provisional(data):
    """The newest By Date day stays provisional until a later snapshot includes a newer day."""
    account = data.get("account_daily") or []
    if not account:
        return
    latest = max(row["date"] for row in account)
    for row in account:
        if row["date"] == latest:
            row["provisional"] = True
        else:
            row.pop("provisional", None)
    for row in data.get("daily", []):
        if row["date"] == latest:
            row["provisional"] = True
        else:
            row.pop("provisional", None)


def _reconcile(daily, ranges, warnings):
    checks = []
    for label, rows in ranges:
        start = next((row.get("start") for row in rows if row.get("start")), "")
        end = next((row.get("end") for row in rows if row.get("end")), "")
        if not start or not end:
            warnings.append(
                "A sales report with no Date column was not stored as daily rows"
                + (f" ({label})" if label else "")
                + ". Pass YYYY-MM-DD=file for one day, a folder of sales_by_child_item_YYYY-MM-DD.csv files, "
                "or START..END=file to check a multi-day total."
            )
        compared = []
        for row in rows:
            sku = row.get("sku") or row.get("asin") or ""
            units = int(row.get("units") or 0)
            entry = {"sku": sku, "units": units}
            if start and end and sku:
                have = sum(item["units"] for item in daily if item.get("sku") == sku and start <= item["date"] <= end)
                entry["daily_units"] = have
                entry["match"] = have == units
                if have != units:
                    warnings.append(
                        f"{sku} {start}..{end} reports {units} units; daily rows in that range sum to {have}"
                        + (f" ({label})" if label else "")
                    )
            compared.append({key: value for key, value in entry.items() if value not in ("", None)})
        checks.append({"label": label or "sales range", "start": start, "end": end, "rows": compared})
    return checks


def _source(report, as_of=None, available=True, **extra):
    value = {"available": available, "report": report}
    if as_of:
        value["as_of"] = as_of
    value.update(extra)
    return value


def build_snapshot(*, sales=None, inventory=None, shipments=None, warehouse=None, returns=None,
                   actions=None, mapping=None, previous=None, generated_at=None):
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
        groups = []
        for text in inventory:
            groups.append((inventory_report_kind(text), parse_inventory(text, warnings)))
        merged = _merge_inventory(groups)
        data["inventory"] = _public_inventory(merged)
        dates = [row["snapshot_date"] for row in merged if isinstance(row.get("snapshot_date"), date)]
        as_of = _end_of_day(max(dates)) if dates else generated_at
        data["sources"]["inventory"] = _source("FBA Manage Inventory / Inventory Health", as_of, available=bool(data["inventory"]))
    if sales is not None:
        child, account, ranges = [], [], []
        for spec in sales:
            forced, text, range_end, label = _sales_tuple(spec)
            rows, kind = parse_sales(text, forced, range_end, warnings)
            if kind == "range":
                ranges.append((label, rows))
            elif kind == "account":
                account.extend(rows)
            else:
                child.extend(rows)
        inventory_rows = data.get("inventory", [])
        # Inventory names live on the snapshot; resolution also uses ASIN.
        lookup_rows = []
        for row in inventory_rows:
            lookup_rows.append({"sku": row.get("sku"), "asin": row.get("asin") or ""})
        child, sales_warnings = _resolve_sales(child, lookup_rows, mapping)
        warnings.extend(sales_warnings)
        resolved_ranges = []
        for label, rows in ranges:
            linked, notes = _resolve_sales(rows, lookup_rows, mapping)
            warnings.extend(notes)
            resolved_ranges.append((label, linked))
        ranges = resolved_ranges
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
        _trim_sales_history(data, warnings)
        child_days = [date.fromisoformat(row["date"]) for row in data.get("daily", [])]
        account_days = [date.fromisoformat(row["date"]) for row in data.get("account_daily", [])]
        # Account days extend history before the first per-product day. Overlap stays one day.
        series = sorted(set(child_days) | set(account_days))
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
        if ranges:
            data["sources"].setdefault("sales", _source("Detail Page Sales and Traffic", available=False))
            data["sources"]["sales"]["reconciliation"] = _reconcile(data.get("daily", []), ranges, warnings)
        _mark_provisional(data)
    if shipments is not None:
        parsed = []
        for text in shipments:
            parsed.extend(parse_shipments(text, warnings))
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
    if actions is not None:
        parsed = []
        for text in actions:
            parsed.extend(parse_actions(text, warnings))
        data["actions"] = parsed
    if returns is not None:
        parsed = []
        for text in returns:
            parsed.extend(parse_returns(text))
        data["returns"] = _upsert([], parsed, lambda row: (row["date"], row["sku"], row.get("reason") or "", row.get("units")))
        days = [date.fromisoformat(row["date"]) for row in data["returns"]]
        data["sources"]["returns"] = _source(
            "FBA customer returns", _end_of_day(max(days)) if days else None, available=bool(days))
    for source in data.get("sources", {}).values():
        if source.get("as_of"):
            source["as_of"] = _not_after(source["as_of"], generated_at)
        if source.get("available") is False or source.get("as_of") in ("", None):
            source.pop("as_of", None)
    _rebuild_catalog(data)
    # Drop builder-only fields if a previous file carried them.
    for row in data.get("daily", []):
        row.pop("cents", None)
        row.pop("name", None)
    validate_snapshot(data)
    unique = []
    for warning in warnings:
        if warning not in unique:
            unique.append(warning)
    return data, unique


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


_SALES_SUFFIXES = {".csv", ".tsv", ".txt", ".xls", ".html", ".htm"}


def parse_sales_arg(value):
    text = str(value)
    head, _, path = text.partition("=")
    if path and ".." in head:
        start_text, _, end_text = head.partition("..")
        try:
            start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        except ValueError:
            raise SnapshotError("Sales range must be YYYY-MM-DD..YYYY-MM-DD=file.csv") from None
        if end < start:
            raise SnapshotError("Sales range end is before the start")
        return "range", start, end, path
    if path and len(head) == 10 and head.count("-") == 2:
        try:
            day = date.fromisoformat(head)
        except ValueError:
            raise SnapshotError("Sales date prefix must be YYYY-MM-DD=file.csv") from None
        return "day", day, None, path
    return "path", None, None, text


def _sales_inputs(values):
    specs = []
    for value in values:
        kind, start, end, path = parse_sales_arg(value)
        file = Path(path)
        if kind == "path" and file.is_dir():
            names = sorted(
                item for item in file.iterdir()
                if item.is_file() and not item.name.startswith(".") and item.suffix.lower() in _SALES_SUFFIXES)
            if not names:
                raise SnapshotError(f"No sales files in {file}")
            specs.extend(_sales_inputs([str(item) for item in names]))
            continue
        if kind == "path":
            found = dates_in_name(file.name)
            if len(found) >= 2:
                kind, start, end = "range", found[0], found[-1]
            elif len(found) == 1:
                kind, start = "day", found[0]
        text = read_export(file)
        if kind == "range":
            specs.append((start, text, end, file.name))
        else:
            specs.append((start, text, None, file.name))
    return specs


def run_build(args):
    mapping = load_mapping(args.mapping)
    previous = None
    if args.previous:
        try:
            previous = json.loads(Path(args.previous).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            raise SnapshotError("Previous snapshot could not be read") from None
    sales = _sales_inputs(args.sales) if args.sales else None
    data, warnings = build_snapshot(
        sales=sales,
        inventory=[read_export(path) for path in args.inventory] if args.inventory else None,
        shipments=[read_export(path) for path in args.shipments] if args.shipments else None,
        warehouse=[read_export(path) for path in args.warehouse] if args.warehouse else None,
        returns=[read_export(path) for path in args.returns] if args.returns else None,
        actions=[read_export(path) for path in args.actions] if args.actions else None,
        mapping=mapping,
        previous=previous,
    )
    write_snapshot(args.output, data)
    print(
        f"Wrote {args.output} · {len(data['daily'])} daily rows · {len(data['inventory'])} FBA SKUs · "
        f"{len(data['warehouse'])} 3PL rows · {len(data['shipments'])} shipments · {len(data.get('actions', []))} attention items")
    for warning in warnings:
        print("Warning: " + warning, file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_snapshot", description="Build a daily Amazon Seller snapshot from exports")
    parser.add_argument("--sales", action="append", default=[], metavar="FILE",
                        help="One-day Child Item file (YYYY-MM-DD=file or sales_by_child_item_YYYY-MM-DD.csv), a folder of those files, a By Date file, or START..END=file for a range check")
    parser.add_argument("--inventory", action="append", default=[], metavar="FILE", help="FBA Manage Inventory, Restock, or Inventory Health")
    parser.add_argument("--shipments", action="append", default=[], metavar="FILE", help="FBA shipments list")
    parser.add_argument("--warehouse", action="append", default=[], metavar="FILE", help="Smart Warehousing CSV, JSON, or HTML .xls (quantities are cases)")
    parser.add_argument("--returns", action="append", default=[], metavar="FILE", help="Optional FBA customer returns CSV")
    parser.add_argument("--actions", action="append", default=[], metavar="FILE",
                        help="Optional morning JSON or CSV: type, title, detail, date, url, needs_response_by")
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
