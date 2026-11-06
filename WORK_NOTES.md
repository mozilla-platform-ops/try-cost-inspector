# POC work notes

## 2026-11-05

- Created the analysis collector, Azure subscription-rate pricing, GCP historical
  public Spot CPU/RAM pricing, and decision-task-based predictor.
- Use **task runs**, not attempts. Preserve task purpose and mark failed tests
  separately. Failed-test costs are included in overall totals.
- Keep physical hardware totals separate, grouped by worker pool.
- Unknown pricing is **Not determined**, never zero. Storage is not yet included;
  removed separate disk columns and reverted proposed Azure inventory grants.
- Defer GCP negotiated billing access/API enablement; use accessible data only.
- Predictor starts from optimized scheduled graph, not the larger label map;
  account for decision separately. GCP pricing uses the preceding 24-hour
  time-weighted Spot average; Azure daily-rate approximation stays labeled.
- Prediction duration history is sparse: exact name/pool median preferred,
  kind/pool fallback low confidence. Do not substitute maximum runtime for average.
- Added manually dispatched **POC Cost Analysis** and **POC Cost Predictor**,
  revision input, CSV artifacts and caveated Markdown workflow summaries.
- Gate both workflows on `.github/relops.json`, matching worker-images actor and
  rerun-actor validation and its current five-user allowlist.
- Publish code and unpriced public task history only; keep local credentials,
  billing-derived example prices and investigative snapshots outside this repo.

## GitHub workflow verification

- Repository: https://github.com/mozilla-platform-ops/try-cost-inspector (public,
  source code and unpriced public history only). GitHub rejected private creation.
- Existing dedicated Azure/GCP credentials configured as encrypted Actions
  secrets; FXCI subscription configured as an Actions variable. No IAM or APIs
  changed. GCP live catalog access remains unavailable under the current identity.
- First live runs exposed Treeherder's rejection of Python's default user agent.
  Added an explicit tool user agent and a regression test.
- 39 tests and actionlint pass. Both live workflow runs passed, including access
  validation and artifact generation:
  - Analysis: https://github.com/mozilla-platform-ops/try-cost-inspector/actions/runs/42130281685
  - Predictor: https://github.com/mozilla-platform-ops/try-cost-inspector/actions/runs/42130286072
- Downloaded both artifacts and verified that report.md includes storage,
  incomplete coverage, Spot assumptions, billing limitations, and predictor
  confidence/retry caveats. Missing GCP access is explicit, not treated as zero.

## Analysis public-pricing change

- User chose current public Spot webpage prices for analysis while GCP Billing
  API access remains deferred. The predictor's 24-hour pricing behavior is unchanged.
- Added price_gcp_public.py to read explicitly regional USD hourly CPU/RAM tables
  from Google's public Spot pricing page without authentication. Validate machine
  CPU/RAM against previously verified specifications; exclude Local SSD price tables,
  ignore monthly views, reject conflicting prices and changed formats.
- Preserve the original VM size in task rows; `-lssd` uses its verified matching
  CPU/RAM-only machine table. Never apply Iowa's default price to other regions.
- Removed GCP login/setup from the analysis workflow. Reports/CSV evidence label
  current prices, retrieval time, source, storage exclusion and historical/24-hour
  limitations. Missing webpage data remains Not determined.
- 44 tests and actionlint pass, including region matching, current-price provenance,
  storage exclusion, no Billing Catalog call for analysis, and unavailable webpage.

- Live verification passed: https://github.com/mozilla-platform-ops/try-cost-inspector/actions/runs/42132151484
  Downloaded the artifact and checked all 88 rows: 79 priced, including all 72 GCP
  runs. Public snapshot contains 263 supported regional rates and a retrieval
  timestamp. Current-price, historical/24-hour limitations and storage caveats
  are present in both report and CSV provenance.

## Summary-first report layout

- User requested summary first and complete individual task list at the bottom.
  Order: total known cost, provider totals, task-type totals, undetermined
  non-hardware costs, hardware breakdown, failed-task costs, caveats/data
  availability, then individual task details as the final section.
- Unknown physical hardware costs are counted separately from missing cloud or
  shared-worker costs. Added provider CSV and failed-task CSV summaries.
- Failed-task totals include all failed tasks, including failed builds; failed
  tests are an explicitly labeled subset. Infrastructure exceptions are separate.
  All are already included in the overall known subtotal. Predictions do not
  invent failure costs or observed hardware worker counts.
