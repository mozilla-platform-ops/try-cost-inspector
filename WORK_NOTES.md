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
