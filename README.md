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

Click the shopping-bag bar widget. **DEMO** is always visible when synthetic data is selected; demo never sends notifications or reads a snapshot. Scroll the widget to select day/week/month/year. Click **Units sold**, **Orders**, or **Sales** in the panel to choose the bar metric; the highlighted card and time frame are saved across restarts. Hover over a product’s 14-day chart (or Tab through its days) for Pacific dates, units sold, and daily completeness. The panel supports product search, selection, favorites, details, assumptions, shipment links, alert acknowledgment and snooze. Ctrl+R requests refresh; Escape returns from details or closes the panel. Tab navigates controls.

## Connect a snapshot

1. Create a private GitHub repository for the data, for example `PatrikHallgren/amazonseller-data`, with a file `snapshot.json` on `main`. A read-only token needs access to that repository’s contents and nothing else.
2. On the Omarchy computer, run `amazon-seller setup`. Choose GitHub (the default) or a local file. Press Enter to keep the default owner, repo, path, and branch. Paste a read-only token, or leave it blank to use `gh auth token` when the GitHub CLI is already logged in. The token is stored at `~/.config/amazon-seller/credentials.json` with owner-only permissions. It is never passed as a shell argument.
3. Check Settings → Connection or `amazon-seller status`. The footer shows the snapshot age, sales age, FBA age, and 3PL age. A failed refresh keeps the last good rows and says so.

For a local file instead of GitHub, choose `file` during setup and give the path. That is the right source for a manual drop or a test.

The panel polls about once an hour. The export only changes about once a day. Data older than 36 hours is marked stale (the `◌` on the bar and a stale line in the panel). Change that window with `amazon-seller settings '{"stale_hours":48}'` if a weekend gap should stay quiet. The age you see is the report time (`generated_at` and each source `as_of`), not the last poll.

## Reports the builder expects

Download these while signed in. CSV or the tab-separated Manage Inventory report are both accepted. Column headers can vary; the parser recognizes the Seller Central names below.

1. **Detail Page Sales and Traffic by Child Item** — Seller Central → **Reports → Business Reports → Detail Page Sales and Traffic**. Choose **By ASIN → Child** and download. A one-day file has no Date column; name it on the command line as `YYYY-MM-DD=file.csv`. A file that already has a Date column can be passed as-is. Units Ordered, Ordered Product Sales, and Total Order Items are read. B2B columns are a subset and are not added on top.
2. **Detail Page Sales and Traffic by Date** (optional) — same Business Reports page, **By Date**. Account totals. Used for the headline cards only when no child-item rows exist.
3. **Manage FBA Inventory** — **Reports → Fulfillment → Inventory → Manage FBA Inventory**. Supplies fulfillable, reserved, unfulfillable, and inbound working / shipped / receiving quantities.
4. **Restock recommendations or Inventory Health** — **Inventory → Inventory Planning** (restock download) or **Reports → Fulfillment → Inventory → Inventory Health**. Supplies days of supply and Amazon’s recommended replenishment quantity and ship date. Pass this with the Manage Inventory file; rows merge by SKU.
5. **FBA shipments** — **Inventory → Shipments** (the shipping queue). Prefer a contents download with one row per SKU: Shipment ID, SKU, Quantity Shipped, Quantity Received, Status. A queue file without SKUs is still listed, but it cannot be tied to a product.
6. **FBA customer returns** (optional) — **Reports → Fulfillment → Customer Concessions → FBA Customer Returns**. Customer comments are ignored.
7. **Smart Warehousing inventory** — the Sumner, WA portal export with SKU, Description, Qty on hand, Qty per case, Cases per pallet, Weight, and Case UPC. Qty on hand is **cases**. A JSON array of those fields works too.

Default 3PL mapping, overridden with `--mapping`:

| 3PL SKU | Product | Units per case |
| --- | --- | --- |
| `PLX-20-BX` | PotLifter (`POTLIFTER`) | 20 |
| `PRLX-8-BX` | ProLifter (`PROLIFTER`) | 8 |

If the Seller Central SKU is not `POTLIFTER` or `PROLIFTER`, point `sku` at the real seller SKU:

```json
{
  "PLX-20-BX": {"sku": "YOUR-POTLIFTER-SKU", "name": "PotLifter", "units_per_case": 20, "asin": ""},
  "PRLX-8-BX": {"sku": "YOUR-PROLIFTER-SKU", "name": "ProLifter", "units_per_case": 8, "asin": ""}
}
```

The mapping’s units per case win over the export’s Qty per case column.

```sh
amazon-seller build-snapshot \
  --sales 2026-10-20=child-item.csv \
  --sales by-date.csv \
  --inventory manage-fba-inventory.csv \
  --inventory restock.csv \
  --shipments shipments.csv \
  --warehouse smart-warehousing.csv \
  --returns fba-returns.csv \
  --mapping mapping.json \
  --previous snapshot.json \
  --output snapshot.json
```

`--previous` keeps older daily rows and replaces any day you downloaded again. Inventory, shipments, and 3PL stock are replaced by the files you pass; omit a flag to keep that section from the previous snapshot. The same command is `python3 tools/build_snapshot.py`. Write the output into the private data repo, not this one.

## What the numbers mean

- US Pacific reporting time with Sunday-start weeks. Week, month, and year compare the same elapsed dates. A daily snapshot cannot split a day in half, so the current day stays provisional (`~`) until that date is in the file. `—` means no usable data. An unavailable source is never shown as a confirmed zero.
- Units are Units Ordered. Orders are Total Order Items per SKU (an order with both products counts once on each SKU). Sales are Ordered Product Sales. Pending orders are not in the Business Report.
- Returns are received units by Amazon’s return date, when that report was included. They are not subtracted from sales.
- FBA quantities come from Manage Inventory. Amazon’s days of supply and recommended replenishment come from the restock / health report and are not the same as this panel’s suggestion.
- US stock = FBA sellable + inbound shipped + inbound receiving + Sumner units. Inbound working stays in the 3PL count until Amazon marks it shipped, so it is not added twice. Reserved and unfulfillable units are shown on the product and are not part of US stock. Weeks of cover = US stock / (daily demand × 7).
- The FBA send quantity starts from the same local estimate as before: daily demand × (lead + safety + interval), minus FBA sellable and shipped/receiving units. Defaults are 30 + 14 + 30 days. That unit estimate is rounded to the nearest whole case, and at least one case when any units are needed, then capped by whole cases on hand in Sumner.
- A separate alert fires when US stock covers fewer days than the factory lead time. The default is 180 days, about six months, for a reorder from Shenzhen. Set `factory_lead_days` in Settings or with `amazon-seller settings`. Under half of that lead time the alert is urgent. It only runs once 3PL quantities are present, so a missing warehouse file does not pretend the US total is known.
- Forecasts still use up to 30 complete, contiguous days. Gaps are not treated as zero demand. These are local estimates, not purchase orders.

Selected products control totals and product alerts. All active products are included until an explicit selection is saved.

## Refresh and reliability

The snapshot is polled about hourly. A failed poll keeps the cached rows, records the error, and retries with backoff. Stale means the snapshot or the sales, FBA, or 3PL `as_of` is over 36 hours old, not merely that one poll failed.

SQLite stores the normalized snapshot, alerts, and checkpoints. Duplicate days in a rebuilt snapshot replace that day only. Source failures do not wipe products.

Attention notifications cover FBA stockout risk, 3PL replenishment, Shenzhen factory reorder, shipment problems, and unusual returned-unit counts when returns were provided. Acknowledging means reviewed, not resolved. Snooze lasts 24 hours. Desktop Do Not Disturb remains controlled by Omarchy. Notifications are grouped and never include customer comments.

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
