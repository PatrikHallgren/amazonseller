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
from seller.core import DEFAULTS, PACIFIC, Store, case_plan, detect_alerts, iso, snapshot
from seller.fetch import github_fetch, pull_snapshot, save_github_token
from seller.ingest import ingest_snapshot
from seller.reports import parse_inventory, parse_returns, parse_sales, parse_shipments, parse_warehouse, load_mapping
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

    def test_undated_sales_need_a_day(self):
        with self.assertRaises(SnapshotError):
            parse_sales(CHILD_HEADER + "\n" + CHILD_ROW + "\n")

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
        self.assertEqual(warnings, [])
        by_sku = {row["sku"]: row for row in rows}
        self.assertEqual(by_sku["PLX-20-BX"]["units_on_hand"], 300)
        self.assertEqual(by_sku["PLX-20-BX"]["product_sku"], "POTLIFTER")
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
        first, warnings = build_snapshot(
            sales=[(date(2026, 10, 20), child_csv())],
            inventory=[MANAGE, RESTOCK, HEALTH],
            shipments=[SHIPMENTS],
            warehouse=[WAREHOUSE],
            returns=[RETURNS])
        self.assertEqual(warnings, [])
        validate_snapshot(first)
        self.assertNotIn("DO NOT STORE", json.dumps(first))
        self.assertEqual(first["daily"][0]["date"], "2026-10-20")
        self.assertEqual(first["daily"][0]["units"], 5)
        pot = next(row for row in first["inventory"] if row["sku"] == "POTLIFTER")
        self.assertEqual(pot["available"], 71)  # restock file was applied after Manage Inventory
        self.assertEqual(pot["restock"]["quantity"], 80)
        self.assertEqual(first["warehouse"][0]["units_on_hand"], 300)
        second, _ = build_snapshot(
            sales=[(date(2026, 10, 21), child_csv(units=7, orders=6, sales="$200.00"))],
            inventory=[MANAGE],
            previous=first)
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
