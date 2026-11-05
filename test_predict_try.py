import copy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

import predict_try as tool
from test_price_gcp import catalog


AS_OF = datetime(2026, 11, 5, 23, tzinfo=timezone.utc)
TASK = {"provisionerId": "gecko-t", "workerType": "linux", "metadata": {"name": "test-example"},
        "tags": {"kind": "test", "test-suite": "xpcshell"}}
DECISION = {"provisionerId": "gecko-1", "workerType": "decision", "metadata": {"name": "Decision"}}
SAMPLE = {"task_id": "old", "run_id": "0", "name": "test-example", "worker_pool": "gecko-t/linux",
          "task_kind": "test", "state": "completed", "resolved": "2026-11-05T20:00:00Z",
          "duration_seconds": "1800", "cloud_provider": "gcp", "region": "us-central1",
          "vm_size": "c3d-standard-16-lssd"}


def prices():
    data = catalog()
    data.update(requested_start="2026-11-04T23:00:00Z", requested_end="2026-11-05T23:00:00Z")
    for sku in data["skus"]:
        sku["pricingInfo"][0]["effectiveTime"] = "2026-11-04T08:00:00Z"
    return data


def predict(history, data=None, graph=None):
    return tool.predict_rows(graph or {"new": {"task": TASK}}, DECISION, "decision", history,
                             data or prices(), AS_OF)


class PredictionTests(unittest.TestCase):
    def test_exact_history_and_no_reused_dependency_or_duplicate_sample(self):
        rows = predict([SAMPLE, SAMPLE])
        self.assertEqual(len(rows), 2)  # Scheduled graph plus decision only.
        row = next(r for r in rows if r["task_id"] == "new")
        self.assertEqual(row["history_samples"], 1)
        self.assertEqual(row["predicted_duration_seconds"], "1800.000")
        self.assertEqual(row["average_spot_usd_per_hour"], "0.22400000")
        self.assertEqual(row["predicted_compute_usd"], "0.11200000")
        self.assertIn("24-hour", row["price_note"])
        self.assertIn("storage is not yet included", row["notes"])

    def test_future_and_failed_runs_excluded_and_missing_history_stays_unknown(self):
        rows = predict([dict(SAMPLE, state="failed"),
                        dict(SAMPLE, task_id="future", resolved="2026-11-06T00:00:00Z"),
                        dict(SAMPLE, task_id="new")])
        self.assertTrue(all(r["predicted_compute_usd"] == "" for r in rows))
        self.assertTrue(all(r["history_samples"] == 0 for r in rows))

    def test_kind_pool_fallback_and_incomplete_price_window(self):
        sample = dict(SAMPLE, name="different-test")
        row = next(r for r in predict([sample]) if r["task_id"] == "new")
        self.assertIn("low confidence", row["history_basis"])
        data = prices()
        data["requested_start"] = "2026-11-05T00:00:00Z"
        row = next(r for r in predict([sample], data) if r["task_id"] == "new")
        self.assertEqual(row["predicted_compute_usd"], "")
        self.assertIn("outside", row["price_note"])

    def test_price_changes_are_time_weighted_over_24_hours(self):
        data = prices()
        second = copy.deepcopy(data["skus"][0]["pricingInfo"][0])
        second["effectiveTime"] = "2026-11-05T11:00:00Z"
        second["pricingExpression"]["tieredRates"][0]["unitPrice"]["nanos"] *= 2
        data["skus"][0]["pricingInfo"].append(second)
        row = next(r for r in predict([SAMPLE], data) if r["task_id"] == "new")
        self.assertEqual(row["average_spot_usd_per_hour"], "0.30400000")
        self.assertEqual(row["predicted_compute_usd"], "0.15200000")

    def test_missing_machine_scenario_does_not_silently_underestimate(self):
        unknown = dict(SAMPLE, task_id="another", vm_size="unknown")
        row = next(r for r in predict([SAMPLE, unknown]) if r["task_id"] == "new")
        self.assertEqual(row["predicted_compute_usd"], "")

    def test_azure_rate_requires_matching_window_and_preserves_method(self):
        sample = dict(SAMPLE, cloud_provider="azure", region="eastus", vm_size="Standard_F8alds_v7")
        rate = {"vm_size": sample["vm_size"], "region": "eastus",
                "window_start": "2026-11-04T23:00:00Z", "window_end": AS_OF.isoformat(),
                "usd_per_hour": "0.1", "price_note": "Approximate daily blended subscription rate"}
        rows = tool.predict_rows({"new": {"task": TASK}}, DECISION, "decision", [sample], {}, AS_OF, [rate])
        row = next(r for r in rows if r["task_id"] == "new")
        self.assertEqual(row["predicted_compute_usd"], "0.05000000")
        self.assertIn("Approximate", row["price_note"])
        rate["window_start"] = "2026-11-05T00:00:00Z"
        rows = tool.predict_rows({"new": {"task": TASK}}, DECISION, "decision", [sample], {}, AS_OF, [rate])
        self.assertEqual(next(r for r in rows if r["task_id"] == "new")["predicted_compute_usd"], "")

    def test_short_revision_resolution_rejects_ambiguity(self):
        prefix = "740941b754ca"
        full = prefix + "0" * 28
        with patch.object(tool.inspection, "get_json", return_value={"results": [{"revision": full}]}):
            self.assertEqual(tool.resolve_revision(prefix), full)
        with patch.object(tool.inspection, "get_json", return_value={"results": [{"revision": full}, {"revision": prefix + "1" * 28}]}):
            with self.assertRaises(ValueError):
                tool.resolve_revision(prefix)


if __name__ == "__main__":
    unittest.main()
