"""Resumable local polling scheduler. Failures are isolated by source."""
from __future__ import annotations

import calendar
import json
import time
from datetime import timedelta
from urllib.parse import quote

from .api import Amazon, APIError, REPORTS, normalize_orders, number, parse_report
from .core import INTERVALS, MARKET, PACIFIC, dt, iso, now


class Sync:
    def __init__(self, store, credentials):
        self.store = store
        self.api = Amazon(credentials)

    def tick(self, force=False):
        store, at = self.store, now()
        if not store.get("meta", "verified"):
            if at.timestamp() < store.get("meta", "auth_retry", 0):
                return
            try:
                self.api.verify()
            except APIError as error:
                store.put("meta", "auth_retry", at.timestamp()+max(900,error.retry_after))
                store.source("orders", state="error", error=str(error))
                store.db.commit()
                return
            store.put("meta", "verified", {"marketplace": MARKET, "at": iso(at)})
            store.db.commit()
        for name in ("orders", "inventory", "listings", "shipments", "shipment_dates"):
            source = store.get("source", name, {})
            if at.timestamp() < source.get("retry_at", 0):
                continue
            if not force and at.timestamp() < source.get("due", 0):
                continue
            try:
                complete = getattr(self, name)(at)
                store.source(name, success=iso(at) if complete else source.get("success"), state="ready" if complete else "syncing", error=None, failures=0,
                             due=at.timestamp()+(INTERVALS[name] if complete else 180))
                store.db.commit()
            except APIError as error:
                store.db.rollback()
                count = source.get("failures", 0) + 1
                retry = max(error.retry_after, min(14400, 60 * 2**min(count, 8)))
                store.source(name, state="unavailable" if error.status == 403 else "error", error=str(error), failures=count, retry_at=at.timestamp()+retry)
                store.db.commit()
            except (KeyError, ValueError, TypeError):
                store.db.rollback()
                store.source(name, state="error", error="Unexpected Amazon response; last successful data retained", retry_at=at.timestamp()+900)
                store.db.commit()
        self.reports(at, force)
        store.db.commit()

    def orders(self, at):
        store = self.store
        progress = store.get("meta", "order_page")
        if not progress:
            source = store.get("source", "orders", {})
            end = at - timedelta(minutes=3)
            start = dt(source["watermark"]) - timedelta(minutes=15) if source.get("watermark") else end-timedelta(days=3)
            progress = {"start": iso(start), "end": iso(end), "token": None, "initial": not source.get("watermark")}
        params = {"marketplaceIds": MARKET, "fulfilledBy": "AMAZON", "maxResultsPerPage": 100,
                  "includedData": "FULFILLMENT,PROCEEDS,CANCELLATION", "lastUpdatedAfter": progress["start"],
                  "lastUpdatedBefore": progress["end"], "paginationToken": progress["token"]}
        data = self.api.call("/orders/2026-01-01/orders", params)
        if "orders" not in data or not isinstance(data["orders"], list):
            raise APIError(0, "Expected orders missing; retaining checkpoint")
        normalize_orders(store, data["orders"])
        token = data.get("pagination", {}).get("nextToken")
        if token:
            progress["token"] = token
            store.put("meta", "order_page", progress)
            return False
        store.put("meta", "order_page", None)
        store.cover("orders", dt(progress["start"]), dt(progress["end"]))
        store.source("orders", watermark=progress["end"])
        return True

    def inventory(self, at):
        items = list(self.api.pages("/fba/inventory/v1/summaries", {"granularityType": "Marketplace", "granularityId": MARKET, "marketplaceIds": MARKET, "details": "true"}, "inventorySummaries", payload=True, token="nextToken"))
        seen = set()
        for item in items:
            sku = item["sellerSku"]
            seen.add(sku)
            detail = item.get("inventoryDetails", {})
            available = detail.get("fulfillableQuantity")
            self.store.product(sku, name=item.get("productName", sku), asin=item.get("asin", ""), fba=True,
                active=True, available=available, reserved=detail.get("reservedQuantity", {}).get("totalReservedQuantity"),
                unsellable=detail.get("unfulfillableQuantity", {}).get("totalUnfulfillableQuantity"),
                inbound_working=detail.get("inboundWorkingQuantity", 0), inbound_shipped=detail.get("inboundShippedQuantity", 0),
                inbound_receiving=detail.get("inboundReceivingQuantity", 0), inventory_at=iso(at))
            if available == 0:
                self.store.product(sku, stockout_at=iso(at))
        for p in self.store.all("product"):
            if p["sku"] not in seen:
                self.store.product(p["sku"], available=None, inventory_at=None, active=False)
        return True

    def listings(self, at):
        seller = quote(self.api.credentials["seller_id"], safe="")
        items = list(self.api.pages("/listings/2021-08-01/items/"+seller, {"marketplaceIds": MARKET, "includedData": "summaries,issues", "pageSize": 20}, "items"))
        for item in items:
            summaries = [s for s in item.get("summaries", []) if s.get("marketplaceId") == MARKET]
            if not summaries:
                continue
            s = summaries[0]
            issues = [i.get("message", i.get("code", "Listing issue")) for i in item.get("issues", []) if i.get("severity") in ("ERROR", "WARNING")]
            self.store.product(item["sku"], name=s.get("itemName", item["sku"]), asin=s.get("asin", ""), listing_known=True,
                               buyable="BUYABLE" in s.get("status", []), issues=issues, listing_at=iso(at))
        return True

    def shipments(self, at):
        # Legacy READ operations remain useful for shipments created in Seller Central,
        # and expose received quantities unavailable in several newer planning operations.
        items = list(self.api.pages("/fba/inbound/v0/shipments", {"QueryType": "SHIPMENT", "MarketplaceId": MARKET,
            "ShipmentStatusList": "WORKING,READY_TO_SHIP,SHIPPED,RECEIVING,ERROR,IN_TRANSIT,DELIVERED,CHECKED_IN"},
            "ShipmentData", payload=True, token="NextToken", token_path=None))
        known = {s["id"]: s for s in self.store.all("shipment")}
        seen = {s["ShipmentId"] for s in items}
        # Refresh previously open shipments that may have closed or been cancelled.
        missing = [key for key, s in known.items() if key not in seen and s["status"] not in ("CLOSED", "DELETED", "CANCELLED")]
        for i in range(0, len(missing), 50):
            items.extend(self.api.pages("/fba/inbound/v0/shipments", {"QueryType": "SHIPMENT", "MarketplaceId": MARKET, "ShipmentIdList": missing[i:i+50]}, "ShipmentData", payload=True, token="NextToken", token_path=None))
        for s in items:
            shipment_id = s["ShipmentId"]
            data = self.api.call("/fba/inbound/v0/shipments/"+quote(shipment_id, safe="")+"/items").get("payload", {})
            if not isinstance(data.get("ItemData"), list):
                raise APIError(0, "Shipment quantities unavailable; retaining previous data")
            shipment_items = data["ItemData"]
            token = data.get("NextToken")
            seen_tokens = set()
            while token:
                if token in seen_tokens:
                    raise APIError(0, "Repeated shipment pagination token")
                seen_tokens.add(token)
                data = self.api.call("/fba/inbound/v0/shipmentItems", {"QueryType": "NEXT_TOKEN", "NextToken": token, "MarketplaceId": MARKET}).get("payload", {})
                if not isinstance(data.get("ItemData"), list):
                    raise APIError(0, "Shipment quantities unavailable; retaining previous data")
                shipment_items.extend(data["ItemData"])
                token = data.get("NextToken")
            self.store.put("shipment", shipment_id, dict(id=shipment_id, name=s.get("ShipmentName", shipment_id), status=s["ShipmentStatus"], eta=known.get(shipment_id,{}).get("eta"),
                updated=iso(at), url="https://sellercentral.amazon.com/fba/inbound-shipment/summary/"+quote(shipment_id, safe=""),
                items=[dict(sku=x["SellerSKU"], shipped=x.get("QuantityShipped"), received=x.get("QuantityReceived"),
                            remaining=max(0, x["QuantityShipped"]-x["QuantityReceived"]) if x.get("QuantityShipped") is not None and x.get("QuantityReceived") is not None else None) for x in shipment_items]))
        return True

    def shipment_dates(self, at):
        known = {s["id"]:s for s in self.store.all("shipment") if s["status"] not in ("CLOSED","DELETED","CANCELLED")}
        if not known:
            return True
        base = "/inbound/fba/2024-03-20/inboundPlans"
        progress = self.store.get("meta", "shipment_date_page", {})
        response = self.api.call(base, {"pageSize":30,"sortBy":"LAST_UPDATED_TIME","sortOrder":"DESC","paginationToken":progress.get("token")})
        if "inboundPlans" not in response:
            raise APIError(0,"Shipment plans unavailable; arrival dates remain unknown")
        for summary in response["inboundPlans"]:
            if MARKET not in summary.get("marketplaceIds", []) or summary.get("status") == "VOIDED":
                continue
            plan = self.api.call(base+"/"+quote(summary["inboundPlanId"],safe=""))
            for shipment in plan.get("shipments", []):
                if shipment.get("status") in ("CLOSED","CANCELLED","DELETED"):
                    continue
                detail = self.api.call(base+"/"+quote(summary["inboundPlanId"],safe="")+"/shipments/"+quote(shipment["shipmentId"],safe=""))
                key = detail.get("shipmentConfirmationId")
                if key in known:
                    window = detail.get("selectedDeliveryWindow") or {}
                    known[key].update(eta=window.get("endDate"), eta_start=window.get("startDate"), eta_at=iso(at))
                    self.store.put("shipment", key, known[key])
        token = response.get("pagination", {}).get("nextToken")
        self.store.put("meta", "shipment_date_page", {"token":token} if token else {})
        return not bool(token)

    def reports(self, at, force):
        store = self.store
        if at.timestamp() < store.get("meta", "reports_retry", 0):
            return
        jobs = store.all("job")
        for job in jobs:
            if job["state"] != "pending" or job.get("next_poll", 0) > at.timestamp():
                continue
            try:
                report = self.api.call("/reports/2021-06-30/reports/"+quote(job["id"], safe=""))
                status = report["processingStatus"]
                if status == "DONE":
                    text = self.api.document(report["reportDocumentId"])
                    parse_report(store, job["kind"], text, dt(job["start"]), dt(job["end"]), at)
                    job["state"] = "done"
                    store.source(job["source"], success=iso(at), error=None, state="ready", due=at.timestamp()+(0 if job["source"] in ("history", "returns_history") else INTERVALS.get(job["source"], 86400)))
                elif status in ("FATAL", "CANCELLED"):
                    # CANCELLED is not proof of an empty result. Keep coverage unknown.
                    job["state"] = "failed"
                    store.source(job["source"], state="unavailable", error="Amazon report "+status.lower()+"; coverage remains incomplete", due=at.timestamp()+14400)
                else:
                    job["next_poll"] = at.timestamp()+60
                store.put("job", job["id"], job)
                store.db.commit()
            except APIError as error:
                store.db.rollback()
                job["next_poll"] = at.timestamp()+max(300, error.retry_after)
                store.put("job", job["id"], job)
                store.source(job["source"], error=str(error), state="error")
                store.db.commit()
            except (ValueError, KeyError, TypeError):
                store.db.rollback()
                job["state"] = "failed"
                store.put("job", job["id"], job)
                store.source(job["source"], error="Report format unexpected; last data retained", state="error", due=at.timestamp()+14400)
                store.db.commit()
        jobs = store.all("job")
        if sum(j["state"] == "pending" for j in jobs) >= 3:
            return
        if at.timestamp() < store.get("meta", "report_create_due", 0):
            return
        local = at.astimezone(PACIFIC)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        earliest = day.replace(year=day.year-1, month=1, day=1)
        # Daily reconciliation receives old orders whose status was changed recently.
        queue = [(name, name, day-timedelta(days=30), at-timedelta(minutes=3)) for name in ("returns", "restock", "health", "stranded", "excess")]
        queue.insert(0, ("reconcile", "reconcile", at-timedelta(days=3), at-timedelta(minutes=3)))
        # Anchor historical windows to calendar months so boundaries remain stable between ticks.
        month_end = day.replace(day=1)
        windows = [(month_end, day)] if month_end < day else []
        while month_end > earliest:
            month_start = (month_end-timedelta(days=1)).replace(day=1)
            windows.append((month_start, month_end))
            month_end = month_start
        for start, end in windows:
            if not store.covered("orders", start, end):
                queue.append(("history", "history", start, end))
            # Daily returns are authoritative only through completed days.
            return_end = min(end, day)
            if start < return_end and not store.covered("returns", start, return_end):
                queue.append(("returns", "returns_history", start, return_end))
        complete = sum(store.covered("orders", a, b) for a, b in windows)
        store.put("meta", "history", dict(start=iso(earliest), windows=len(windows), complete=complete))
        for kind, source, start, end in queue:
            if any(j["state"] == "pending" and j["source"] == source for j in jobs):
                continue
            state = store.get("source", source, {})
            if at.timestamp() < state.get("due", 0) and not (force and source in INTERVALS and not state.get("success")):
                continue
            # Failed historical windows retry later, not in a tight loop.
            if any(j["state"] == "failed" and j["kind"] == kind and j["start"] == iso(start) and at.timestamp()-j.get("created", 0) < 14400 for j in jobs):
                continue
            try:
                body = dict(reportType=REPORTS[kind], marketplaceIds=[MARKET], dataStartTime=iso(start), dataEndTime=iso(end))
                response = self.api.call("/reports/2021-06-30/reports", body=body)
                store.put("meta", "report_create_due", at.timestamp()+65)
                report_id = response["reportId"]
                store.put("job", report_id, dict(id=report_id, kind=kind, source=source, start=iso(start), end=iso(end), state="pending", created=at.timestamp(), next_poll=at.timestamp()+60))
                store.source(source, state="syncing", due=at.timestamp()+INTERVALS.get(source, 0))
                store.db.commit()
            except APIError as error:
                store.source(source, error=str(error), state="unavailable" if error.status == 403 else "error", due=at.timestamp()+14400)
                store.put("meta", "reports_retry", at.timestamp()+max(error.retry_after, 60))
                store.db.commit()
            break  # Bound createReport rate; persist before the next scheduler tick.
