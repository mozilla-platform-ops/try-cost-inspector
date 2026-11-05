import io
import json
import subprocess
import unittest
from decimal import Decimal
from unittest.mock import patch

import price_gcp as tool


ROW = {"cloud_provider": "gcp", "vm_size": "c3d-standard-16-lssd", "region": "us-central1",
       "started": "2026-11-04T17:00:00Z", "resolved": "2026-11-04T18:00:00Z",
       "duration_seconds": "3600", "azure_runtime_estimate_usd": ""}


def price(unit_price, base, factor, effective="2026-11-04T08:00:00Z"):
    return {"effectiveTime": effective, "pricingExpression": {
        "baseUnit": base, "baseUnitConversionFactor": factor,
        "tieredRates": [{"startUsageAmount": 0, "unitPrice": {
            "currencyCode": "USD", "units": "0", "nanos": unit_price}}]}}


def catalog():
    skus = []
    for component, sku_id, info in [
            ("cpu", tool.SKUS["c3d"][0], price(10_000_000, "s", 3600)),
            ("ram", tool.SKUS["c3d"][1], price(1_000_000, "By.s", 2**30 * 3600))]:
        skus.append({"skuId": sku_id, "serviceRegions": ["us-central1"],
                     "category": {"usageType": "Preemptible"}, "pricingInfo": [info]})
    return {"requested_start": "2026-11-04T16:00:00Z",
            "requested_end": "2026-11-04T19:00:00Z", "skus": skus}


class GcpPricingTests(unittest.TestCase):
    def test_eight_cpu_c3_and_c3d_sizes(self):
        for size, family in (("c3-standard-8", "c3"), ("c3d-standard-8", "c3d"),
                             ("c3d-standard-8-lssd", "c3d")):
            with self.subTest(size=size):
                data = catalog()
                for sku, sku_id in zip(data["skus"], tool.SKUS[family]):
                    sku["skuId"] = sku_id
                row = tool.price_rows([dict(ROW, vm_size=size)], data)[0]
                self.assertEqual(row["gcp_runtime_estimate_usd"], "0.11200000")

    def test_catalog_fetch_follows_pages_and_requests_historical_window(self):
        data = catalog()
        pages = [{"skus": data["skus"][:1], "nextPageToken": "next-page"},
                 {"skus": data["skus"][1:]}]
        with patch.object(tool.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "test-token\n")), \
                patch.object(tool, "urlopen", side_effect=[io.StringIO(json.dumps(page)) for page in pages]) as fetch:
            result = tool.fetch_catalog([ROW])
        self.assertEqual(len(result["skus"]), 2)
        self.assertIn("startTime=2026-11-04", fetch.call_args_list[0].args[0].full_url)
        self.assertIn("pageToken=next-page", fetch.call_args_list[1].args[0].full_url)

    def test_cpu_ram_cost_excludes_storage(self):
        data = catalog()
        data["skus"].append({"skuId": "unused-storage-sku"})
        row = tool.price_rows([ROW], data)[0]
        self.assertEqual(row["gcp_runtime_estimate_usd"], "0.22400000")
        self.assertEqual(row["gcp_spot_usd_per_hour"], "0.22400000")
        self.assertEqual(row["total_known_cost_usd"], "0.22400000")
        self.assertFalse(any("disk_cost" in key or "ssd" in key for key in row))
        self.assertEqual(row["total_cost_status"], "Partial: storage is not yet included")
        self.assertEqual({part["component"] for part in json.loads(row["gcp_rate_components"])}, {"cpu", "ram"})

    def test_price_change_within_run_is_split(self):
        data = catalog()
        data["skus"][0]["pricingInfo"].append(price(20_000_000, "s", 3600, "2026-11-04T17:30:00Z"))
        row = tool.price_rows([ROW], data)[0]
        self.assertEqual(row["gcp_runtime_estimate_usd"], "0.30400000")

    def test_memory_units_are_converted_using_base_units(self):
        info = price(1_000_000, "By.s", 10**9 * 3600)
        self.assertEqual(tool.hourly_price(info, "ram", 1), Decimal("0.001073741824"))

    def test_mismatch_stays_unpriced(self):
        cases = [dict(ROW, region="europe-west1"), dict(ROW, vm_size="unverified-size"),
                 dict(ROW, started="2026-11-04T15:00:00Z"),
                 dict(ROW, duration_seconds="-1"), dict(ROW, duration_seconds=""),
                 dict(ROW, cloud_provider="azure", azure_runtime_estimate_usd="0.12345678")]
        result = tool.price_rows(cases, catalog())
        self.assertTrue(all(row["gcp_runtime_estimate_usd"] == "" for row in result))
        self.assertEqual(result[-1]["azure_runtime_estimate_usd"], "0.12345678")
        self.assertEqual(cases[-1]["azure_runtime_estimate_usd"], "0.12345678")

    def test_missing_component_and_future_price_stay_unpriced(self):
        data = catalog()
        data["skus"].pop()
        self.assertEqual(tool.price_rows([ROW], data)[0]["gcp_runtime_estimate_usd"], "")
        data = catalog()
        data["skus"][0]["pricingInfo"][0]["effectiveTime"] = "2026-11-04T17:30:00Z"
        self.assertEqual(tool.price_rows([ROW], data)[0]["gcp_runtime_estimate_usd"], "")

    def test_wrong_units_currency_or_usage_type_are_rejected(self):
        for change in ("currency", "unit", "usage"):
            data = catalog()
            sku = data["skus"][0]
            expression = sku["pricingInfo"][0]["pricingExpression"]
            if change == "currency":
                expression["tieredRates"][0]["unitPrice"]["currencyCode"] = "EUR"
            elif change == "unit":
                expression["baseUnit"] = "unknown"
            else:
                sku["category"]["usageType"] = "OnDemand"
            self.assertEqual(tool.price_rows([ROW], data)[0]["gcp_runtime_estimate_usd"], "")


if __name__ == "__main__":
    unittest.main()
