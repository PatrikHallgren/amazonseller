import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from seller.core import DEFAULTS, PACIFIC, Store, dt, iso, period_ranges, summarize, inventory_estimate, snapshot, detect_alerts, validate_settings, atomic_json
from seller.api import normalize_orders, parse_report, APIError, Amazon
from seller.sync import Sync
from seller.app import notifications


class SellerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.db")
        self.at = datetime(2026, 9, 7, 12, tzinfo=PACIFIC)
        self.config = DEFAULTS | {"selected": ["A", "B"]}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def add(self, order="1", sku="A", qty=2, cents=500, status="SHIPPED", at=None):
        at = at or self.at-timedelta(hours=1)
        self.store.order(order, sku, iso(at), iso(at), status, qty, cents)

    def test_metric_preferences_and_legacy_defaults(self):
        from seller.core import load_config
        config_dir = Path(self.tmp.name)
        atomic_json(config_dir / "settings.json", {"period": "month"})
        self.assertEqual(load_config(config_dir)["metric"], "units")
        for metric in ("units", "orders", "revenue"):
            validate_settings({"metric": metric})
            atomic_json(config_dir / "settings.json", {"period": "year", "metric": metric})
            config = load_config(config_dir)
            self.assertEqual((config["period"], config["metric"]), ("year", metric))
        for metric in ("returns", "", None, 1, []):
            with self.assertRaises(ValueError):
                validate_settings({"metric": metric})

    def test_trend_dates_counts_and_daily_coverage_across_dst(self):
        self.at = dt("2026-03-10T00:30:00-07:00")
        self.store.put("product", "A", dict(sku="A", name="Test", active=True, fba=True, available=10))
        start = dt("2026-03-08T00:00:00-08:00")
        end = dt("2026-03-09T00:00:00-07:00")
        self.store.cover("orders", start, end)
        self.add(qty=7, at=start+timedelta(hours=12))
        self.add(order="pending", qty=9, status="PENDING", at=start+timedelta(hours=12))
        product = snapshot(self.store, self.config, self.at)["products"][0]
        days = product["trend_days"]
        self.assertEqual(len(days), 14)
        self.assertEqual(days[0]["date"], "2026-02-24")
        self.assertEqual(days[-1]["date"], "2026-03-09")
        self.assertEqual(days[-2], dict(date="2026-03-08", units=7, complete=True))
        self.assertEqual(days[-1], dict(date="2026-03-09", units=0, complete=False))
        self.assertFalse(product["trend_complete"])
        self.assertEqual([d["units"] for d in days], product["trend"])

    def test_calendar_periods(self):
        for date, period, expected in [("2026-03-31T14:30:00-07:00", "month", "2026-02-28"), ("2024-02-29T13:00:00-08:00", "year", "2023-02-28")]:
            a,b,c,d = period_ranges(period, dt(date))
            self.assertEqual(str(d.date()), expected)
            self.assertEqual(b.hour, d.hour)
        a,b,c,d = period_ranges("week", self.at)
        self.assertEqual(a.weekday(), 6)
        self.assertEqual(a.day, 6)

    def test_dst_and_midnight(self):
        a,b,c,d = period_ranges("day", dt("2026-03-08T14:00:00-07:00"))
        self.assertEqual(b.hour, d.hour)
        self.assertNotEqual(b.utcoffset(), d.utcoffset())
        a,b,c,d = period_ranges("day", dt("2026-11-01T00:00:00-07:00"))
        self.assertEqual(a, b)

    def test_orders_distinct_selection_pending_cancelled(self):
        self.add(sku="A")
        self.add(sku="B", qty=3, cents=700)
        self.add(order="2", qty=5, status="PENDING")
        self.add(order="3", qty=9, status="CANCELLED")
        value = summarize(self.store, {"A", "B"}, self.at-timedelta(days=1), self.at)
        self.assertEqual((value["units"],value["orders"],value["revenue"],value["pending"]), (5,1,12,5))
        self.assertFalse(value["sales_complete"])
        value = summarize(self.store, {"B"}, self.at-timedelta(days=1), self.at)
        self.assertEqual(value["units"],3)

    def test_missing_revenue_and_older_report(self):
        self.add(cents=None)
        self.store.order("1", "A", iso(self.at-timedelta(hours=1)), iso(self.at-timedelta(days=1)), "CANCELLED", 0, 0)
        value = summarize(self.store,{"A"},self.at-timedelta(days=1),self.at)
        self.assertEqual(value["units"], 2)
        self.assertFalse(value["revenue_complete"])

    def test_coverage_gaps(self):
        self.store.cover("orders",self.at-timedelta(days=3),self.at-timedelta(days=2))
        self.store.cover("orders",self.at-timedelta(days=1),self.at)
        self.assertFalse(self.store.covered("orders",self.at-timedelta(days=3),self.at))
        self.store.cover("orders",self.at-timedelta(days=2),self.at-timedelta(days=1))
        self.assertTrue(self.store.covered("orders",self.at-timedelta(days=3),self.at))

    def test_forecast_no_double_counting(self):
        p = dict(sku="A",available=20,inbound_working=999,inbound_shipped=40,inbound_receiving=10)
        value = inventory_estimate(p,60,30,self.config)
        self.assertEqual(value["days_left"],10)
        self.assertEqual(value["target"],148)
        self.assertEqual(value["suggested"],78)
        self.assertIsNone(inventory_estimate(p,0,30,self.config)["days_left"])
        self.assertIsNone(inventory_estimate(p,0,0,self.config)["suggested"])
        p["available"] = None
        self.assertIsNone(inventory_estimate(p,60,30,self.config)["suggested"])

    def test_normalize_2026_orders(self):
        order = {"orderId":"1","createdTime":iso(self.at-timedelta(hours=1)),"lastUpdatedTime":iso(self.at),
                 "salesChannel":{"marketplaceId":"ATVPDKIKX0DER"}, "fulfillment":{"fulfilledBy":"AMAZON","fulfillmentStatus":"SHIPPED"},
                 "buyer":{"buyerName":"DO NOT STORE"}, "orderItems":[{"product":{"sellerSku":"A","title":"Item","asin":"ASIN"},"quantityOrdered":2,
                    "proceeds":{"breakdowns":[{"type":"ITEM","subtotal":{"amount":"20.00","currencyCode":"USD"}}, {"type":"TAX","subtotal":{"amount":"2.00","currencyCode":"USD"}},
                    {"type":"DISCOUNT","detailedBreakdowns":[{"subtype":"ITEM","value":{"amount":"3.00","currencyCode":"USD"}}]}]}}]}
        normalize_orders(self.store,[order])
        normalize_orders(self.store,[order])
        row = self.store.db.execute("SELECT * FROM orders").fetchall()
        self.assertEqual(len(row),1)
        self.assertEqual(row[0]["cents"],1700)
        self.assertNotIn("DO NOT STORE", json.dumps(self.store.all("product")))

    def test_returns_idempotent_partial_and_corrected(self):
        text = "return-date\tsku\tquantity\treason\torder-id\tcustomer-comments\n2026-09-06T10:00:00-07:00\tA\t1\tDefective\t1\tDO NOT STORE\n"
        start,end=self.at-timedelta(days=2),self.at
        for _ in range(2):
            parse_report(self.store,"returns",text,start,end,self.at)
        self.assertEqual(self.store.db.execute("SELECT sum(quantity) FROM returns").fetchone()[0],1)
        parse_report(self.store,"returns",text.replace("\t1\tDefective", "\t2\tDefective"),start,end,self.at)
        self.assertEqual(self.store.db.execute("SELECT sum(quantity) FROM returns").fetchone()[0],2)
        self.assertNotIn("DO NOT STORE",json.dumps([dict(r) for r in self.store.db.execute("SELECT * FROM returns")]))

    def test_alert_ack_escalation_recurrence(self):
        self.store.product("A", available=0, name="Bottle", active=True, fba=True)
        view=snapshot(self.store,self.config,self.at)
        detect_alerts(self.store,view,self.at)
        self.store.db.execute("UPDATE alerts SET acknowledged=1,notified=1")
        detect_alerts(self.store,view,self.at+timedelta(hours=1))
        self.assertEqual(self.store.db.execute("SELECT acknowledged FROM alerts").fetchone()[0],1)
        self.store.product("A", available=200)
        detect_alerts(self.store,snapshot(self.store,self.config,self.at),self.at)
        self.assertEqual(self.store.db.execute("SELECT active FROM alerts").fetchone()[0],0)
        self.store.product("A",available=0)
        detect_alerts(self.store,snapshot(self.store,self.config,self.at),self.at)
        self.assertEqual(self.store.db.execute("SELECT acknowledged FROM alerts").fetchone()[0],0)

    def test_hidden_products_and_account_alerts(self):
        self.store.product("X",available=0,name="Hidden",active=True,fba=True)
        self.store.put("meta","health_issues",[dict(key="status",title="AT_RISK",detail="Review")])
        detect_alerts(self.store,snapshot(self.store,self.config,self.at),self.at)
        view=snapshot(self.store,self.config,self.at)
        self.assertEqual(len(view["alerts"]),1)
        self.assertEqual(view["alerts"][0]["sku"],"")

    def test_demo_never_notifies(self):
        with patch("seller.app.subprocess.run") as send:
            notifications(self.store,{"mode":"demo","settings":self.config})
            send.assert_not_called()

    def test_secure_files_and_validation(self):
        file=Path(self.tmp.name)/"secret.json"
        atomic_json(file,{"secret":"private"})
        self.assertEqual(file.stat().st_mode & 0o777,0o600)
        for change in ({"lead_days":-1},{"lead_days":float("nan")},{"period":"banana"},{"selected":"A"},{"overrides":{"A":{"mode":"demo"}}}):
            with self.assertRaises(ValueError): validate_settings(change)

    def test_read_only_api(self):
        with self.assertRaises(ValueError):
            Amazon({}).call("/listings/items",body={"price":1})

    def test_paginated_order_checkpoint(self):
        sync=Sync(self.store,{})
        with patch.object(sync.api,"call",side_effect=[{"orders":[],"pagination":{"nextToken":"next"}},{"orders":[]}]) as call:
            self.assertFalse(sync.orders(self.at))
            self.assertEqual(self.store.get("meta","order_page")["token"],"next")
            self.assertTrue(sync.orders(self.at+timedelta(minutes=5)))
            self.assertIsNone(self.store.get("meta","order_page"))
            self.assertEqual(call.call_args.args[1]["paginationToken"],"next")

    def test_throttle_isolated_and_cached(self):
        self.store.put("meta","verified",True)
        self.store.product("A",available=22)
        self.store.db.commit()
        sync=Sync(self.store,{})
        with patch.object(sync,"orders",side_effect=APIError(429,"Rate limit",600)), patch.object(sync,"inventory",return_value=True), patch.object(sync,"listings",return_value=True), patch.object(sync,"shipments",return_value=True), patch.object(sync,"reports"):
            sync.tick()
        self.assertEqual(self.store.get("product","A")["available"],22)
        self.assertEqual(self.store.get("source","orders")["state"],"error")
        self.assertEqual(self.store.get("source","inventory")["state"],"ready")

    def test_inventory_pagination_outside_payload(self):
        api=Amazon({})
        with patch.object(api,"call",side_effect=[{"payload":{"inventorySummaries":[{"sellerSku":"A"}]},"pagination":{"nextToken":"more"}}, {"payload":{"inventorySummaries":[{"sellerSku":"B"}]}}]) as call:
            items=list(api.pages("/fba/inventory/v1/summaries",{},"inventorySummaries",payload=True,token="nextToken"))
        self.assertEqual(len(items),2)
        self.assertEqual(call.call_args.args[1]["nextToken"],"more")

    def test_malformed_report_does_not_claim_coverage(self):
        with self.assertRaises(ValueError):
            parse_report(self.store,"history","Wrong\tFields\n",self.at-timedelta(days=1),self.at,self.at)
        self.assertFalse(self.store.covered("orders",self.at-timedelta(days=1),self.at))


if __name__ == "__main__":
    unittest.main()
