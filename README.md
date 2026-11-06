# Try cost inspector — POC

Two manually started GitHub Actions workflows:

- **POC Cost Analysis**: inspect actual task runs and estimate known compute costs.
- **POC Cost Predictor**: read the decision task's optimized graph and forecast compute costs from earlier run durations.

Open **Actions**, choose a workflow, select **Run workflow**, and enter a try
revision (12–40 hexadecimal characters) or a Treeherder try URL. A short revision
must resolve to exactly one push. Results appear in the workflow summary and a
14-day artifact containing `report.md`, task CSVs, category totals, pricing
evidence and (for analysis) a separate hardware summary. Both workflows save the
public regional price snapshot and its retrieval timestamp as
`gcp-public-spot-rates.json`.

## Report layout

The workflow summary and downloadable `report.md` start with the summary:

1. Total known cost and coverage (partial compute estimate; storage excluded).
2. Cost by cloud provider, including unknown and physical-hardware groups.
3. Cost by task type.
4. **Uncategorized** non-hardware costs, grouped by worker pool with total runtime.
5. Physical hardware breakdown by worker pool.
6. Cost of all failed tasks, failed tests as a subset, and infrastructure exceptions.

Caveats and data availability follow the summary. The full individual task list
is the final section. The top summary shows total task runtime; group summaries
also include runtime. These times are summed task execution, not elapsed push
time. Missing infrastructure is labeled **Uncategorized**; unknown dollar
amounts still read **Not determined**. Physical hardware stays separate. Failure subtotals are already included in the total and
must not be added together. Prediction reports do not forecast failure costs.
Artifacts include `provider-costs.csv`, `category-costs.csv`,
`hardware-totals.csv`, `uncategorized-totals.csv`, and (for analysis)
`failed-task-costs.csv`.

## Authorized users

