# Snapshot schema

Version 1. The panel does not call Amazon. Once a day, an assistant that is already signed in to Seller Central and to Smart Warehousing (Sumner, WA) downloads the reports below, builds this document, and publishes it. The plugin reads that file.

Do not put this file, or the exports it is built from, in the public `amazonseller` repository.

## Top level

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Integer `1`. Any other value is rejected and the previous data stays on screen. |
| `generated_at` | yes | ISO-8601 timestamp with a timezone. When this file was built. The panel treats the snapshot as stale when this, or a watched source `as_of`, is over `stale_hours` (default 36). |
| `seller` | no | Labels only. Defaults: PALE Trading Co., marketplace US, currency USD, timezone `America/Los_Angeles`, warehouse Smart Warehousing in Sumner, factory Shenzhen. |
| `sources` | yes | One object per input. Each may include `as_of` (timezone-aware timestamp), `available` (boolean), `report` (name), and for sales `start`, `end` (`YYYY-MM-DD`), and `complete`. |
| `catalog` | no | Product master: `sku`, `name`, `asin`, `units_per_case`, `warehouse_sku`. |
| `daily` | no | Per SKU, per Pacific date. This is the sales history. |
| `account_daily` | no | Account-level By Date totals. A day with no per-product `daily` row uses this for period totals, labeled combined / all products. Those days are not used for per-product restock forecasts. |
| `inventory` | no | Current FBA quantities, one row per seller SKU. |
| `shipments` | no | FBA inbound shipments. |
| `warehouse` | no | Smart Warehousing on-hand stock. Quantities in the source export are cases; this file stores cases and units. |
| `returns` | no | Received return units. Omit the key to leave prior returns untouched. An empty array clears them. |

Unknown extra keys are ignored so a newer builder can add fields.

## `sources`

Watched for staleness: `sales`, `inventory`, and `warehouse` when warehouse has an `as_of`. `as_of` is when the underlying report is current through, not when the plugin last polled.

```json
"sources": {
  "sales": {
    "as_of": "2026-10-21T07:00:00+00:00",
    "available": true,
    "report": "Detail Page Sales and Traffic",
    "start": "2026-10-01",
    "end": "2026-10-20",
    "complete": true
  },
  "inventory": {"as_of": "2026-10-21T15:00:00+00:00", "available": true, "report": "FBA Manage Inventory / Inventory Health"},
  "shipments": {"as_of": "2026-10-21T15:00:00+00:00", "available": true, "report": "FBA shipments"},
  "warehouse": {"as_of": "2026-10-21T15:00:00+00:00", "available": true, "report": "Smart Warehousing inventory", "location": "Smart Warehousing, Sumner, WA"},
  "returns": {"available": false, "report": "FBA customer returns"}
}
```

`sales.as_of` is the instant after the last included Pacific date (midnight at the end of that day). `complete` is true when every calendar day from `start` through `end` is present. A gap is not filled with zeros.

## `daily`

One object per seller SKU per day. Re-building the same day replaces that day. Other days are kept when you pass the previous snapshot.

| Field | Required | Meaning |
| --- | --- | --- |
| `date` | yes | `YYYY-MM-DD` in Pacific time. |
| `sku` | yes | Seller SKU. The child ASIN is used only until inventory or the mapping file supplies a SKU. |
| `asin` | no | Child ASIN. |
| `units` | yes | Units Ordered. Non-negative integer. B2B units are already inside this total and are not added again. |
| `orders` | no | Total Order Items for that SKU that day. This is not a distinct-checkout count across SKUs. |
| `sales` | no | Ordered Product Sales in USD. Item sales, not tax or shipping. |
| `returns` | no | Optional received units that day, if the sales file carried them. Prefer `returns` rows when you have a reason. |
| `provisional` | no | True on the newest By Date day. That day stays `~` in the panel until a later snapshot includes a newer day. |

## `inventory`

| Field | Meaning |
| --- | --- |
| `sku` | Seller SKU. Required. |
| `asin`, `name` | From the inventory file. |
| `available` | Fulfillable / AFN fulfillable quantity. Missing means unknown, not zero. |
| `reserved` | Reserved at Amazon. |
| `unfulfillable` | Unsellable / unfulfillable. |
| `inbound_working` | Shipments created, not yet shipped. |
| `inbound_shipped` | Shipped toward FBA, not yet receiving. |
| `inbound_receiving` | Receiving at the fulfillment center. |
| `fc_transfer` | Units moving between Amazon fulfillment centers. Counted in US stock. Taken from the Restock report's FC transfer column. |
| `days_of_supply` | Amazon's days of supply, when the health or restock report has it. |
| `restock.quantity` | Amazon's recommended replenishment quantity. |
| `restock.ship_date` | Amazon's recommended ship date, `YYYY-MM-DD` or empty. |

## `shipments`

