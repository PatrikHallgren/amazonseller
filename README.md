# Amazon Seller for Omarchy

A native Omarchy panel for PALE Trading Co.: PotLifter and ProLifter sales, returns, FBA inventory, inbound shipments, Smart Warehousing stock in Sumner, WA, and replenishment alerts. Runs locally with Python 3.11+ and the installed Omarchy Quickshell shell. No additional Python dependencies, and no Amazon SP-API application.

Sales and inventory come from a daily JSON snapshot. An assistant that is already signed in to Seller Central and the 3PL portal downloads the reports, builds `snapshot.json`, and publishes it to a **private** repository. This repository is public. Do not commit snapshots, exports, or tokens here.

The snapshot contract is [SCHEMA.md](SCHEMA.md).

## Install and try

```sh
git clone https://github.com/PatrikHallgren/amazonseller.git
cd amazonseller
python3 install.py install --demo
```

Click the shopping-bag bar widget. **DEMO** is always visible when synthetic data is selected; demo never sends notifications or reads a snapshot. Scroll the widget to select Weekly, Monthly, or Yearly. Click **Units sold** or **Sales** in the panel to choose the bar metric; the highlighted card and time frame are saved across restarts. A saved Today / day period becomes Weekly, and a saved Orders or Returns metric becomes Units sold. Hover over a product’s 14-day chart (or Tab through its days) for Pacific dates, units sold, and daily completeness. The panel supports product search, selection, favorites, details, assumptions, shipment links, alert acknowledgment and snooze. Ctrl+R requests refresh; Escape returns from details or closes the panel. Tab navigates controls.

## Connect a snapshot

1. Create a private GitHub repository for the data, for example `PatrikHallgren/amazonseller-data`, with a file `snapshot.json` on `main`. A read-only token needs access to that repository’s contents and nothing else.
2. On the Omarchy computer, run `amazon-seller setup`. Choose GitHub (the default) or a local file. Press Enter to keep the default owner, repo, path, and branch. Paste a read-only token, or leave it blank to use `gh auth token` when the GitHub CLI is already logged in. The token is stored at `~/.config/amazon-seller/credentials.json` with owner-only permissions. It is never passed as a shell argument.
3. Check Settings → Connection or `amazon-seller status`. The footer shows the snapshot age, sales age, FBA age, and 3PL age. A failed refresh keeps the last good rows and says so.

For a local file instead of GitHub, choose `file` during setup and give the path. That is the right source for a manual drop or a test.

The panel polls about once an hour. The export only changes about once a day. Data older than 36 hours is marked stale (the `◌` on the bar and a stale line in the panel). Change that window with `amazon-seller settings '{"stale_hours":48}'` if a weekend gap should stay quiet. The age you see is the report time (`generated_at` and each source `as_of`), not the last poll.

## Reports the builder expects

Download these while signed in. CSV or the tab-separated Manage Inventory report are both accepted. Column headers can vary; the parser recognizes the Seller Central names below.

1. **Detail Page Sales and Traffic by Child Item** — Seller Central → **Reports → Business Reports → Detail Page Sales and Traffic**. Choose **By ASIN → Child** and download. A one-day file has no Date column. Pass it as `YYYY-MM-DD=file.csv`, or name it `sales_by_child_item_YYYY-MM-DD.csv` and pass the file or a folder of those files for a backfill. A multi-day Child Item download also has no Date column; pass it as `YYYY-MM-DD..YYYY-MM-DD=file.csv` (or a filename that contains both dates). That file is a range total used to cross-check daily rows. It is never written as one day of history. A file that already has a Date column can be passed as-is. Units Ordered, Ordered Product Sales, and Total Order Items are read. B2B columns are a subset and are not added on top.
2. **Detail Page Sales and Traffic by Date** (optional) — same Business Reports page, **By Date**. This file is the account total for every product together. A day with no Child Item row uses these totals on the week, month, and year cards, labeled combined / all products. A 90-day By Date file still fills those cards, and the previous-period comparison, when only the last 30 days were backfilled per product. The reported sales start is the earlier of the two series. Per-product charts and restock forecasts stay on the Child Item days. Amazon still revises the newest day in this report, so that day stays provisional (`~`) until a later snapshot contains a newer day. The builder keeps up to 731 days (about two years) of this file. Week and month compare through the last finished sales day, the same number of days in the previous period. Year shows the year-to-date total and, until the prior year is in the file, says how many days of history exist and that there is no comparison yet.
3. **Manage FBA Inventory** — **Reports → Fulfillment → Inventory → Manage FBA Inventory**. Supplies fulfillable, reserved, unfulfillable, and inbound working / shipped / receiving quantities.
4. **Restock recommendations or Inventory Health** — **Inventory → Inventory Planning** (restock download) or **Reports → Fulfillment → Inventory → Inventory Health**. Supplies days of supply and Amazon’s recommended replenishment quantity and ship date. Pass this with the Manage Inventory file; rows merge by SKU.
5. **FBA shipments** — **Inventory → Shipments**, then the Manage Shipments download. Columns used: Shipment name, Status, Created, Last updated, Destination, Delivery window, SKUs (a count, not seller SKUs), Units expected, Units located, and Shipment ID. Those shipment-level quantities are shown even when the file has no SKU ids. A contents file with one row per SKU (Shipment ID, SKU, Quantity Shipped, Quantity Received) is still the one that ties units to a product.
6. **FBA customer returns** (optional) — **Reports → Fulfillment → Customer Concessions → FBA Customer Returns**. Customer comments are ignored.
7. **Smart Warehousing inventory** — the Sumner, WA portal export with SKU, Description, Qty on hand, Qty per case, Cases per pallet, Weight, and Case UPC. Qty on hand is **cases**. CSV, JSON, and the portal's HTML `.xls` download are read. A binary Excel workbook is rejected with a message to export CSV (or save as Web Page) instead of a traceback. The Qty per case column often says 1; the mapping's units per case is what the snapshot uses.
8. **Morning attention file** (optional) — a small JSON or CSV you write from the browser. It is not an Amazon download. Omit the file and Attention simply has no rows from it. See [SCHEMA.md](SCHEMA.md) for the columns `type`, `title`, `detail`, `date`, `url`, `needs_response_by`. Buyer messages older than about 20 hours are urgent. The reply target is 24 hours.

