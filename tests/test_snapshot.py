"""Snapshot schema, Seller Central / 3PL parsers, and stale-data handling.

Every figure in this file is synthetic.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from seller.app import SETUP_INTRO
from seller.build import build_snapshot, main as build_main
from seller.core import DEFAULTS, PACIFIC, Store, case_plan, detect_alerts, iso, snapshot, summarize
from seller.fetch import github_fetch, pull_snapshot, save_github_token
from seller.ingest import ingest_snapshot
from seller.reports import parse_inventory, parse_returns, parse_sales, parse_shipments, parse_warehouse, load_mapping, read_export
from seller.schema import SnapshotError, validate_snapshot

CHILD_HEADER = (
    "(Parent) ASIN,(Child) ASIN,Title,Sessions - Total,Sessions - Total - B2B,"
    "Session Percentage - Total,Session Percentage - Total - B2B,Page Views - Total,"
    "Page Views - Total - B2B,Page Views Percentage - Total,Page Views Percentage - Total - B2B,"
    "Featured Offer (Buy Box) Percentage,Featured Offer (Buy Box) Percentage - B2B,"
    "Units Ordered,Units Ordered - B2B,Unit Session Percentage,Unit Session Percentage - B2B,"
    "Ordered Product Sales,Ordered Product Sales - B2B,Total Order Items,Total Order Items - B2B"
)
CHILD_ROW = 'B0PARENT01,B0EXAMPLE01,PotLifter,10,1,50%,5%,20,2,40%,4%,95%,90%,5,2,50%,10%,"$129.95","$40.00",4,1'
BY_DATE = """Date,Ordered Product Sales,Ordered Product Sales - B2B,Units Ordered,Units Ordered - B2B,Total Order Items,Total Order Items - B2B,Average Sales per Order Item,Average Selling Price,Sessions - Total
2026-10-20,"$200.00","$40.00",8,1,6,1,$33.33,$25.00,40
"""
DATED_CHILD = """Date,(Parent) ASIN,(Child) ASIN,Title,Units Ordered,Ordered Product Sales,Total Order Items
2026-10-19,B0PARENT01,B0EXAMPLE01,PotLifter,3,$59.98,2
"""
MANAGE = (
    "sku\tfnsku\tasin\tproduct-name\tcondition\tyour-price\tmfn-listing-exists\tmfn-fulfillable-quantity\t"
    "afn-listing-exists\tafn-warehouse-quantity\tafn-fulfillable-quantity\tafn-unsellable-quantity\t"
    "afn-reserved-quantity\tafn-total-quantity\tper-unit-volume\tafn-inbound-working-quantity\t"
    "afn-inbound-shipped-quantity\tafn-inbound-receiving-quantity\tafn-researching-quantity\t"
    "afn-reserved-future-supply\tafn-future-supply-buyable\n"
    "POTLIFTER\tX001\tB0EXAMPLE01\tPotLifter\tNew\t29.99\tNo\t0\tYes\t80\t70\t2\t5\t80\t0.1\t0\t40\t4\t0\t0\t0\n"
    "USED-1\tX009\tB0EXAMPLE09\tUsed unit\tUsed\t9.00\tNo\t0\tYes\t1\t1\t0\t0\t1\t0.1\t0\t0\t0\t0\t0\t0\n"
)
RESTOCK = (
    "Country\tProduct Name\tFNSKU\tMerchant SKU\tASIN\tCondition\tSupplier\tSupplier part no.\tCurrency code\tPrice\t"
    "Sales last 30 days\tUnits Sold Last 30 Days\tTotal Units\tInbound\tAvailable\tFC transfer\tFC Processing\t"
    "Customer Order\tUnfulfillable\tWorking\tShipped\tReceiving\tFulfilled by\t"
    "Total Days of Supply (including units from open shipments)\tDays of Supply at Amazon Fulfillment Network\t"
    "Alert\tRecommended replenishment qty\tRecommended ship date\n"
    "US\tPotLifter\tX001\tPOTLIFTER\tB0EXAMPLE01\tNew\t\t\tUSD\t29.99\t500\t20\t120\t44\t71\t1\t2\t3\t2\t0\t40\t4\t"
    "Amazon\t25\t18\t\t80\t10/24/2026\n"
)
HEALTH = (
    "snapshot-date,sku,fnsku,asin,product-name,condition,available,days-of-supply,inbound-working,"
    "inbound-shipped,inbound-received,reserved-quantity,unfulfillable-quantity\n"
    "2026-10-20,PROLIFTER,X002,B0EXAMPLE02,ProLifter,New,12,9,1,8,0,1,0\n"
)
SHIPMENTS = (
    "Shipment ID,Shipment Name,Shipment Status,Last Updated,SKU,ASIN,Title,Quantity Shipped,Quantity Received\n"
    "FBA15DEMO01,October PotLifter,IN_TRANSIT,10/18/2026,POTLIFTER,B0EXAMPLE01,PotLifter,40,0\n"
    "FBA15DEMO01,October PotLifter,IN_TRANSIT,10/18/2026,PROLIFTER,B0EXAMPLE02,ProLifter,16,0\n"
)
QUEUE = (
    "Shipment ID,Shipment name,Created,Last updated,Ship to,SKUs,Units expected,Units received,Status\n"
    "FBA15DEMO02,Queue export,10/01/2026,10/19/2026,SMF3,2,160,0,IN_TRANSIT\n"
)
WAREHOUSE = (
    "SKU,Description,Qty on hand,Qty per case,Cases per pallet,Weight,Case UPC\n"
    "PLX-20-BX,PotLifter case,15,20,40,22.5,012345678905\n"
    "PRLX-8-BX,ProLifter case,4,8,50,18,012345678912\n"
)
RETURNS = (
    "return-date,order-id,sku,asin,fnsku,product-name,quantity,fulfillment-center-id,detailed-disposition,"
    "reason,status,license-plate-number,customer-comments\n"
    "2026-10-18T15:00:00+00:00,111-000,POTLIFTER,B0EXAMPLE01,X001,PotLifter,1,SMF3,SELLABLE,Defective,"
    "Unit returned to inventory,LPN1,DO NOT STORE\n"
)


def child_csv(units=5, orders=4, sales="$129.95", asin="B0EXAMPLE01", title="PotLifter"):
    return CHILD_HEADER + f"\nB0PARENT01,{asin},{title},10,1,50%,5%,20,2,40%,4%,95%,90%,{units},2,50%,10%,{sales},$1.00,{orders},1\n"


class ParserTests(unittest.TestCase):
    def test_child_item_headers_ignore_b2b_subset(self):
        rows, kind = parse_sales("Business Report\n\n" + CHILD_HEADER + "\n" + CHILD_ROW + "\n", date(2026, 10, 20))
        self.assertEqual(kind, "child")
        self.assertEqual(rows[0]["date"], date(2026, 10, 20))
        self.assertEqual(rows[0]["asin"], "B0EXAMPLE01")
        self.assertEqual((rows[0]["units"], rows[0]["orders"], rows[0]["sales"]), (5, 4, 129.95))

    def test_dated_child_and_by_date(self):
        rows, kind = parse_sales(DATED_CHILD)
        self.assertEqual((kind, rows[0]["date"].isoformat(), rows[0]["units"]), ("child", "2026-10-19", 3))
        account, account_kind = parse_sales(BY_DATE)
        self.assertEqual(account_kind, "account")
        self.assertEqual((account[0]["units"], account[0]["orders"], account[0]["sales"]), (8, 6, 200.0))

    def test_undated_child_is_a_range_total(self):
        rows, kind = parse_sales(CHILD_HEADER + "\n" + CHILD_ROW + "\n")
        self.assertEqual(kind, "range")
        self.assertNotIn("date", {key for row in rows for key in row if row[key]})
        data, warnings = build_snapshot(sales=[(None, CHILD_HEADER + "\n" + CHILD_ROW + "\n")])
        self.assertEqual(data["daily"], [])
        self.assertTrue(any("not stored as daily" in note for note in warnings))

    def test_manage_inventory_and_restock_headers(self):
        manage = parse_inventory(MANAGE)
        self.assertEqual([row["sku"] for row in manage], ["POTLIFTER"])
        self.assertEqual((manage[0]["available"], manage[0]["unfulfillable"], manage[0]["inbound_shipped"], manage[0]["inbound_receiving"]), (70, 2, 40, 4))
        restock = parse_inventory(RESTOCK)
        self.assertEqual(restock[0]["available"], 71)
        self.assertEqual(restock[0]["days_of_supply"], 25)
        self.assertEqual(restock[0]["inbound_working"], 0)
        self.assertEqual(restock[0]["inbound_shipped"], 40)
        self.assertEqual(restock[0]["restock"], {"quantity": 80, "ship_date": "2026-10-24"})
        health = parse_inventory(HEALTH)
        self.assertEqual(health[0]["snapshot_date"], date(2026, 10, 20))
        self.assertEqual(health[0]["days_of_supply"], 9)
        self.assertEqual(health[0]["inbound_receiving"], 0)

    def test_quantity_on_hand_alias_and_currency(self):
        rows, _ = parse_warehouse(
            "Item,Item Description,Quantity on hand,Units per case\nPLX-20-BX,PotLifter,3,10\n",
            load_mapping())
        self.assertEqual(rows[0]["cases_on_hand"], 3)
        self.assertEqual(rows[0]["units_per_case"], 20)  # mapping wins over the column's 10
        self.assertEqual(rows[0]["units_on_hand"], 60)

    def test_shipments_item_level_and_queue(self):
        shipments = parse_shipments(SHIPMENTS)
        self.assertEqual(shipments[0]["id"], "FBA15DEMO01")
        self.assertEqual(shipments[0]["status"], "IN_TRANSIT")
        self.assertEqual(len(shipments[0]["items"]), 2)
        self.assertEqual(shipments[0]["items"][0]["shipped"], 40)
        queue = parse_shipments(QUEUE)
        self.assertEqual(queue[0]["items"], [])
        self.assertEqual(queue[0]["status"], "IN_TRANSIT")
        self.assertNotIn("sku", queue[0])

    def test_warehouse_cases_and_json(self):
        rows, warnings = parse_warehouse(WAREHOUSE, load_mapping())
        self.assertTrue(any("PRLX-8-BX" in note and "856455001068" in note for note in warnings))
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["PLX-20-BX"]["units_on_hand"], 300)
        self.assertEqual(by_sku["PLX-20-BX"]["product_sku"], "856455001068")
        self.assertEqual(by_sku["PRLX-8-BX"]["units_on_hand"], 32)
        self.assertEqual(by_sku["PRLX-8-BX"]["product_name"], "ProLifter")
        parsed, _ = parse_warehouse('[{"sku":"PLX-20-BX","description":"PotLifter","qty_on_hand":2}]', load_mapping())
        self.assertEqual(parsed[0]["units_on_hand"], 40)

    def test_mapping_override_and_unmapped_cases(self):
        mapping = load_mapping()
        mapping["PLX-20-BX"] = {"sku": "REAL-POT", "name": "PotLifter", "units_per_case": 24}
        rows, _ = parse_warehouse("SKU,Qty on hand\nPLX-20-BX,2\nUNKNOWN-1,5\n", mapping)
        self.assertEqual(rows[0]["product_sku"], "REAL-POT")
        self.assertEqual(rows[0]["units_on_hand"], 48)
        self.assertIsNone(rows[1]["units_on_hand"])
        self.assertFalse(rows[1]["mapped"])
        self.assertEqual(rows[1]["cases_on_hand"], 5)

    def test_returns_drop_customer_comments(self):
        rows = parse_returns(RETURNS)
        self.assertEqual(rows, [{
            "date": "2026-10-18", "sku": "POTLIFTER", "asin": "B0EXAMPLE01", "units": 1, "reason": "Defective"}])
        self.assertNotIn("DO NOT STORE", json.dumps(rows))


class BuildTests(unittest.TestCase):
    def test_merge_keeps_history_and_replaces_stock(self):
        mapping = {
            "PLX-20-BX": {"sku": "POTLIFTER", "name": "PotLifter", "units_per_case": 20, "asin": "B0EXAMPLE01"},
            "PRLX-8-BX": {"sku": "PROLIFTER", "name": "ProLifter", "units_per_case": 8, "asin": "B0EXAMPLE02"},
        }
        first, warnings = build_snapshot(
            sales=[(date(2026, 10, 20), child_csv())],
            inventory=[MANAGE, RESTOCK, HEALTH],
            shipments=[SHIPMENTS],
            warehouse=[WAREHOUSE],
            returns=[RETURNS],
            mapping=mapping)
        self.assertEqual(warnings, [])
        validate_snapshot(first)
        self.assertNotIn("DO NOT STORE", json.dumps(first))
        self.assertEqual(first["daily"][0]["date"], "2026-10-20")
        self.assertEqual(first["daily"][0]["units"], 5)
        pot = next(row for row in first["inventory"] if row["sku"] == "POTLIFTER")
        self.assertEqual(pot["available"], 70)  # Manage FBA Inventory fulfillable wins over the later Restock Available
        self.assertEqual(pot["restock"]["quantity"], 80)
        self.assertEqual(first["warehouse"][0]["units_on_hand"], 300)
        second, _ = build_snapshot(
            sales=[(date(2026, 10, 21), child_csv(units=7, orders=6, sales="$200.00"))],
            inventory=[MANAGE],
            previous=first,
            mapping=mapping)
        days = [row["date"] for row in second["daily"]]
        self.assertEqual(days, ["2026-10-20", "2026-10-21"])
        self.assertEqual(second["daily"][0]["units"], 5)
        self.assertEqual(second["daily"][1]["units"], 7)
        replaced = next(row for row in second["inventory"] if row["sku"] == "POTLIFTER")
        self.assertEqual(replaced["available"], 70)
        self.assertEqual(len(second["warehouse"]), 2)
        self.assertTrue(second["sources"]["sales"]["complete"])
        self.assertIn("T", second["sources"]["sales"]["as_of"])

    def test_schema_rejects_bad_documents(self):
        good, _ = build_snapshot(sales=[(date(2026, 10, 20), child_csv())])
        validate_snapshot(good)
        for broken in (
            {"schema_version": 2, "generated_at": good["generated_at"]},
            {"schema_version": 1},
            {"schema_version": 1, "generated_at": "2026-10-21"},
            {"schema_version": 1, "generated_at": good["generated_at"], "daily": [{"date": "2026-10-20", "sku": "A", "units": "five"}]},
        ):
            with self.assertRaises(SnapshotError):
                validate_snapshot(broken)

    def test_cli_writes_owner_only_file(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        sales = root / "child.csv"
        sales.write_text(child_csv(), encoding="utf-8")
        output = root / "out" / "snapshot.json"
        build_main(["--sales", f"2026-10-20={sales}", "--output", str(output)])
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        data = json.loads(output.read_text())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["daily"][0]["sku"], "B0EXAMPLE01")  # ASIN until inventory links it


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "seller.sqlite3")
        self.at = datetime(2026, 10, 21, 18, tzinfo=PACIFIC)
        self.config = DEFAULTS | {"selected": None}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def document(self, when, available=12, warehouse_units=60):
        days = []
        for offset in range(20):
            day = date(2026, 10, 1) + timedelta(days=offset)
            days.append({"date": day.isoformat(), "sku": "POTLIFTER", "units": 2, "orders": 1, "sales": 20})
        cases = warehouse_units // 20
        return {
            "schema_version": 1,
            "generated_at": iso(when),
            "sources": {
                "sales": {"as_of": iso(when), "available": True, "start": "2026-10-01", "end": "2026-10-20", "complete": True},
                "inventory": {"as_of": iso(when), "available": True},
                "warehouse": {"as_of": iso(when), "available": True},
                "shipments": {"as_of": iso(when), "available": True},
                "returns": {"available": False},
            },
            "catalog": [{"sku": "POTLIFTER", "name": "PotLifter", "asin": "B0EXAMPLE01", "units_per_case": 20, "warehouse_sku": "PLX-20-BX"}],
            "daily": days,
            "inventory": [{
                "sku": "POTLIFTER", "asin": "B0EXAMPLE01", "name": "PotLifter", "available": available,
                "reserved": 1, "unfulfillable": 0, "inbound_working": 999, "inbound_shipped": 40,
                "inbound_receiving": 0, "days_of_supply": 10, "restock": {"quantity": 80, "ship_date": "2026-10-24"},
            }],
            "shipments": [{"id": "FBA15DEMO01", "name": "October", "status": "IN_TRANSIT", "items": [{"sku": "POTLIFTER", "shipped": 40, "received": 0}]}],
            "warehouse": [{"sku": "PLX-20-BX", "product_sku": "POTLIFTER", "cases_on_hand": cases, "units_per_case": 20, "units_on_hand": warehouse_units}],
            "returns": [],
        }

    def test_case_rounding_us_stock_and_factory_alert(self):
        self.assertEqual(case_plan(78, 20, None), (80, 4))
        self.assertEqual(case_plan(78, 20, 50), (40, 2))
        self.assertEqual(case_plan(9, 20, 100), (20, 1))
        self.assertEqual(case_plan(0, 20, 100), (0, 0))
        self.assertEqual(case_plan(78, None, None), (78, None))
        ingest_snapshot(self.store, self.document(self.at - timedelta(hours=2)))
        view = snapshot(self.store, self.config, self.at)
        product = next(row for row in view["products"] if row["sku"] == "POTLIFTER")
        # 20 days × 2 units → demand 2. Target 148. Raw suggestion 148 - 12 - 40 = 96 → 5 cases, capped at 3.
        self.assertEqual(product["daily_demand"], 2)
        self.assertEqual(product["suggested"], 96)
        self.assertEqual((product["suggested_cases"], product["suggested_units"]), (3, 60))
        self.assertEqual(product["total_us"], 112)  # 12 + 40 shipped + 60 at the 3PL; working 999 is excluded
        self.assertEqual(product["weeks_cover"], 8)
        self.assertEqual(product["warehouse_cases"], 3)
        self.assertFalse(view["stale"])
        detect_alerts(self.store, view, self.at)
        alerts = {row["id"]: row["severity"] for row in self.store.db.execute("SELECT id, severity FROM alerts WHERE active=1")}
        self.assertEqual(alerts["factory:POTLIFTER"], "urgent")
        self.assertIn("Shenzhen", json.loads(self.store.db.execute("SELECT data FROM alerts WHERE id='factory:POTLIFTER'").fetchone()[0])["detail"])

    def test_factory_warning_between_half_and_full_lead(self):
        # 30 units over 30 days → demand 1. US stock 150 covers 150 days. Lead 180, half 90.
        days = [{"date": (date(2026, 9, 21) + timedelta(days=offset)).isoformat(), "sku": "POTLIFTER", "units": 1, "orders": 1, "sales": 10} for offset in range(30)]
        document = self.document(self.at - timedelta(hours=1), available=100, warehouse_units=50)
        document["daily"] = days
        document["sources"]["sales"].update(start=days[0]["date"], end=days[-1]["date"])
        document["inventory"][0]["available"] = 100
        document["inventory"][0]["inbound_shipped"] = 0
        document["warehouse"][0].update(cases_on_hand=5, units_on_hand=50)
        ingest_snapshot(self.store, document)
        view = snapshot(self.store, self.config, self.at)
        product = view["products"][0]
        self.assertEqual(product["total_us"], 150)
        self.assertEqual(product["factory_cover_days"], 150)
        detect_alerts(self.store, view, self.at)
        severity = self.store.db.execute("SELECT severity FROM alerts WHERE id='factory:POTLIFTER'").fetchone()[0]
        self.assertEqual(severity, "warning")

    def test_stale_after_36_hours_and_fresh_inside_the_window(self):
        ingest_snapshot(self.store, self.document(self.at - timedelta(hours=36, seconds=1)))
        self.assertTrue(snapshot(self.store, self.config, self.at)["stale"])
        ingest_snapshot(self.store, self.document(self.at - timedelta(hours=36)))
        self.assertFalse(snapshot(self.store, self.config, self.at)["stale"])
        ingest_snapshot(self.store, self.document(self.at - timedelta(hours=2)))
        self.assertFalse(snapshot(self.store, self.config, self.at)["stale"])

    def test_failed_refresh_keeps_stock_and_does_not_mark_fresh_data_stale(self):
        ingest_snapshot(self.store, self.document(self.at - timedelta(hours=2), available=40))
        with patch("seller.fetch.load_snapshot_bytes", side_effect=SnapshotError("GitHub unreachable; showing the last saved data")):
            self.assertFalse(pull_snapshot(self.store, self.config, Path(self.tmp.name), force=True))
        self.assertEqual(self.store.get("product", "POTLIFTER")["available"], 40)
        self.assertIn("unreachable", self.store.get("source", "snapshot")["error"])
        view = snapshot(self.store, self.config, self.at)
        self.assertEqual(view["products"][0]["available"], 40)
        self.assertFalse(view["stale"])
        self.assertIn("unreachable", view["source_error"])

    def test_local_file_source(self):
        path = Path(self.tmp.name) / "drop.json"
        path.write_text(json.dumps(self.document(self.at - timedelta(hours=1), available=33)))
        config = self.config | {"source": "file", "snapshot_file": str(path)}
        with patch("seller.fetch.gh_token") as token, patch("seller.fetch.github_fetch") as remote:
            self.assertTrue(pull_snapshot(self.store, config, Path(self.tmp.name), force=True))
            token.assert_not_called()
            remote.assert_not_called()
        self.assertEqual(self.store.get("product", "POTLIFTER")["available"], 33)

    def test_github_token_and_gh_fallback(self):
        body = json.dumps(self.document(self.at - timedelta(hours=1), available=21)).encode()

        class Response:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        seen = {}

        def opener(request, timeout=0):
            seen["header"] = request.get_header("Authorization")
            seen["agent"] = request.get_header("User-agent")
            return Response()

        config_dir = Path(self.tmp.name) / "config"
        config_dir.mkdir()
        save_github_token(config_dir, "secret-token")
        with patch("urllib.request.urlopen", side_effect=opener):
            self.assertTrue(pull_snapshot(self.store, self.config, config_dir, force=True))
        self.assertEqual(seen["header"], "Bearer secret-token")
        self.assertIn("AmazonSellerOmarchy", seen["agent"])
        self.assertEqual(self.store.get("product", "POTLIFTER")["available"], 21)
        self.assertNotIn("secret-token", json.dumps(self.store.get("source", "snapshot")))

        def rejected(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

        with patch("urllib.request.urlopen", side_effect=rejected):
            pull_snapshot(self.store, self.config, config_dir, force=True)
        self.assertNotIn("secret-token", self.store.get("source", "snapshot")["error"])
        self.assertEqual(self.store.get("product", "POTLIFTER")["available"], 21)

        (config_dir / "credentials.json").unlink()
        def opener_gh(request, timeout=0):
            seen["gh"] = request.get_header("Authorization")
            return Response()
        with patch("seller.fetch.gh_token", return_value="gh-token-value"), patch("urllib.request.urlopen", side_effect=opener_gh):
            self.assertTrue(pull_snapshot(self.store, self.config, config_dir, force=True))
        self.assertEqual(seen["gh"], "Bearer gh-token-value")

    def test_loose_credentials_are_refused(self):
        config_dir = Path(self.tmp.name) / "config"
        config_dir.mkdir()
        file = config_dir / "credentials.json"
        file.write_text('{"github_token":"secret-token"}')
        os.chmod(file, 0o644)
        with patch("urllib.request.urlopen") as remote:
            pull_snapshot(self.store, self.config, config_dir, force=True)
            remote.assert_not_called()
        self.assertIn("0600", self.store.get("source", "snapshot")["error"])
        self.assertNotIn("secret-token", self.store.get("source", "snapshot")["error"])

    def test_saved_token_is_owner_only_even_with_open_umask(self):
        previous = os.umask(0)
        try:
            save_github_token(self.tmp.name, "abc")
            mode = (Path(self.tmp.name) / "credentials.json").stat().st_mode & 0o777
        finally:
            os.umask(previous)
        self.assertEqual(mode, 0o600)

    def test_no_factory_alert_without_warehouse_quantities(self):
        self.store.product("A", name="A", active=True, fba=True, available=5)
        view = snapshot(self.store, DEFAULTS | {"selected": ["A"]}, self.at)
        detect_alerts(self.store, view, self.at)
        self.assertIsNone(self.store.db.execute("SELECT id FROM alerts WHERE id='factory:A'").fetchone())


MANAGE_AVAILABLE = (
    "sku\tasin\tproduct-name\tcondition\tafn-fulfillable-quantity\n"
    "856455001068\tB002ECFFJ8\tPotLifter\tNew\t70\n"
)
RESTOCK_TRANSFER = (
    "Merchant SKU\tASIN\tCondition\tAvailable\tFC transfer\tFC Processing\tCustomer Order\tUnfulfillable\t"
    "Working\tShipped\tReceiving\tRecommended replenishment qty\tRecommended ship date\n"
    "856455001068\tB002ECFFJ8\tNew\t71\t23\t2\t3\t1\t0\t40\t4\t80\tnone\n"
)
MANAGE_SHIPMENTS = (
    "Shipment name,Status,Created,Last updated,Destination,Delivery window,SKUs,Units expected,Units located,Shipment ID\n"
    "October PotLifter,IN_TRANSIT,10/01/2026,10/21/2026,SMF3,10/20/2026 - 10/24/2026,2,160,40,FBA15DEMO09\n"
)
BY_DATE_PARTIAL = """Date,Ordered Product Sales,Units Ordered,Total Order Items
2026-10-19,"$40.00",4,3
2026-10-20,"$80.00",8,6
"""
WAREHOUSE_XLS = """<html><body><table>
<tr><td>Smart Warehousing</td></tr>
<tr><th>SKU</th><th>Description</th><th>Qty on hand</th><th>Qty per case</th><th>Cases per pallet</th><th>Weight</th><th>Case UPC</th></tr>
<tr><td>PLX-20-BX</td><td>PotLifter case</td><td>15</td><td>1</td><td>40</td><td>22.5</td><td>012345678905</td></tr>
</table></body></html>
"""


class ExportTests(unittest.TestCase):
    def test_restock_none_ship_date_does_not_abort(self):
        data, warnings = build_snapshot(inventory=[MANAGE_AVAILABLE, RESTOCK_TRANSFER])
        row = data["inventory"][0]
        self.assertEqual(row["sku"], "856455001068")
        self.assertEqual(row["available"], 70)
        self.assertEqual(row["fc_transfer"], 23)
        self.assertEqual(row["inbound_shipped"], 40)
        self.assertEqual(row["inbound_receiving"], 4)
        self.assertEqual(row["restock"]["quantity"], 80)
        self.assertEqual(row["restock"]["ship_date"], "")
        self.assertTrue(any("none" in note.lower() for note in warnings))
        garbage = RESTOCK_TRANSFER.replace("none", "not-a-date")
        again, notes = build_snapshot(inventory=[garbage])
        self.assertEqual(again["inventory"][0]["available"], 71)
        self.assertTrue(any("could not be read" in note for note in notes))

    def test_restock_transfer_survives_when_manage_is_later(self):
        data, _ = build_snapshot(inventory=[RESTOCK_TRANSFER, MANAGE_AVAILABLE])
        row = data["inventory"][0]
        self.assertEqual(row["available"], 70)
        self.assertEqual(row["fc_transfer"], 23)
        self.assertEqual(row["inbound_shipped"], 40)

    def test_one_day_filenames_folder_and_range_check(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        days = root / "days"
        days.mkdir()
        for day, units in (("2026-10-19", 3), ("2026-10-20", 5)):
            (days / f"sales_by_child_item_{day}.csv").write_text(child_csv(units=units), encoding="utf-8")
        span = root / "sales_by_child_item_2026-10-19_2026-10-20.csv"
        span.write_text(child_csv(units=9), encoding="utf-8")
        output = root / "snapshot.json"
        build_main(["--sales", str(days), "--sales", str(span), "--output", str(output)])
        data = json.loads(output.read_text())
        self.assertEqual([(row["date"], row["units"]) for row in data["daily"]], [("2026-10-19", 3), ("2026-10-20", 5)])
        self.assertEqual(data["sources"]["sales"]["reconciliation"][0]["rows"][0]["units"], 9)
        self.assertFalse(data["sources"]["sales"]["reconciliation"][0]["rows"][0]["match"])
        checked = root / "checked.json"
        build_main(["--sales", str(days), "--sales", f"2026-10-19..2026-10-20={span}", "--output", str(checked)])
        match = json.loads(checked.read_text())["sources"]["sales"]["reconciliation"][0]["rows"][0]
        self.assertEqual(match["daily_units"], 8)
        self.assertFalse(match["match"])

    def test_manage_shipments_keep_units_without_sku_ids(self):
        generated = iso(datetime(2026, 10, 21, 18, tzinfo=PACIFIC))
        data, _ = build_snapshot(shipments=[MANAGE_SHIPMENTS], generated_at=generated)
        shipment = data["shipments"][0]
        self.assertEqual(shipment["items"], [])
        self.assertEqual((shipment["units_expected"], shipment["units_located"]), (160, 40))
        self.assertEqual(shipment["destination"], "SMF3")
        self.assertNotIn("sku", shipment)
        self.assertLessEqual(data["sources"]["shipments"]["as_of"], generated)

    def test_html_xls_uses_configured_case_size_and_binary_xls_is_rejected(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        html = Path(folder.name) / "sumner.xls"
        html.write_text(WAREHOUSE_XLS, encoding="utf-8")
        rows, warnings = parse_warehouse(read_export(html), load_mapping())
        self.assertEqual(rows[0]["units_per_case"], 20)
        self.assertEqual(rows[0]["units_on_hand"], 300)
        self.assertTrue(any("Qty per case is 1" in note for note in warnings))
        binary = Path(folder.name) / "binary.xls"
        binary.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
        with self.assertRaises(SnapshotError) as caught:
            read_export(binary)
        self.assertIn("CSV", str(caught.exception))
        self.assertNotIn("Traceback", str(caught.exception))

    def test_latest_by_date_day_is_provisional_until_a_later_day_arrives(self):
        child = """Date,(Child) ASIN,Title,Units Ordered,Ordered Product Sales,Total Order Items
