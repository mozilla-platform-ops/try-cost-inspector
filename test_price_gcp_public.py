import json
from pathlib import Path
import unittest
from unittest.mock import patch

import price_gcp_public as tool
from test_price_gcp import ROW


HTML = Path('tests/fixtures/gcp-public-spot.html').read_text()


def page(tables):
    return "AF_initDataCallback({key: 'ds:0', hash: '1', data:" + json.dumps(tables) + "});"


def regional(price='$0.16656 / 1 hour', region='Iowa (us-central1)', headers=None, cpu='16'):
    return [headers or ['Machine type', 'Virtual CPUs', 'Memory', 'Current Spot pricing (USD)'],
            [['c3d-standard-16', cpu, '64 GiB', price]], region, [3]]


class PublicSpotTests(unittest.TestCase):
    def test_real_regional_payload_and_monthly_views(self):
        rates = tool.parse_rates(HTML)
        self.assertEqual(str(rates['c3d-standard-16', 'us-central1']), '0.16656')
        self.assertEqual(str(rates['c3d-standard-16', 'us-east1']), '0.16656')
        self.assertNotIn(('c3d-standard-16', 'us-west1'), rates)
        rates = tool.parse_rates(page([regional(), regional('$121.5888 / 1 month')]))
        self.assertEqual(len(rates), 1)

    def test_region_specific_prices_are_not_defaulted_to_iowa(self):
        rates = tool.parse_rates(page([regional(), regional('$0.20 / 1 hour', 'South Carolina (us-east1)')]))
        self.assertNotEqual(rates['c3d-standard-16', 'us-central1'], rates['c3d-standard-16', 'us-east1'])

    def test_currency_units_storage_and_layout_fail_closed(self):
        cases = [page([regional('$121.5888 / 1 month')]),
                 page([regional(headers=['Machine type', 'Virtual CPUs', 'Memory', 'Current Spot pricing (EUR)'])]),
                 page([regional(headers=['Machine type', 'Virtual CPUs', 'Memory', 'Local SSD', 'Current Spot pricing (USD)'])]),
                 '<html>No price data</html>']
        for html in cases:
            with self.subTest(html=html[:100]), self.assertRaises(ValueError):
                tool.parse_rates(html)
        with self.assertRaises(ValueError):
            tool.parse_rates(page([regional(cpu='32')]))
        with self.assertRaises(ValueError):
            tool.parse_rates(page([regional(), regional('$0.20 / 1 hour')]))

    def test_lssd_uses_matching_cpu_ram_price_and_records_current_basis(self):
        with patch.object(tool, 'get', return_value=HTML.encode()) as get:
            snapshot = tool.fetch_rates()
        self.assertEqual(get.call_args.args[0], tool.URL)
        rows = tool.price_rows([ROW, dict(ROW, region='us-west1'),
                               dict(ROW, duration_seconds=''), dict(ROW, vm_size='unverified'), dict(ROW, vm_size=None),
                               dict(ROW, cloud_provider='azure', azure_runtime_estimate_usd='0.12345678')], snapshot)
        self.assertEqual(rows[0]['gcp_runtime_estimate_usd'], '0.16656000')
        self.assertIn('NOT historical or a 24-hour average', rows[0]['gcp_price_note'])
        self.assertIn(snapshot['retrieved_at'], rows[0]['gcp_price_note'])
        self.assertEqual(json.loads(rows[0]['gcp_rate_components'])[0]['machine_type'], 'c3d-standard-16')
        self.assertTrue(all(r['gcp_runtime_estimate_usd']=='' for r in rows[1:]))
        self.assertEqual(rows[-1]['total_known_cost_usd'], '0.12345678')
        self.assertEqual(rows[0]['total_cost_status'], 'Partial: storage is not yet included')


if __name__ == '__main__':
    unittest.main()
