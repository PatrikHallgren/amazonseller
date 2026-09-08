"""Read-only Amazon operations and allowlisted report parsing."""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .core import MARKET, dt, iso

BASE = "https://sellingpartnerapi-na.amazon.com"
REPORTS = {
    "history": "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
    "reconcile": "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_LAST_UPDATE_GENERAL",
    "returns": "GET_FBA_FULFILLMENT_CUSTOMER_RETURNS_DATA",
    "restock": "GET_RESTOCK_INVENTORY_RECOMMENDATIONS_REPORT",
    "stranded": "GET_STRANDED_INVENTORY_UI_DATA",
    "excess": "GET_FBA_INVENTORY_PLANNING_DATA",
    "health": "GET_V2_SELLER_PERFORMANCE_REPORT",
}


class APIError(Exception):
    def __init__(self, status, message, retry_after=300):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


class Amazon:
    def __init__(self, credentials):
        self.credentials = credentials
        self.token = None
        self.expires = 0
        self.last_request = 0

    @staticmethod
    def http(url, method="GET", body=None, headers=None):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, method=method, headers=headers or {}), timeout=35) as response:
                return response.read(), response.headers
        except urllib.error.HTTPError as e:
            # Never log Amazon response bodies: they can contain credentials, URLs or PII.
            retry = e.headers.get("Retry-After", "300")
            messages = {401: "Authorization expired; check your connection", 403: "Permission unavailable; check application roles", 429: "Amazon rate limit; retry scheduled", 400: "Amazon rejected this request; check API configuration"}
            raise APIError(e.code, messages.get(e.code, "Amazon request failed (HTTP %s)" % e.code), int(retry) if retry.isdigit() else 300) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0, "Amazon unreachable; keeping last successful data") from None

    def access_token(self):
        if self.token and time.time() < self.expires:
            return self.token
        data = urllib.parse.urlencode(dict(grant_type="refresh_token", client_id=self.credentials["client_id"], client_secret=self.credentials["client_secret"], refresh_token=self.credentials["refresh_token"])).encode()
        raw, _ = self.http("https://api.amazon.com/auth/o2/token", "POST", data, {"Content-Type": "application/x-www-form-urlencoded"})
        response = json.loads(raw)
        self.token = response["access_token"]
        self.expires = time.time() + int(response.get("expires_in", 3600)) - 120
        return self.token

    def call(self, path, params=None, body=None):
        # Only report creation is a permitted POST; no selling-account mutations.
        if body is not None and path != "/reports/2021-06-30/reports":
            raise ValueError("Only read/report operations are supported")
        delay = 0.55 - (time.monotonic()-self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        query = urllib.parse.urlencode({k: ",".join(v) if isinstance(v, list) else v for k, v in (params or {}).items() if v is not None})
        raw, _ = self.http(BASE + path + ("?" + query if query else ""),
                           "POST" if body is not None else "GET",
                           json.dumps(body).encode() if body is not None else None,
                           {"x-amz-access-token": self.access_token(), "Content-Type": "application/json", "User-Agent": "AmazonSellerOmarchy/1.0"})
        return json.loads(raw)

    def pages(self, path, params, key, *, payload=False, token="pageToken", token_path="pagination", limit=10000):
        seen_tokens = set()
        for _ in range(limit):
            response = self.call(path, params)
            data = response.get("payload", {}) if payload else response
            if key not in data or not isinstance(data[key], list):
                raise APIError(0, "Expected data missing from Amazon response; cache retained")
            yield from data[key]
            nxt = (data.get(token_path) or response.get(token_path) or {}).get("nextToken") if token_path else data.get("NextToken")
            if not nxt:
                return
            if nxt in seen_tokens:
                raise APIError(0, "Amazon repeated a pagination token; retry scheduled")
            seen_tokens.add(nxt)
            params = params | {token: nxt}
            if token == "NextToken" and "QueryType" in params:
                params = {"QueryType": "NEXT_TOKEN", "NextToken": nxt, "MarketplaceId": MARKET}
        raise APIError(0, "Pagination safety limit reached; data is incomplete")

    def verify(self):
        data = self.call("/sellers/v1/marketplaceParticipations").get("payload", [])
        if not any(x.get("marketplace", {}).get("id") == MARKET and x.get("participation", {}).get("isParticipating") for x in data):
            raise APIError(403, "This authorization does not have an active US marketplace")
        # A listings read also verifies that the supplied seller ID belongs to the authorization.
        self.call("/listings/2021-08-01/items/" + urllib.parse.quote(self.credentials["seller_id"], safe=""), {"marketplaceIds": MARKET, "pageSize": 1})

    def document(self, document_id):
        document = self.call("/reports/2021-06-30/documents/" + urllib.parse.quote(document_id, safe=""))
        url = document["url"]
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or not (parsed.hostname.endswith(".amazonaws.com") or parsed.hostname.endswith(".amazon.com")):
            raise APIError(0, "Unexpected report download host")
        raw, _ = self.http(url)
        if document.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8-sig")


def money(value):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
        if not number.is_finite():
            return None
        return int((number * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError):
        return None


def number(value, default=0):
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError, OverflowError):
        return default


def normalize_orders(store, orders):
    for order in orders:
        if order.get("salesChannel", {}).get("marketplaceId") != MARKET:
            continue
        if order.get("fulfillment", {}).get("fulfilledBy") != "AMAZON":
            continue
        status = order.get("fulfillment", {}).get("fulfillmentStatus", "UNKNOWN")
        grouped = {}
        for item in order.get("orderItems", []):
            product = item.get("product", {})
            sku = product.get("sellerSku")
            if not sku:
                continue
            store.product(sku, name=product.get("title", sku), asin=product.get("asin", ""), fba=True)
            item_status = "CANCELLED" if item.get("cancellation", {}).get("cancellationExecution") else status
            if item_status == "CANCELLED":
                qty, cents = 0, 0
            else:
                qty = number(item.get("quantityOrdered"))
                breakdowns = item.get("proceeds", {}).get("breakdowns", [])
                values = [b for b in breakdowns if b.get("type") == "ITEM"]
                cents = money(values[0].get("subtotal", {}).get("amount")) if values and values[0].get("subtotal", {}).get("currencyCode") == "USD" else None
                for b in breakdowns:
                    if b.get("type") == "DISCOUNT" and cents is not None:
                        detail = b.get("detailedBreakdowns", [])
                        if detail:
                            for discount in detail:
                                if discount.get("subtype") == "ITEM":
                                    amount = money(discount.get("value", {}).get("amount"))
                                    cents = cents - abs(amount) if amount is not None else None
                        else:
                            # No item/shipping allocation: do not present a guessed net figure.
                            cents = None
            group = grouped.setdefault(sku, [0, 0])
            group[0] += qty
            group[1] = group[1] + cents if group[1] is not None and cents is not None else None
        for sku, (qty, cents) in grouped.items():
            store.order(order["orderId"], sku, order["createdTime"], order["lastUpdatedTime"], status, qty, cents)


def table(text):
    return list(csv.DictReader(io.StringIO(text), delimiter="\t"))


def parse_report(store, kind, text, start, end, at):
    if kind == "health":
        data = json.loads(text)
        if not isinstance(data, dict) or not ("accountStatuses" in data or "performanceMetrics" in data):
            raise ValueError("Health report schema missing")
        issues = []
        for account in data.get("accountStatuses", []):
            if account.get("marketplaceId") == MARKET and account.get("status", "NORMAL") not in ("NORMAL", "ACTIVE", "HEALTHY"):
                issues.append(dict(key="status", title=account["status"], detail="Review Account Health in Seller Central"))
        metrics = data.get("performanceMetrics", [])
        if isinstance(metrics, dict):
            metrics = [metrics]
        for block in metrics:
            if block.get("marketplaceId", MARKET) != MARKET:
                continue
            for key, value in block.items():
                if isinstance(value, dict) and value.get("status") in ("AT_RISK", "BAD", "POOR", "WARNING", "DEACTIVATED"):
                    issues.append(dict(key=key, title=key, detail="Reported status: " + value["status"]))
        store.put("meta", "health_issues", issues)
        return
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    required = {"history": {"amazon-order-id", "sku", "purchase-date", "quantity", "order-status", "fulfillment-channel", "sales-channel"},
                "reconcile": {"amazon-order-id", "sku", "purchase-date", "quantity", "order-status", "fulfillment-channel", "sales-channel"},
                "returns": {"return-date", "sku", "quantity"}, "restock": {"Merchant SKU"}, "stranded": {"sku"}, "excess": {"sku"}}
    if not required[kind].issubset(set(reader.fieldnames or [])):
        raise ValueError("Report columns changed or document is empty")
    rows = list(reader)
    if kind in ("history", "reconcile"):
        grouped = {}
        for r in rows:
            if r.get("fulfillment-channel") not in ("Amazon", "AFN") or r.get("sales-channel") != "Amazon.com":
                continue
            key = (r["amazon-order-id"], r["sku"])
            amount = money(r.get("item-price")) if r.get("currency") == "USD" else None
            discount = money(r.get("item-promotion-discount", "0"))
            amount = amount - abs(discount or 0) if amount is not None else None
            if key not in grouped:
                grouped[key] = dict(r=r, qty=0, cents=0)
            g = grouped[key]
            cancelled = r.get("item-status", "").upper() in ("CANCELLED", "CANCELED")
            g["qty"] += 0 if cancelled else number(r.get("quantity"))
            if not cancelled:
                g["cents"] = g["cents"] + amount if g["cents"] is not None and amount is not None else None
        for (order_id, sku), g in grouped.items():
            r = g["r"]
            store.product(sku, name=r.get("product-name") or sku, asin=r.get("asin", ""), fba=True)
            store.order(order_id, sku, r["purchase-date"], r.get("last-updated-date") or r["purchase-date"], r.get("order-status", "UNKNOWN"), g["qty"], g["cents"])
        if kind == "history":
            store.cover("orders", start, end)
    elif kind == "returns":
        # Replace precisely the requested window: duplicate downloads and corrected rows are idempotent.
        store.db.execute("DELETE FROM returns WHERE occurred>=? AND occurred<?", (iso(start), iso(end)))
        occurrences = defaultdict(int)
        for r in rows:
            if not r.get("return-date") or not r.get("sku"):
                continue
            occurred = dt(r["return-date"])
            if not start <= occurred < end:
                continue
            identity = "|".join(r.get(k, "") for k in ("order-id", "sku", "return-date", "fulfillment-center-id", "license-plate-number", "reason", "detailed-disposition"))
            occurrences[identity] += 1
            key = hashlib.sha256((identity + "|" + str(occurrences[identity])).encode()).hexdigest()
            # Intentionally ignore customer-comments, buyer identity and location fields.
            store.db.execute("INSERT OR REPLACE INTO returns VALUES(?,?,?,?,?)", (key, r["sku"], iso(occurred), number(r.get("quantity")), r.get("reason", "Unspecified")))
        # The current day remains provisional even when today's report has rows.
        from .core import PACIFIC
        day = at.astimezone(PACIFIC).replace(hour=0, minute=0, second=0, microsecond=0)
        if start < min(end, day):
            store.cover("returns", start, min(end, day))
    elif kind == "restock":
        for p in store.all("product"):
            store.product(p["sku"], amazon_recommendation=None)
        for r in rows:
            sku = r.get("Merchant SKU") or r.get("sku")
            if sku and r.get("Country", "US") == "US":
                store.product(sku, amazon_recommendation=dict(quantity=number(r.get("Recommended replenishment qty"), None), date=r.get("Recommended ship date", ""), days=number(r.get("Total Days of Supply"), None), updated=iso(at)))
    elif kind == "stranded":
        for p in store.all("product"):
            store.product(p["sku"], stranded=False)
        for r in rows:
            if r.get("sku"):
                store.product(r["sku"], stranded=True)
    elif kind == "excess":
        for r in rows:
            if r.get("sku") and r.get("marketplace", "US") in ("US", "Amazon.com"):
                store.product(r["sku"], excess=number(r.get("estimated-excess-quantity"), None), aged=number(r.get("inv-age-181-to-330-days"), None))
