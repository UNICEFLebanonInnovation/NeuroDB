# Known divergences from v2 behaviour

Each entry is an intentional difference between v3 and the v2 pages, to be confirmed with the
product owner during the parallel run. Golden tests compare v3 values with v2 outputs on the same
data; anything not listed here must match.

| # | Area | v2 behaviour | v3 behaviour | Rationale |
|---|---|---|---|---|
| D1 | Funded-by filter | `AND funded_by = 'UNICEF'` commented out in 26 places; effectively never applied | Applied when `Database.is_funded_by_unicef` is true, otherwise not | One explicit flag instead of dead SQL |
| D2 | Zero target | NeuroReport query divided by `awp_target` (default 0) and could raise; dashboards showed `achieved = 0` | A zero or null target yields status "No target" and no percentage | Crash removed; no fake 0% |
| D3 | SUM_OVER_SUM on dashboards | Dashboard queries handled only SUM/AVERAGE/MAXIMUM/COUNT; ratio masters had no value | Ratio computed as numerator / denominator × 100 | Consistent with the HPM percentage display; confirm the ×100 |
| D4 | MINIMUM aggregation | Not implemented (choice existed) | Falls back to MAXIMUM of monthly sums until a rule is agreed | No v2 reference behaviour |
| D5 | Month source on import | `month_name` taken from the extract's `month` column (empty in the 2025 extract) | `month_of_reporting` when present, else `month`; rows with a year outside reporting year ±1 rejected and counted | Data-correctness fix |
| D6 | Emergency filter | `emergency` query parameter spliced into SQL | Validated to yes/no and bound | Security |
| D7 | Analytical feed | Pretty-printed JSON (indent=4) | Compact JSON, gzip | Performance |
| D8 | Import transaction | Delete then row-by-row insert, no transaction | Delete and bulk insert inside one transaction; "last updated" set only on success | Dashboards never show a half-imported database |
| D9 | eTools funding lines | Only the last funding reservation's line items kept | All reservations accumulated | Under-reporting fixed |
| D10 | Travel activity date | Trip start date | The activity's own date | Year attribution fixed |
| D11 | Trip sync range | Pages 45+ only | From page 1 | Stale older trips fixed |
| D12 | Partner staff contacts | Synced and served anonymously | Not displayed; kept only in the replica table pending the data-protection record | Personal data |
| D13 | Reporting level `GATEWAY` | Displayed as DISTRICT | Displayed as DISTRICT (unchanged) but flagged for cleanup | Admin clarity |