Default 3PL mapping, overridden with `--mapping`:

| 3PL SKU | Product | Seller SKU | ASIN | Units per case |
| --- | --- | --- | --- | --- |
| `PLX-20-BX` | PotLifter | `856455001068` | `B002ECFFJ8` | 20 |
| `PRLX-8-BX` | ProLifter | set this | set this | 8 |

PotLifter's seller SKU and ASIN are filled in. ProLifter's seller SKU is numeric as well; copy it from Seller Central → Inventory (the SKU column, not the ASIN) into a mapping file. Until that SKU is set, ProLifter cases still convert at 8 units but they are not joined to the FBA row.

```json
{
  "PLX-20-BX": {"sku": "856455001068", "name": "PotLifter", "units_per_case": 20, "asin": "B002ECFFJ8"},
  "PRLX-8-BX": {"sku": "YOUR-PROLIFTER-SKU", "name": "ProLifter", "units_per_case": 8, "asin": "YOUR-PROLIFTER-ASIN"}
}
```

Pass it with `--mapping mapping.json`. Smart Warehousing's Qty per case column says 1 for these cases. The mapping's 20 and 8 override that column, and the builder says so when they disagree.

```sh
amazon-seller build-snapshot \
  --sales ./sales-by-day \
  --sales 2026-10-01..2026-10-20=child-item-range.csv \
  --sales by-date.csv \
  --inventory manage-fba-inventory.csv \
  --inventory restock.csv \
  --shipments shipments.csv \
  --warehouse smart-warehousing.csv \
  --returns fba-returns.csv \
  --actions morning-attention.json \
  --mapping mapping.json \
  --previous snapshot.json \
  --output snapshot.json
```

`--previous` keeps older daily rows and replaces any day you downloaded again. Inventory, shipments, and 3PL stock are replaced by the files you pass; omit a flag to keep that section from the previous snapshot. The same command is `python3 tools/build_snapshot.py`. Write the output into the private data repo, not this one.

To give Yearly a prior-year comparison, download **Detail Page Sales and Traffic by Date** again with a custom range from **1 January of last year through yesterday** (the builder keeps 731 days, about two years, so a range back to the same month last year is enough). Pass that CSV as another `--sales` file with `--previous snapshot.json`. Child Item files can stay on the recent days you already backfilled; restock still uses only those per-product days. With sales through 2026-10-20, the file to add is By Date for **2025-01-01 through 2026-10-20**. Week and month already compare once the snapshot includes every day in those shorter windows.

## What the numbers mean