- 46 tests pass, including exact summary order, details-last layout, provider
  reconciliation, unknown hardware separation and failed-build cost coverage.

## Uncategorized labels and runtimes

- User requested **Uncategorized** for missing non-hardware cost assignment,
  including the previously Unknown infrastructure provider bucket. Unknown
  dollar values remain Not determined; existing task-purpose categories and
  known providers are retained. Physical hardware remains separate.
- Added total task runtime and duration coverage at the top, group runtimes in
  provider/type/failure summaries, and Uncategorized runtime totals by pool.
  Times are summed task execution, not elapsed push time. Added
  uncategorized-totals.csv; 46 tests pass, covering label and runtime separation.
- User requested another analysis query after five minutes; target dispatch
  2026-11-06T00:11:21Z for revision 38085f64c3c4. No pricing queries during wait.

- Five-minute pause honored; dispatched rerun at 2026-11-06T00:11:29Z.
  https://github.com/mozilla-platform-ops/try-cost-inspector/actions/runs/42135004892
  passed. Artifact confirms total task runtime 840.57 minutes, Uncategorized
  provider bucket 7 runs / 5.3307 minutes, and all unpriced non-hardware costs
  14 runs / 23.7732 minutes (including 7 Azure runs whose billing query failed).
  Azure prices remain unknown; this run did not capture the precise HTTP status.

## Azure 24-hour cache and public fallback

- User approved cached subscription rates first, clearly labeled public Spot
  fallback, and a 24-hour cache lifetime. Both workflows share a GitHub Actions
  cache and report-job concurrency group. Price files are Git-ignored.
- Subscription cache keys include subscription, requested dates and worker
  resource-group scope. Retail keys include SKU, region, OS and price class.
  TTL is strict <24 hours from successful retrieval; hits do not renew it.
- Fresh subscription cache has priority. If fresh public fallback covers all
  tasks and no subscription cache exists, reuse it without more Cost Management
  calls until expiry. Do not store failed queries as rate data.
- Billing uses the service principal's ARM token, with bounded retries for
  429/503 honoring the longest retry header. HTTP failures are now identified
  in report warnings; tokens are never logged/cached. Redirects are rejected.
- Current retail fallback matches Windows DevTestConsumption Spot, region/SKU,
  USD hourly units and primary meter. Never substitute Linux, ordinary Windows,
  reservations or ambiguous prices. Source/retrieval/cache status is in CSVs;
  fallback does not claim historical/24-hour-average/subscription-specific prices.
- 54 tests and actionlint pass. Live verification succeeded:
  - Cache population: https://github.com/mozilla-platform-ops/try-cost-inspector/actions/runs/42136839983
  - Predictor/shared-cache round trip: https://github.com/mozilla-platform-ops/try-cost-inspector/actions/runs/42136844646
  - Analysis cache hits: https://github.com/mozilla-platform-ops/try-cost-inspector/actions/runs/42137015361
  The final artifact confirms all seven Azure rows hit the restored subscription
  cache, keeping the original 2026-11-06T00:27:14.243937+00:00 retrieval timestamp
  and identical costs. All 79 supported cloud runs are priced. Live billing
  recovered, so retail fallback did not need to replace successful subscription
  rates; fallback selection was checked against a real retail API response and
  simulated billing failures. No IAM/API/credential changes were made.

## 2026-11-06 — Public GCP prices for prediction

- User requested current publicly available GCP Spot prices for estimates while
  Billing Catalog access remains deferred. Predictor now reuses analysis's
  regional public CPU/RAM price reader; no GCP login or Billing API calls.
- Both workflow artifacts save gcp-public-spot-rates.json. Prediction CSV notes
  include source/retrieval timestamp and explicitly reject historical/24-hour
  average claims. GCP pricing-window fields are blank. Missing regions/machines
  or webpage failures remain Not determined; Azure estimates are preserved.
- Standalone predictor defaults to public prices; explicit --gcp-catalog retains
  saved historical reproduction. Duration history and placement weighting remain
  unchanged; no storage or guessed overhead charges added.
- 55 tests and actionlint 1.7.7 pass. Downloaded Google's current public page and
  parsed 263 regional rates; all 20 GCP tasks in the saved 22-task example priced
  successfully, with a $0.20915283 GCP subtotal. Live workflow verification pending.
