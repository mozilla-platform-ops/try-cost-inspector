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