| Field | Meaning |
| --- | --- |
| `id` | Shipment ID. Required. |
| `name` | Shipment name. |
| `status` | `WORKING`, `READY_TO_SHIP`, `SHIPPED`, `IN_TRANSIT`, `DELIVERED`, `CHECKED_IN`, `RECEIVING`, `CLOSED`, `CANCELLED`, `ERROR`, or another Seller Central status. |
| `updated` | `YYYY-MM-DD` when the export has it. |
| `items[]` | `sku`, `shipped`, `received`. A Manage Shipments export has no SKU ids; `items` is then empty. |
| `units_expected`, `units_located` | Shipment-level quantities from Manage Shipments. Shown even when no SKU is known. |
| `destination`, `delivery_window` | Optional text from that same export. |

## `warehouse`

Smart Warehousing quantities are cases. `PLX-20-BX` is PotLifter, 20 units per case. `PRLX-8-BX` is ProLifter, 8 units per case. Override either in the mapping file.

| Field | Meaning |
| --- | --- |
| `sku` | 3PL SKU. Required. |
| `description` | Description from the portal. |
| `cases_on_hand` | Qty on hand, still in cases. |
| `units_per_case` | From the mapping file when that SKU is listed there. The portal's Qty per case column is often 1 and does not override the mapping. |
| `units_on_hand` | `cases_on_hand * units_per_case`. Null when the case size is unknown. |
| `cases_per_pallet`, `weight`, `case_upc` | Passed through when present. |
| `product_sku` | Seller SKU this row replenishes. |
| `mapped` | False when no mapping matched. Those cases are kept and are not added to a product. |

## `returns`

| Field | Meaning |
| --- | --- |
| `date` | Pacific date the return was received. |
| `sku` | Seller SKU. |
| `units` | Received units. |
| `reason` | Amazon's reason. Customer comments are never stored. |

## How the panel uses it

- US stock = FBA sellable + FC transfer + inbound shipped + inbound receiving + 3PL units. Inbound working is left in the 3PL count so it is not added twice. Reserved and unfulfillable units are shown separately and are not part of US stock. A product with no 3PL row still contributes its FBA units; the header total does not go blank because one product is missing.
- Weeks of cover = US stock / (daily demand × 7). Daily demand is the average of up to 30 contiguous complete days.
- FBA restock suggestion = the existing lead + safety + interval estimate, rounded to the nearest whole case (at least one case when any units are needed), then capped by whole cases on hand at the 3PL.
- A factory alert fires when US stock covers fewer days than `factory_lead_days` (default 180, about six months). The factory is in Shenzhen. Under half of that lead time, the alert is urgent.

## Example

Synthetic numbers only:

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-21T15:04:00+00:00",
  "seller": {
    "name": "PALE Trading Co.",
    "marketplace": "US",
    "currency": "USD",
    "timezone": "America/Los_Angeles",
    "warehouse": "Smart Warehousing, Sumner, WA",
    "factory": "Shenzhen"
  },
  "sources": {
    "sales": {"as_of": "2026-10-21T07:00:00+00:00", "available": true, "report": "Detail Page Sales and Traffic", "start": "2026-10-20", "end": "2026-10-20", "complete": true},
    "inventory": {"as_of": "2026-10-21T15:04:00+00:00", "available": true, "report": "FBA Manage Inventory / Inventory Health"},
    "shipments": {"as_of": "2026-10-21T15:04:00+00:00", "available": true, "report": "FBA shipments"},
    "warehouse": {"as_of": "2026-10-21T15:04:00+00:00", "available": true, "report": "Smart Warehousing inventory", "location": "Smart Warehousing, Sumner, WA"},
    "returns": {"available": false, "report": "FBA customer returns"}
  },
  "catalog": [
    {"sku": "POTLIFTER", "name": "PotLifter", "asin": "B0EXAMPLE01", "units_per_case": 20, "warehouse_sku": "PLX-20-BX"}
  ],
  "daily": [
    {"date": "2026-10-20", "sku": "POTLIFTER", "asin": "B0EXAMPLE01", "units": 5, "orders": 4, "sales": 129.95}
  ],
  "account_daily": [],
  "inventory": [
    {"sku": "POTLIFTER", "asin": "B0EXAMPLE01", "name": "PotLifter", "available": 70, "reserved": 5, "unfulfillable": 2, "inbound_working": 0, "inbound_shipped": 40, "inbound_receiving": 4, "days_of_supply": 25, "restock": {"quantity": 80, "ship_date": "2026-10-24"}}
  ],
  "shipments": [
    {"id": "FBA15DEMO01", "name": "October PotLifter", "status": "IN_TRANSIT", "updated": "2026-10-18", "items": [{"sku": "POTLIFTER", "asin": "B0EXAMPLE01", "shipped": 40, "received": 0}]}
  ],
  "warehouse": [
    {"sku": "PLX-20-BX", "description": "PotLifter", "cases_on_hand": 15, "units_per_case": 20, "units_on_hand": 300, "product_sku": "POTLIFTER", "mapped": true}
  ],
  "returns": []
}
```
