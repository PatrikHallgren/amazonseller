# Amazon Seller for Omarchy

A native Omarchy panel for a US FBA seller: selected-product sales, returns, inventory, inbound shipments, replenishment estimates and attention notifications. Runs locally with Python 3.11+ and the installed Omarchy Quickshell shell. No additional Python dependencies.

## Install and try

```sh
git clone https://github.com/PatrikHallgren/amazonseller.git
cd amazonseller
python3 install.py install --demo
```

Click the shopping-bag bar widget. **DEMO** is always visible when synthetic data is selected; demo never sends notifications or contacts Amazon. Scroll the widget to select day/week/month/year. Click **Units sold**, **Orders**, or **Sales** in the panel to choose the bar metric; the highlighted card and time frame are saved across restarts. Hover over a product’s 14-day chart (or Tab through its days) for Pacific dates, units sold, and daily completeness. The panel supports product search, selection, favorites, details, assumptions, shipment links, alert acknowledgment and snooze. Ctrl+R requests refresh; Escape returns from details or closes the panel. Tab navigates controls.

## Connect your account

1. Verify you have a Professional US Seller Central account. Create a private SP-API developer profile through Amazon's Solution Provider Portal / Seller Central **Apps and Services → Develop Apps**.
2. Describe your own-company use: local sales/inventory/return analytics, replenishment estimates, listing and account-performance monitoring. Request **Amazon Fulfillment**, **Product Listing**, **Inventory and Order Tracking**, and **Selling Partner Insights**. Do not request buyer PII for this dashboard.
3. Complete Amazon's developer review and register a production private application with the approved roles. Self-authorize it. Save its **LWA client ID**, **LWA client secret**, **refresh token**, and your **seller ID / merchant token**.
4. Run `amazon-seller setup` in a terminal, or click **Connect Amazon** in the panel. It verifies active US marketplace participation and checks a listings read against the supplied seller ID before saving credentials and switching to live mode.
5. Check Settings → Connection or `amazon-seller status`. Each source reports its own readiness, permission errors and freshness. Keep the computer awake during initial history collection. Recent activity appears first; history loads in background.

Amazon decides access approval. The plugin cannot approve your developer application. Review Amazon's current terms/fees during registration. An active Seller Central login alone is not an API credential. Never paste secrets into a chat, source code or shell arguments.

Official references: [private registration](https://developer-docs.amazon.com/sp-api/docs/register-as-a-private-developer), [authorization/connection](https://developer-docs.amazon.com/sp-api/docs/connecting-to-the-selling-partner-api), [FBA reports](https://developer-docs.amazon.com/sp-api/docs/report-type-values-fba), [Orders API](https://developer-docs.amazon.com/sp-api/docs/orders-api), [account performance](https://developer-docs.amazon.com/sp-api/docs/report-type-values-performance).

## What the numbers mean

- US Pacific reporting time with Sunday-start weeks. Compare matching elapsed calendar portions of the previous period, clamping short months and leap dates. Date labels are shown in Pacific time.
- Units sold exclude pending/cancelled orders. Pending units appear separately. Orders containing multiple selected SKUs count once. Sales value includes selected items, less item discounts, excluding taxes/shipping. Unallocated discounts or missing prices make revenue incomplete.
- `~` means provisional/incomplete. `—` means no usable data. An unavailable source is never represented as confirmed zero. Recent order totals have at least Amazon's processing delay.
- Returns are **received units**, grouped by Amazon's return date. The report updates daily; this is not an immediate customer return-request feed. Returned units are not subtracted from today's sales.
- Inventory summaries are authoritative for quantities. Shipment details explain incoming units and are not added again. Preparing, shipped and receiving are separate. Arrival windows come from the newer inbound API when supplied; unknown dates are not inferred.
- Forecasts use up to 30 complete, contiguous days of sales. Defaults are 30 lead days + 14 safety days + 30 replenishment-interval days. Target = daily demand × their sum; suggested additional stock subtracts sellable plus shipped/receiving units. These are local estimates, not purchase orders. Amazon's recommendation is separate. New items, missing history and recent stockouts reduce confidence.
- Selected products control totals and product alerts. Urgent account issues remain visible. All active FBA products are included until an explicit selection is saved; subsequent newly discovered products are then hidden for review.

## Refresh and reliability

Orders 5 min; inventory 10 min; listings 15 min; shipments 30 min; reports 4 hours. Actual availability depends on Amazon. No hosted service or AWS queue is needed. Sync pauses during sleep and catches up after wake. Initial historical sales/returns are fetched from January 1 of the prior year, as available. Unavailable history stays labeled incomplete.

SQLite stores normalized products, order/SKU records, return rows, report jobs, coverage, alerts and checkpoints. Paginated orders resume across restarts. Duplicate reports are idempotent; older historical order rows do not override more recently updated records. Source failures retain cached data and retry with backoff. Report cancellation is not assumed to prove zero activity.

Attention notifications cover stockout risk, replenishment, listing/stranded inventory issues, shipment errors/discrepancies, unusual returned-unit counts and available account-health warnings. Routine sales/returns are quiet. Acknowledging means reviewed, not resolved. Snooze lasts 24h. Desktop Do Not Disturb remains controlled by Omarchy. Notifications are grouped and never expose customer information.

**Not monitored:** incoming buyer messages, the full support-case inbox, email-only notices. Use the customer inbox and Account Health links to review those directly. No replies, refunds, pricing/listing changes or other selling-account mutations are performed. Read/report endpoints are used; reports require asynchronous creation requests.

The inbound adapter uses Amazon's retained Fulfillment Inbound v0 read operations for Seller Central shipment discovery and quantities received, enriched with selected delivery windows from v2024-03-20. Orders use v2026-01-01; Reports v2021-06-30; FBA Inventory v1; Listings v2021-08-01. Unexpected API schemas fail visibly and retain cached data.

## CLI

```sh
amazon-seller status
amazon-seller snapshot
amazon-seller refresh
amazon-seller mode demo
amazon-seller mode live
amazon-seller settings
amazon-seller settings '{"period":"week","metric":"revenue","selected":["YOUR-SKU"]}'
amazon-seller settings '{"lead_days":35,"safety_days":14,"interval_days":30,"reviewed":true}'
amazon-seller alert ack 'stock:YOUR-SKU'
amazon-seller alert snooze 'stock:YOUR-SKU'
systemctl --user status amazon-seller.service
journalctl --user -u amazon-seller.service
```

Use `--root /tmp/your-test-directory` before the command to isolate test/demo data. CLI settings/snapshots use JSON. Credentials are only accepted interactively by `setup`; they are never included in snapshots. Both databases and credentials have owner-only permissions. Live/demo databases and product selections/overrides are separate; global planning preferences are shared.

Paths: plugin in `~/.config/omarchy/plugins/patrik.amazon-seller/`; settings/credentials in `~/.config/amazon-seller/`; databases, snapshots and backups in `~/.local/share/amazon-seller/`; service in `~/.config/systemd/user/amazon-seller.service`.

## Remove and recover

```sh
python3 ~/.config/omarchy/plugins/patrik.amazon-seller/install.py remove
```

This disables the widget/service, moves installed plugin/service files into a timestamped backup, and removes only its own CLI symlink. Business data and settings are retained. Reinstall to recover; no Omarchy packaged files are changed. Installation over an existing copy also backs up the plugin first.

## Development verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q seller
```

Live acceptance requires your approved credentials and a comparison to equivalent Seller Central exports with matching dates/statuses. Automated fixtures cover calculation and adapter behavior; they cannot establish your account's report availability or production totals.