2026-10-19,B0EXAMPLE01,PotLifter,4,$40.00,3
2026-10-20,B0EXAMPLE01,PotLifter,8,$80.00,6
"""
        first, _ = build_snapshot(sales=[(None, child), (None, BY_DATE_PARTIAL)])
        flags = {(row["date"], row.get("provisional", False)) for row in first["daily"]}
        self.assertIn(("2026-10-20", True), flags)
        self.assertIn(("2026-10-19", False), flags)
        later = BY_DATE_PARTIAL + '2026-10-21,"$10.00",1,1\n'
        second, _ = build_snapshot(sales=[(None, later)], previous=first)
        by_day = {row["date"]: row.get("provisional", False) for row in second["account_daily"]}
        self.assertFalse(by_day["2026-10-20"])
        self.assertTrue(by_day["2026-10-21"])
        daily_flags = {row["date"]: row.get("provisional", False) for row in second["daily"]}
        self.assertFalse(daily_flags["2026-10-20"])

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "seller.sqlite3")
        self.addCleanup(store.db.close)
        at = datetime(2026, 10, 21, 18, tzinfo=PACIFIC)
        ingest_snapshot(store, first, at)
        view = snapshot(store, DEFAULTS | {"selected": None}, at)
        product = view["products"][0]
        complete = {row["date"]: row["complete"] for row in product["trend_days"]}
        self.assertFalse(complete["2026-10-20"])
        self.assertTrue(complete["2026-10-19"])
        day = summarize(store, {product["sku"]}, datetime(2026, 10, 20, tzinfo=PACIFIC), datetime(2026, 10, 21, tzinfo=PACIFIC))
        earlier = summarize(store, {product["sku"]}, datetime(2026, 10, 19, tzinfo=PACIFIC), datetime(2026, 10, 20, tzinfo=PACIFIC))
        self.assertTrue(day["provisional"])
        self.assertTrue(day["sales_complete"])
        self.assertFalse(earlier["provisional"])
        self.assertTrue(earlier["sales_complete"])
        self.assertEqual(day["units"], 8)

    def test_account_totals_fill_days_without_per_product_rows(self):
        at = datetime(2026, 10, 21, 18, tzinfo=PACIFIC)
        end = date(2026, 10, 20)
        account_days = [end - timedelta(days=offset) for offset in range(89, -1, -1)]
        sku_days = account_days[-10:]
        self.assertEqual(account_days[0], date(2026, 7, 23))
        self.assertEqual(sku_days[0], date(2026, 10, 11))
        account = "Date,Ordered Product Sales,Units Ordered,Total Order Items\n" + "".join(
            f'{day.isoformat()},"$100.00",10,4\n' for day in account_days)
        child = "Date,(Child) ASIN,Title,Units Ordered,Ordered Product Sales,Total Order Items\n" + "".join(
            f"{day.isoformat()},B0EXAMPLE01,PotLifter,2,$20.00,1\n" for day in sku_days)
        data, _ = build_snapshot(sales=[(None, child), (None, account)], generated_at=iso(at))
        self.assertEqual(data["sources"]["sales"]["start"], account_days[0].isoformat())
        self.assertEqual(data["sources"]["sales"]["end"], "2026-10-20")
        self.assertEqual(len(data["daily"]), 10)
        self.assertEqual(len(data["account_daily"]), 90)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "seller.sqlite3")
        self.addCleanup(store.db.close)
        ingest_snapshot(store, data, at)
        view = snapshot(store, DEFAULTS | {"selected": None}, at)
        history = view["history"]
        self.assertEqual(history["start"], account_days[0].isoformat())
        self.assertEqual(history["end"], "2026-10-20")
        self.assertEqual(history["days"], 90)
        product = view["products"][0]
        self.assertEqual(product["sku"], "B0EXAMPLE01")
        self.assertEqual(product["history_days"], 10)
        self.assertEqual(product["daily_demand"], 2.0)

        self.assertNotIn("day", view["periods"])
        month = view["periods"]["month"]
        self.assertEqual(month["span"], ["2026-10-01", "2026-10-20", "2026-09-01", "2026-09-20"])
        self.assertEqual(month["current"]["units"], 120)
        self.assertEqual(month["current"]["scope"], "combined")
        self.assertTrue(month["current"]["sales_complete"])
        self.assertTrue(month["current"]["provisional"])
        self.assertEqual(month["previous"]["units"], 200)
        self.assertEqual(month["previous"]["scope"], "account")
        self.assertTrue(month["previous"]["sales_complete"])
        self.assertIsNone(month["note"])
        week = view["periods"]["week"]
        self.assertEqual(week["span"], ["2026-10-18", "2026-10-20", "2026-10-11", "2026-10-13"])
        self.assertEqual((week["current"]["units"], week["previous"]["units"]), (6, 6))
        self.assertTrue(week["current"]["sales_complete"])
        self.assertTrue(week["previous"]["sales_complete"])
        self.assertIsNone(week["note"])
        year = view["periods"]["year"]
        self.assertEqual(year["current"]["units"], 820)
        self.assertEqual(year["current"]["scope"], "combined")
        self.assertFalse(year["current"]["sales_complete"])
        self.assertEqual(year["note"], "Only 90 days of history; no comparison yet")
        overlap = summarize(
            store, {product["sku"]},
            datetime(2026, 10, 20, tzinfo=PACIFIC), datetime(2026, 10, 21, tzinfo=PACIFIC))
        self.assertEqual(overlap["units"], 2)
        self.assertEqual(overlap["scope"], "sku")

    def test_actions_json_and_csv_and_a_missing_section(self):
        at = datetime(2026, 10, 21, 18, tzinfo=PACIFIC)
        payload = json.dumps({"actions": [
            {"type": "message", "title": "PotLifter question", "detail": "Inbox thread",
             "date": "2026-10-20T20:00:00-07:00",
             "needs_response_by": "2026-10-21T20:00:00-07:00"},
            {"type": "support", "title": "Case 1234", "detail": "Where is the inbound shipment",
             "date": "2026-10-20", "url": "https://sellercentral.amazon.com/cu/case-lobby"},
        ]})
        csv_text = (
            "type,title,detail,date,url,needs_response_by\n"
            "action_required,Image check,Main image,2026-10-19,https://sellercentral.amazon.com/inventory,\n"
        )
        data, _ = build_snapshot(actions=[payload, csv_text], generated_at=iso(at))
        kinds = {row["type"]: row for row in data["actions"]}
        self.assertEqual(set(kinds), {"message", "support", "action_required"})
        self.assertEqual(kinds["message"]["url"], "https://sellercentral.amazon.com/messaging/inbox")
        self.assertTrue(kinds["message"]["id"])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "seller.sqlite3")
        self.addCleanup(store.db.close)
        ingest_snapshot(store, data, at)
        view = snapshot(store, DEFAULTS | {"selected": None}, at)
        detect_alerts(store, view, at)
        store.db.commit()
        view = snapshot(store, DEFAULTS | {"selected": None}, at)
        self.assertEqual(view["buyer_messages"]["count"], 1)
        self.assertEqual(view["buyer_messages"]["target_hours"], 24)
        self.assertGreaterEqual(view["buyer_messages"]["oldest_hours"], 20)
        by_title = {alert["title"]: alert for alert in view["alerts"]}
        self.assertEqual(by_title["Buyer message · PotLifter question"]["severity"], "urgent")
        self.assertEqual(by_title["Support case · Case 1234"]["severity"], "warning")
        self.assertTrue(by_title["Action required · Image check"]["url"].startswith("https://sellercentral.amazon.com/"))
        fresh = json.loads(payload)
        fresh["actions"][0]["date"] = "2026-10-21T16:00:00-07:00"
        fresh["actions"][0]["needs_response_by"] = "2026-10-22T16:00:00-07:00"
        recent, _ = build_snapshot(actions=[json.dumps(fresh)], generated_at=iso(at))
        ingest_snapshot(store, recent, at)
        detect_alerts(store, snapshot(store, DEFAULTS | {"selected": None}, at), at)
        store.db.commit()
        again = snapshot(store, DEFAULTS | {"selected": None}, at)
        message = next(alert for alert in again["alerts"] if alert["title"].startswith("Buyer message"))
        self.assertEqual(message["severity"], "warning")
        omitted = {key: value for key, value in data.items() if key != "actions"}
        validate_snapshot(omitted)
        kept = len(store.all("action"))
        ingest_snapshot(store, omitted, at)
        self.assertEqual(len(store.all("action")), kept)
        self.assertGreater(kept, 0)
        with self.assertRaises(SnapshotError):
            build_snapshot(actions=['[{"type":"message","title":"Nope","url":"https://example.com/inbox"}]'], generated_at=iso(at))

    def test_by_date_history_keeps_two_years(self):
        end = date(2026, 10, 20)
        days = [end - timedelta(days=offset) for offset in range(800, -1, -1)]
        account = "Date,Ordered Product Sales,Units Ordered,Total Order Items\n" + "".join(
            f'{day.isoformat()},"$10.00",1,1\n' for day in days)
        data, warnings = build_snapshot(sales=[(None, account)], generated_at=iso(datetime(2026, 10, 21, 18, tzinfo=PACIFIC)))
        self.assertEqual(len(data["account_daily"]), 731)
        self.assertEqual(data["account_daily"][0]["date"], (end - timedelta(days=730)).isoformat())
        self.assertEqual(data["account_daily"][-1]["date"], "2026-10-20")
        self.assertEqual(data["sources"]["sales"]["start"], data["account_daily"][0]["date"])
        self.assertTrue(any("two years" in note for note in warnings))

    def test_header_stock_ignores_a_product_with_no_3pl_row(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        store = Store(Path(folder.name) / "seller.sqlite3")
        self.addCleanup(store.db.close)
        at = datetime(2026, 10, 21, 18, tzinfo=PACIFIC)
        store.product("POT", name="PotLifter", active=True, fba=True, available=10, inbound_shipped=0, inbound_receiving=0,
                      warehouse_units=40, warehouse_cases=2, fc_transfer=23)
        store.product("NEW", name="Unlaunched", active=True, fba=True, available=0, inbound_shipped=0, inbound_receiving=0,
                      warehouse_units=None)
        view = snapshot(store, DEFAULTS | {"selected": None}, at)
        self.assertEqual(view["stock"]["total_us"], 10 + 23 + 40)
        self.assertEqual(view["stock"]["warehouse_units"], 40)
        self.assertTrue(view["stock"]["warehouse_partial"])
        self.assertEqual(view["stock"]["fba_available"], 10)
        detect_alerts(store, view, at)
        self.assertIsNone(store.db.execute("SELECT id FROM alerts WHERE id='stock:NEW'").fetchone())
        store.order("1", "NEW", iso(at - timedelta(days=2)), iso(at), "SHIPPED", 2, 100)
        detect_alerts(store, snapshot(store, DEFAULTS | {"selected": None}, at), at)
        self.assertIsNotNone(store.db.execute("SELECT id FROM alerts WHERE id='stock:NEW'").fetchone())


class SetupTests(unittest.TestCase):
    def test_setup_intro_does_not_ask_for_lwa(self):
        self.assertNotIn("LWA", SETUP_INTRO)
        self.assertNotIn("client secret", SETUP_INTRO.lower())
        self.assertNotIn("refresh token", SETUP_INTRO.lower())
        self.assertIn("read-only", SETUP_INTRO.lower())

    def test_setup_help_hides_legacy_spapi(self):
        result = subprocess.run([sys.executable, "amazon-seller", "setup", "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("spapi", result.stdout.lower())
        self.assertNotIn("lwa", result.stdout.lower())

    def test_github_fetch_decodes_contents_api_json(self):
        import base64
        raw = b'{"schema_version":1}'
        payload = json.dumps({"encoding": "base64", "content": base64.b64encode(raw).decode()}).encode()

        class Response:
            def read(self):
                return payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with patch("urllib.request.urlopen", return_value=Response()):
            self.assertEqual(github_fetch("PatrikHallgren", "amazonseller-data", "snapshot.json", "main", "token"), raw)


if __name__ == "__main__":
    unittest.main()