- US Pacific reporting time with Sunday-start weeks. Weekly, Monthly, and Yearly compare the same elapsed dates through the last finished sales day (a daily snapshot has no row for today). The newest day in the By Date report stays provisional (`~`) until a later snapshot includes a newer day. That mark does not by itself make the comparison incomplete. Days that have Child Item rows use those rows. Earlier days use the By Date account total, and the card says those days are combined, all products. With only part of the year on file, Yearly shows the year-to-date total and a note such as “Only 90 days of history; no comparison yet”. `—` means no usable data. An unavailable returns report is left off the settings list.
- Units are Units Ordered. Orders are Total Order Items per SKU (an order with both products counts once on each SKU). Sales are Ordered Product Sales. Pending orders are not in the Business Report.
- Returns are received units by Amazon’s return date, when that report was included. They are not subtracted from sales.
- FBA quantities come from Manage Inventory. Amazon’s days of supply and recommended replenishment come from the restock / health report and are not the same as this panel’s suggestion.
- US stock = FBA sellable + units transferring between fulfillment centers (FC transfer) + inbound shipped + inbound receiving + Sumner units. Inbound working stays in the 3PL count until Amazon marks it shipped, so it is not added twice. Reserved and unfulfillable units are shown on the product and are not part of US stock. A product that is missing from the 3PL file still counts its FBA units in the US total; the header says when 3PL is not linked for every selected product. Weeks of cover = US stock / (daily demand × 7).
- Manage FBA Inventory supplies fulfillable quantity. The Restock report supplies FC transfer, reserved, and inbound columns when Manage Inventory does not include them. Restock's Available does not replace Manage Inventory's fulfillable quantity. `none`, `n/a`, `-`, and blank cells are empty; one unreadable cell is a warning and does not stop the build.
- A new listing with no sales history does not raise a stockout alert.
- The FBA send quantity starts from the same local estimate as before: daily demand × (lead + safety + interval), minus FBA sellable and shipped/receiving units. Defaults are 30 + 14 + 30 days. That unit estimate is rounded to the nearest whole case, and at least one case when any units are needed, then capped by whole cases on hand in Sumner.
- A separate alert fires when US stock covers fewer days than the factory lead time. The default is 180 days, about six months, for a reorder from Shenzhen. Set `factory_lead_days` in Settings or with `amazon-seller settings`. Under half of that lead time the alert is urgent. It only runs once 3PL quantities are present, so a missing warehouse file does not pretend the US total is known.
- Forecasts still use up to 30 complete, contiguous days. Gaps are not treated as zero demand. These are local estimates, not purchase orders.

Selected products control totals and product alerts. All active products are included until an explicit selection is saved.

## Refresh and reliability

The snapshot is polled about hourly. A failed poll keeps the cached rows, records the error, and retries with backoff. Stale means the snapshot or the sales, FBA, or 3PL `as_of` is over 36 hours old, not merely that one poll failed.

SQLite stores the normalized snapshot, alerts, and checkpoints. Duplicate days in a rebuilt snapshot replace that day only. Source failures do not wipe products.

Attention notifications cover FBA stockout risk, 3PL replenishment, Shenzhen factory reorder, shipment problems, unusual returned-unit counts when returns were provided, and any action items or buyer messages in the morning file. Acknowledging means reviewed, not resolved. Snooze lasts 24 hours. Desktop Do Not Disturb remains controlled by Omarchy. Notifications are grouped and never include customer comments. A snapshot with no `actions` section adds no error.

No replies, refunds, pricing, or other selling-account changes are made. The panel only reads the snapshot.

## CLI

```sh
amazon-seller setup
amazon-seller status
amazon-seller snapshot
amazon-seller refresh
amazon-seller build-snapshot --help
amazon-seller mode demo
amazon-seller mode live
amazon-seller settings
amazon-seller settings '{"period":"week","metric":"revenue","selected":["POTLIFTER"]}'
amazon-seller settings '{"lead_days":35,"safety_days":14,"interval_days":30,"factory_lead_days":180,"reviewed":true}'
amazon-seller alert ack 'factory:POTLIFTER'
amazon-seller alert snooze 'stock:POTLIFTER'
systemctl --user status amazon-seller.service
journalctl --user -u amazon-seller.service
```

Use `--root /tmp/your-test-directory` before the command to isolate test data. Credentials are only accepted interactively by `setup`. Both databases and credentials have owner-only permissions. Live and demo databases and product selections are separate; planning preferences, including the Shenzhen lead time, are shared.

Paths: plugin in `~/.config/omarchy/plugins/patrik.amazon-seller/`; settings and credentials in `~/.config/amazon-seller/`; databases and the panel’s view in `~/.local/share/amazon-seller/`; service in `~/.config/systemd/user/amazon-seller.service`.

## Legacy SP-API

Not required, and not the default. If you already have an approved private SP-API application, `amazon-seller setup --spapi` stores the LWA credentials and `amazon-seller settings '{"source":"spapi"}'` uses the old sync. New setup never asks for those keys.

## Remove and recover

```sh
python3 ~/.config/omarchy/plugins/patrik.amazon-seller/install.py remove
```

This disables the widget and service, moves installed plugin and service files into a timestamped backup, and removes only its own CLI symlink. Business data and settings are retained. Reinstall to recover; no Omarchy packaged files are changed. Installation over an existing copy also backs up the plugin first.

## Development verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q seller
```

Fixtures and demo products are synthetic. They do not prove a particular Seller Central export will match a given day’s totals; compare a built snapshot to the files you downloaded before trusting it.
