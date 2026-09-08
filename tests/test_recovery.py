import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from seller.core import DEFAULTS, Store, PACIFIC, iso, snapshot, detect_alerts, load_config
from seller.api import APIError, parse_report, Amazon
from seller.sync import Sync
from seller.app import notifications, change_mode, publish


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/"data.db")
        self.at=datetime(2026,9,7,12,tzinfo=PACIFIC)
        self.config=DEFAULTS | {"selected":["A"]}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_completed_job_survives_restart_and_marks_window(self):
        start=self.at-timedelta(days=1)
        self.store.put("job","R1",dict(id="R1",kind="history",source="history",start=iso(start),end=iso(self.at),state="pending",next_poll=0))
        self.store.put("meta","report_create_due",self.at.timestamp()+1000)
        self.store.db.commit()
        self.store.db.close()
        self.store=Store(self.root/"data.db")
        sync=Sync(self.store,{})
        text="amazon-order-id\tsku\tpurchase-date\tquantity\torder-status\tfulfillment-channel\tsales-channel\n"
        with patch.object(sync.api,"call",return_value={"processingStatus":"DONE","reportDocumentId":"D1"}), patch.object(sync.api,"document",return_value=text):
            sync.reports(self.at,False)
        self.assertEqual(self.store.get("job","R1")["state"],"done")
        self.assertTrue(self.store.covered("orders",start,self.at))
        self.assertLessEqual(self.store.get("source","history")["due"],self.at.timestamp())

    def test_cancelled_job_never_claims_zero(self):
        start=self.at-timedelta(days=1)
        self.store.put("job","R1",dict(id="R1",kind="returns",source="returns",start=iso(start),end=iso(self.at),state="pending",next_poll=0))
        self.store.put("meta","report_create_due",self.at.timestamp()+1000)
        self.store.db.commit()
        sync=Sync(self.store,{})
        with patch.object(sync.api,"call",return_value={"processingStatus":"CANCELLED"}):
            sync.reports(self.at,False)
        self.assertFalse(self.store.covered("returns",start,self.at))
        self.assertEqual(self.store.get("job","R1")["state"],"failed")

    def test_current_day_returns_always_provisional(self):
        start=self.at-timedelta(days=3)
        parse_report(self.store,"returns","return-date\tsku\tquantity\n",start,self.at,self.at)
        self.assertFalse(self.store.covered("returns",start,self.at))
        self.assertTrue(self.store.covered("returns",start,self.at.replace(hour=0)))

    def test_one_grouped_notification_ack_snooze_and_recurrence(self):
        self.store.product("A",available=0,active=True,fba=True,name="A")
        detect_alerts(self.store,snapshot(self.store,self.config,self.at),self.at)
        view=snapshot(self.store,self.config,self.at)
        with patch("seller.app.now",return_value=self.at),patch("seller.app.subprocess.run") as send:
            notifications(self.store,view)
            notifications(self.store,view)
            self.assertEqual(send.call_count,1)
            self.store.db.execute("UPDATE alerts SET snoozed_until=?",(iso(self.at+timedelta(hours=24)),))
            notifications(self.store,view)
            self.assertEqual(send.call_count,1)
        with patch("seller.app.now",return_value=self.at+timedelta(hours=25)),patch("seller.app.subprocess.run") as send:
            notifications(self.store,view)
            self.assertEqual(send.call_count,1)
            self.store.db.execute("UPDATE alerts SET acknowledged=1")
            notifications(self.store,view,resume=True)
            self.assertEqual(send.call_count,1)

    def test_warning_to_urgent_resets_ack(self):
        self.store.product("A",available=30,active=True,fba=True,name="A")
        end=self.at.replace(hour=0)
        self.store.cover("orders",end-timedelta(days=30),self.at)
        self.store.order("1","A",iso(end-timedelta(days=1)),iso(self.at),"SHIPPED",30,100)
        detect_alerts(self.store,snapshot(self.store,self.config,self.at),self.at)
        self.assertEqual(self.store.db.execute("SELECT severity FROM alerts").fetchone()[0],"warning")
        self.store.db.execute("UPDATE alerts SET acknowledged=1,notified=1")
        self.store.product("A",available=5)
        detect_alerts(self.store,snapshot(self.store,self.config,self.at),self.at)
        r=self.store.db.execute("SELECT * FROM alerts").fetchone()
        self.assertEqual((r["severity"],r["acknowledged"],r["notified"]),("urgent",0,0))

    def test_live_demo_selection_isolated(self):
        live=DEFAULTS | {"selected":["REAL"],"pinned":["REAL"]}
        demo=change_mode(self.root,live,"demo")
        self.assertIsNone(demo["selected"])
        demo["selected"]=["TRAIL-24"]
        restored=change_mode(self.root,demo,"live")
        self.assertEqual(restored["selected"],["REAL"])

    def test_empty_api_payload_fails_closed(self):
        api=Amazon({})
        with patch.object(api,"call",return_value={"payload":{}}):
            with self.assertRaises(APIError): list(api.pages("/inventory",{},"inventorySummaries",payload=True))

    def test_old_delivery_window_not_late_after_arrival(self):
        self.store.put("shipment","s",dict(id="s",name="Arrived",status="RECEIVING",eta=iso(self.at-timedelta(days=2)),url="https://sellercentral.amazon.com/",items=[dict(sku="A",remaining=5)]))
        detect_alerts(self.store,snapshot(self.store,self.config,self.at),self.at)
        self.assertEqual(len(snapshot(self.store,self.config,self.at)["alerts"]),0)

    def test_shipment_window_enrichment(self):
        self.store.put("shipment","FBA1",dict(id="FBA1",status="IN_TRANSIT",items=[]))
        sync=Sync(self.store,{})
        responses=[{"inboundPlans":[{"inboundPlanId":"p","marketplaceIds":["ATVPDKIKX0DER"],"status":"ACTIVE"}]},
                   {"shipments":[{"shipmentId":"s","status":"SHIPPED"}]},
                   {"shipmentConfirmationId":"FBA1","selectedDeliveryWindow":{"startDate":iso(self.at),"endDate":iso(self.at+timedelta(days=1))}}]
        with patch.object(sync.api,"call",side_effect=responses): self.assertTrue(sync.shipment_dates(self.at))
        self.assertEqual(self.store.get("shipment","FBA1")["eta"],iso(self.at+timedelta(days=1)))

    def test_snapshot_handles_thousand_products(self):
        import time
        for n in range(1000): self.store.product(str(n),name="Product "+str(n),fba=True,active=True,available=n)
        self.store.db.commit()
        started=time.monotonic()
        view=publish(self.store,DEFAULTS,self.root)
        self.assertEqual(len(view["products"]),1000)
        self.assertLess(time.monotonic()-started,5)


if __name__ == "__main__": unittest.main()
