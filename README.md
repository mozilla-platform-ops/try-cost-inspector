# Try cost inspector — POC

Two manually started GitHub Actions workflows:

- **POC Cost Analysis**: inspect actual task runs and estimate known compute costs.
- **POC Cost Predictor**: read the decision task's optimized graph and forecast compute costs from earlier run durations.

Open **Actions**, choose a workflow, select **Run workflow**, and enter a try
revision (12–40 hexadecimal characters) or a Treeherder try URL. A short revision
must resolve to exactly one push. Results appear in the workflow summary and a
14-day artifact containing `report.md`, task CSVs, category totals, pricing
evidence and (for analysis) a separate hardware summary. Analysis saves the
public regional price snapshot and its retrieval timestamp as
`gcp-public-spot-rates.json`.

## Report layout

The workflow summary and downloadable `report.md` start with the summary:

1. Total known cost and coverage (partial compute estimate; storage excluded).
2. Cost by cloud provider, including unknown and physical-hardware groups.
3. Cost by task type.
4. Undetermined non-hardware costs, grouped by worker pool.
5. Physical hardware breakdown by worker pool.
6. Cost of all failed tasks, failed tests as a subset, and infrastructure exceptions.

Caveats and data availability follow the summary. The full individual task list
is the final section. Failure subtotals are already included in the total and
must not be added together. Prediction reports do not forecast failure costs.
Artifacts include `provider-costs.csv`, `category-costs.csv`,
`hardware-totals.csv`, and (for analysis) `failed-task-costs.csv`.

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
| `GCP_CREDENTIALS` | Optional secret, predictor only | Dedicated service-account JSON for historical Cloud Billing Catalog access |

Azure needs only **Cost Management Reader** at subscription scope. No VM/disk
Reader grant is required. **Analysis fetches current regional prices directly
from [Google’s public Spot pricing page](https://cloud.google.com/spot-vms/pricing),
without GCP authentication or Billing API calls.** The page includes the data for
each region; we do not apply the visible default Iowa price to other regions.
Only verified supported CPU/RAM machine specifications are priced. For `-lssd`
variants, the matching CPU/RAM-only table is used, excluding the separate storage
price. Missing regions, unsupported machines or changed page structure leave
prices Not determined.

The **predictor** still needs working public Billing Catalog API access
and API enablement in its consumer project; Compute Viewer alone is insufficient
for that endpoint. The currently prepared GCP identity still needs that catalog
access. Billing API enablement and negotiated/account-specific pricing access
remain deferred; neither workflow enables APIs or changes IAM. Do not substitute
personal user refresh tokens. Missing authentication/provider data is reported
as **Not determined**. Cloud-login errors are allowed to reach the report so it
can explain incomplete coverage rather than hide it behind a login failure.

The repository contains unpriced task metadata from two public example pushes to
seed prediction durations. No cloud keys, OAuth tokens, subscription billing
exports or previously calculated subscription prices are committed. Without live
GCP access or a saved catalog covering the entire requested window, GCP predictor costs
remain unknown; the predictor never stretches an old rate across a new window.
Analysis instead uses current public webpage prices; they are not historical
prices and do not establish a preceding-24-hour average.

## Caveats included in every workflow report

- POC estimates, not invoices or full push costs. **Not determined is never zero**;
  coverage counts and known subtotals are shown explicitly.
- **Storage is not yet included.** Azure temporary storage bundled into its VM
  rate remains part of that rate.
- Spot instances are assumed. Only task execution runtime is counted; queue
  wait, boot/idle outside the task, network and shared overhead are excluded.
- Azure prices use daily, usage-weighted subscription billing rates, which lag
  and can blend OS/licensing classes or price changes. Analysis uses the latest
  day on/before execution, at most seven days old. Prediction uses the latest
  reported day at most seven days old, labeled as an approximation rather than
  an exact rolling 24-hour Spot average.
- GCP analysis uses **current** public regional CPU/RAM Spot prices retrieved
  when the workflow runs, not prices effective during the task or a 24-hour
  average. The predictor retains historical/24-hour Catalog pricing. Negotiated
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
- GCP prediction uses a time-weighted Spot CPU/RAM average over the preceding
  24 hours, integrating changes during that interval. Historical catalog queries
  crossing a Los Angeles calendar-month boundary currently remain unknown in
  the workflow until split-window collection is implemented.

## Local use

Python 3.11+ and the standard library. Azure CLI is needed for Azure pricing; log in with the dedicated service
identity above. GCP analysis needs only public HTTP access. The predictor needs
gcloud and catalog access for live GCP pricing.

```sh
python3 -m unittest -q
python3 workflow_report.py analysis 38085f64c3c4
python3 workflow_report.py predict 740941b754ca
```

Set `AZURE_SUBSCRIPTION_ID` for Azure queries. Outputs go to `reports/`.
`inspect_try.py`, `price_azure.py`, `price_gcp.py`, and `predict_try.py` remain
available separately for saved inputs and reproducible offline calculations.
