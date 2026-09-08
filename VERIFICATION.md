# Verification — September 8, 2026

Installed `patrik.amazon-seller` into the user's Omarchy shell, preserving existing bar placement. The local user service is active. Demo mode is explicitly labeled and remains separate from live business data.

## Passed

- 29 automated tests (including metric preference migration/persistence and daily chart coverage across DST): calendar boundaries/DST, selected-product totals, pending/cancelled orders, returns reconciliation, missing history, forecasts, alert lifecycle, notification deduplication, credential-file permissions, pagination, throttling, report recovery, arrival-window enrichment, and a 1,000-product snapshot.
- Running-shell visual checks: compact Attention panel, expanded Shipments and product details, bar count, and comparison cards. Settings was also checked during development.
- Service restart and retained demo data; no service exception in the inspected log.

Run tests from this directory with `python3 -B -m unittest discover -s tests -v`.

## Version 1.1 interaction checks

- QML parses successfully; updated compact and expanded panels render in the running shell.
- Keyboard activation of Orders changes the weekly bar to 10; Sales changes it to $1,148.61 in the current demo.
- Chart hover shows a Pacific calendar date and count (September 5: 7 units sold).
- Metric cards retain independent values when the bar selection changes; selected metric is highlighted.
- Existing demo mode and week selection survived installation. The installer backed up the previous plugin.

## Still requires live acceptance

Amazon credentials and application approval were not available. Live API permissions, report availability/schema compatibility, initial history backfill, and reconciliation against Seller Central totals have not been verified. The connector is implemented, but mocked tests are not proof of live compatibility.

Real desktop notification delivery under Do Not Disturb and sleep/wake, alternate themes/display scales, and full keyboard interaction remain manual acceptance checks. Demo mode deliberately sends no business notifications.

Use Settings → Connect Amazon or `amazon-seller setup` to enter credentials locally; do not paste secrets into chat. See README.md for registration and connection instructions.