Both workflows first run a separate **Verify RelOps User Access** job, using
`.github/relops.json` from `main`. The report job depends on its success. Both
`GITHUB_ACTOR` and the rerun's `GITHUB_TRIGGERING_ACTOR` must be allowed, matching
[worker-images user validation](https://github.com/mozilla-platform-ops/worker-images/blob/main/ci/check-authorized-user.ps1).
The allowlist is named **relops**, and contains the same users as the reference
`relsre.json`: jwmossmoz, markcor, aerickson, gibbsju, rcurranmoz.
No cloud authentication runs before this check passes.

## Cloud configuration

Repository Actions configuration:

| Setting | Kind | Purpose |
|---|---|---|
| `AZURE_CREDENTIALS` | Secret | JSON with `clientId`, `clientSecret`, `tenantId`, `subscriptionId` for the inspector service principal |
| `AZURE_SUBSCRIPTION_ID` | Variable | FXCI subscription to query |

Azure needs only **Cost Management Reader** at subscription scope for subscription rates. No VM/disk
Reader grant is required. **Both workflows fetch current regional prices directly
from [Google’s public Spot pricing page](https://cloud.google.com/spot-vms/pricing),
without GCP authentication or Billing API calls.** The page includes the data for
each region; we do not apply the visible default Iowa price to other regions.
Only verified supported CPU/RAM machine specifications are priced. For `-lssd`
variants, the matching CPU/RAM-only table is used, excluding the separate storage
price. Missing regions, unsupported machines or changed page structure leave
prices Not determined.

Neither workflow requires GCP credentials, gcloud or Billing API access.
Billing API enablement and negotiated/account-specific pricing access remain
deferred; neither workflow enables APIs or changes IAM. Missing provider data
is reported as **Not determined**. Azure login errors are allowed to reach the
report so it can explain incomplete coverage.

The repository contains unpriced task metadata from two public example pushes to
seed prediction durations. No cloud keys, OAuth tokens, subscription billing
exports or previously calculated subscription prices are committed. GCP estimates
use current public webpage prices, not historical prices or a preceding-24-hour
average. Both workflows preserve the source and retrieval timestamp in pricing
evidence; prediction CSV rows also include them in `price_note`.

## Caveats included in every workflow report

- POC estimates, not invoices or full push costs. **Not determined is never zero**;
  coverage counts and known subtotals are shown explicitly.
- **Storage is not yet included.** Azure temporary storage bundled into its VM
  rate remains part of that rate.
- Spot instances are assumed. Only task execution runtime is counted; queue
  wait, boot/idle outside the task, network and shared overhead are excluded.
- Azure prefers subscription billing rates with a **24-hour cache TTL**.
  Entries are keyed by subscription, query dates and worker-pool scope. Analysis
  selects a billing day on/before execution, at most seven days old. Billing can
  lag and blend OS/licensing classes or price changes. When unavailable, a
  current public Windows **DevTestConsumption** Spot rate can be used for these
  FXCI DevTest workers, matching region, SKU, OS, currency, hourly units and
  purchasing class. Ambiguous/missing matches remain unknown. Fallback is not
  negotiated or historical pricing, nor an exact 24-hour average.
- GCP analysis and prediction use **current** public regional CPU/RAM Spot prices retrieved
  when the workflow runs, not prices effective during the task or a 24-hour
  average. Negotiated
  project/account prices, discounts and credits are excluded. More accurate
  billing access is follow-up work.
- Hardware/shared signing and artifact workers, unsupported machines/regions,
  missing metadata and missing rates remain unknown.
- Classification maps known task metadata; Other/unknown needs refinement.
  Failed test means a failed task, not an individual failing test case.
- Analysis counts observed failed runs and retries. Failed-test and infrastructure
  exception subtotals are already included. Reused dependencies are not new runs.
  Running pushes are snapshots; rerun after completion. Expired/missing logs can
  leave machine metadata unavailable.
- Prediction uses the optimized scheduled graph plus decision, excludes reused
  dependencies, and forecasts one run per task. Later action tasks, retriggers
  and retries are excluded.
- Predicted duration is the completed-run median by name/pool; same-kind/pool
  fallbacks are labeled low confidence. Seed history is only two pushes and may
  include cached builds. It is not a trained or calibrated budgeting model.
- Machine and regional placement is inferred from historical samples. It can
  change. Missing history or any missing scenario price leaves that task unknown.
- GCP prediction applies current regional public CPU/RAM prices to predicted
  runtime, with the historical machine/region mix weighted by sample count.
  Its CSV pricing-window fields are blank for GCP; current prices do not imply
  a historical window. Azure retains its explicitly labeled approximations.

## Azure rate cache and throttling

Both workflows restore/save a shared GitHub Actions cache and serialize report
jobs under the same concurrency group. Each entry is valid for **less than
24 hours from its successful retrieval**. A cache hit does not extend its life;
expired, future-dated or unreadable entries are not used. GitHub may physically
retain old cache archives longer, but the tool rejects expired rates.

Source priority is fresh cached subscription rates, then a live subscription
query if needed, then current public Spot fallback. If a fresh public fallback
cache already covers all requested Azure tasks and no subscription cache exists,
reuse it without querying Cost Management again until it expires. Missing prices
are never replaced with zero, and failed queries are not cached as prices.

The query authenticates with the existing service principal. HTTP 429/503
responses use the longest supplied Azure/Retry-After delay plus a second; without
one, exponential backoff is used. At most three attempts and four minutes of
waiting per request are allowed. Authentication failures are not retried. Logs
and report warnings identify HTTP failures without exposing tokens.

Public fallback uses Microsoft's unauthenticated
[Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices).
Windows DevTest Spot pricing is explicitly selected for this FXCI subscription;
Linux rates, ordinary Windows consumption rates, reservations, non-USD and
non-hourly prices are not substituted. Each CSV row records source, retrieval
time and cache hit/miss status. A fallback rate has no historical billing date.
**A 24-hour cache lifetime is not a 24-hour average Spot price.**

For local runs the cache defaults to `.azure-price-cache/` (Git-ignored); set
`AZURE_PRICE_CACHE_DIR` to change its directory. Cache files contain only query
scope and rate evidence, never credentials or access tokens. The shared cache
is best-effort GitHub storage; a miss refreshes rates or uses the labeled fallback.

## Local use

Python 3.11+ and the standard library. Azure CLI is needed for subscription pricing; public fallback is unauthenticated.
Log in with the dedicated service
identity above. GCP analysis and prediction need only public HTTP access.

```sh
python3 -m unittest -q
python3 workflow_report.py analysis 38085f64c3c4
python3 workflow_report.py predict 740941b754ca
```

Set `AZURE_SUBSCRIPTION_ID` for Azure queries. Outputs go to `reports/`.
`inspect_try.py`, `price_azure.py`, `price_gcp.py`, and `predict_try.py` remain
available separately for saved inputs and reproducible offline calculations.

`predict_try.py` also defaults to current public GCP prices. Its explicit
`--gcp-catalog` option still accepts a saved historical catalog covering the
preceding 24 hours for offline reproduction; workflows use current prices.
