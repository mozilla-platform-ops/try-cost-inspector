import csv
import io
import json
import subprocess
import unittest
from unittest.mock import mock_open, patch

import price_azure as tool


SUBSCRIPTION = "108d46d5-fe9b-4850-9a7d-8c914aa6c1f0"
ROW = {"cloud_provider": "azure", "vm_size": "Standard_F8alds_v7", "region": "eastus",
       "worker_pool": "gecko-t/win11-64-25h2", "started": "2026-11-04T17:00:00Z",
       "duration_seconds": "1800"}


def billing(rows, next_link=None):
    return {"properties": {"columns": [{"name": name} for name in (
        "PreTaxCost", "UsageQuantity", "UsageDate", "Meter", "ResourceLocation", "Currency")],
        "rows": rows, "nextLink": next_link}}


class AzurePricingTests(unittest.TestCase):
    def test_failed_test_subtotal_reports_pricing_coverage(self):
        stream = io.StringIO()
        row = dict(ROW, failure_category="Failed test")
        writer = csv.DictWriter(stream, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
        for entries, expected in [
                ([[1, 10, 20261104, "F8alds v7 Spot", "us east", "USD"]], "$0.050000 USD"),
                ([], "Not determined")]:
            with self.subTest(expected=expected), \
                    patch("builtins.open", mock_open(read_data=stream.getvalue())), \
                    patch.object(tool, "cached_billing_rates", return_value=(tool.billing_rates(billing(entries)), "2026-11-05T00:00:00+00:00", "hit")), \
                    patch("sys.argv", ["price_azure.py", "tasks.csv", "--subscription", SUBSCRIPTION]), \
                    patch("sys.stdout", new_callable=io.StringIO), \
                    patch("sys.stderr", new_callable=io.StringIO) as stderr:
                tool.main()
                self.assertIn(f"Failed tests: 1 task runs; Azure runtime estimate {expected}", stderr.getvalue())
                self.assertIn("included in totals above", stderr.getvalue())

    def test_rate_and_cost(self):
        rates = tool.billing_rates(billing([
            [1, 10, 20261104, "F8alds v7 Spot", "us east", "USD"],
            [2, 10, 20261104, "F8alds v7 Spot", "us east", "USD"]]))
        row = tool.price_rows([ROW], rates)[0]
        self.assertEqual(row["azure_spot_usd_per_hour"], "0.15000000")
        self.assertEqual(row["azure_runtime_estimate_usd"], "0.07500000")
        self.assertEqual(row["azure_rate_date"], "2026-11-04")

    def test_no_guessing_or_future_rates(self):
        rates = tool.billing_rates(billing([
            [1, 10, 20261105, "F8alds v7 Spot", "us east", "USD"],
            [1, 10, 20261001, "F8alds v7 Spot", "us east", "USD"],
            [1, 10, 20261104, "F8alds v7", "us east", "USD"]]))
        row = tool.price_rows([ROW], rates)[0]
        self.assertEqual(row["azure_runtime_estimate_usd"], "")
        self.assertIn("no matching", row["azure_price_note"])

    def test_latest_rate_and_zero_cost(self):
        rates = tool.billing_rates(billing([
            [1, 10, 20261102, "F8alds v7 Spot", "us east", "USD"],
            [0, 10, 20261103, "F8alds v7 Spot", "us east", "USD"],
            [0, 0, 20261104, "F8alds v7 Spot", "us east", "USD"]]))
        self.assertEqual(tool.price_rows([ROW], rates)[0]["azure_runtime_estimate_usd"], "0.00000000")

    def test_validation(self):
        with self.assertRaises(ValueError):
            tool.billing_rates(billing([[1, 10, 20261104, "F8alds v7 Spot", "us east", "EUR"]]))
        for value in ["NaN", "Infinity", "-1", "invalid"]:
            with self.assertRaises(ValueError):
                tool.number(value)
        for location, expected in [("us south central", "southcentralus"),
                                   ("us west 3", "westus3"), ("uk south", "uksouth"),
                                   ("eastus2", "eastus2")]:
            self.assertEqual(tool.billing_region(location), expected)

    def test_unpriced_rows_stay_in_output(self):
        result = tool.price_rows([dict(ROW, cloud_provider="gcp"),
                                  dict(ROW, duration_seconds=""),
                                  dict(ROW, vm_size="")], {})
        self.assertEqual(len(result), 3)
        self.assertTrue(all(row["azure_runtime_estimate_usd"] == "" for row in result))

    def test_authenticated_query_pagination(self):
        first = billing([[1, 10, 20261103, "F8alds v7 Spot", "us east", "USD"]],
                        "https://management.azure.com/next")
        second = billing([[2, 10, 20261104, "F8alds v7 Spot", "us east", "USD"]])
        with patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "test-token")), \
                patch.object(tool, "request_json", side_effect=[first, second]) as run:
            payload = tool.fetch_billing([ROW], SUBSCRIPTION)
        self.assertEqual(len(payload["properties"]["rows"]), 2)
        args = run.call_args_list[0].args
        body = args[1]
        self.assertEqual(body["dataset"]["aggregation"]["hours"]["name"], "UsageQuantity")
        self.assertEqual(body["dataset"]["filter"]["and"][0]["dimensions"]["values"],
                         ["rg-tc-gecko-t-win11-64-25h2"])
        self.assertIn(SUBSCRIPTION, args[0])
        self.assertEqual(args[2], "test-token")


if __name__ == "__main__":
    unittest.main()
