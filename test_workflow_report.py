import copy
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ci.check_authorized_user import authorized
import workflow_report as tool
import inspect_try
from test_inspect_try import TASK, RUN
from test_predict_try import SAMPLE, TASK as PREDICT_TASK, DECISION, prices, AS_OF


class AccessTests(unittest.TestCase):
    def test_actor_and_rerun_actor_must_both_be_allowed(self):
        self.assertTrue(authorized(["markcor"], "markcor", "markcor"))
        self.assertTrue(authorized(["markcor"], "MarkCor", ""))
        self.assertFalse(authorized(["markcor"], "markcor", "outsider"))
        self.assertFalse(authorized(["markcor"], "outsider", "markcor"))
        self.assertFalse(authorized(["markcor"], "", "markcor"))
        with self.assertRaises(ValueError):
            authorized({"markcor": True}, "markcor", "markcor")


class WorkflowReportTests(unittest.TestCase):
    def test_analysis_uses_public_page_without_billing_catalog_and_handles_failure(self):
        sample = dict.fromkeys(inspect_try.FIELDS, '')
        sample.update(SAMPLE, started='2026-11-05T19:30:00Z', category='Unit/source test')
        snapshot = {'source': tool.public_spot.URL, 'retrieved_at': AS_OF.isoformat(),
                    'rates': [{'machine_type': 'c3d-standard-16', 'region': 'us-central1', 'usd_per_hour': '0.16656'}]}
        previous_cwd = Path.cwd()
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable), tempfile.TemporaryDirectory() as folder:
                try:
                    os.chdir(folder)
                    with patch.object(tool.inspection, 'inspect', return_value=[sample]), \
                         patch.object(tool, 'catalog_for', side_effect=AssertionError('Analysis must not query billing')), \
                         patch.object(tool.public_spot, 'fetch_rates', return_value=snapshot,
                                      side_effect=OSError if unavailable else None), \
                         patch.object(sys, 'argv', ['workflow_report.py', 'analysis', 'a'*40]), \
                         patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(Path(folder)/'summary.md')}), \
                         patch('sys.stdout'):
                        tool.main()
                    with Path('reports/costs.csv').open() as f:
                        row = next(csv.DictReader(f))
                    self.assertEqual(row['gcp_runtime_estimate_usd'], '' if unavailable else '0.08328000')
                    report = Path('summary.md').read_text()
                    self.assertIn('not prices effective when the task ran', report)
                    self.assertIn('Storage is not yet included', report)
                    if unavailable:
                        self.assertIn('webpage unavailable or changed', report)
                finally:
                    os.chdir(previous_cwd)

    def test_analysis_summary_carries_caveats_and_unknown_coverage(self):
        task = copy.deepcopy(TASK)
        task['task']['tags'] = {'kind': 'test', 'test-suite': 'xpcshell'}
        with patch.object(inspect_try, 'get', return_value=b''):
            row = inspect_try.run_row(task, RUN)
        rows = tool.gcp.price_rows(tool.azure.price_rows([row], {}), {})
        with tempfile.TemporaryDirectory() as folder:
            report = tool.render_report('analysis', 'revision', rows, ['Azure billing query unavailable'], Path(folder))
            self.assertIn('Priced 0/1', report)
            self.assertIn('Known compute subtotal: Not determined', report)
            self.assertIn('Storage is not yet included', report)
            self.assertIn('billing query unavailable', report)
            self.assertIn('Failed tests: 1 runs; 0 priced', report)
            self.assertIn('already included', report)
            self.assertIn('Spot instances are assumed', report)
            self.assertIn('not negotiated', report)
            self.assertTrue((Path(folder)/'hardware-totals.csv').exists())

    def test_prediction_summary_explains_low_confidence_and_window(self):
        rows = tool.predictor.predict_rows({'new': {'task': PREDICT_TASK}}, DECISION, 'decision',
                                          [dict(SAMPLE, name='different')], prices(), AS_OF)
        with tempfile.TemporaryDirectory() as folder:
            report = tool.render_report('predict', 'revision', rows, [], Path(folder))
        self.assertIn('low-confidence', report)
        self.assertIn('preceding 24 hours', report)
        self.assertIn('reused dependencies', report)
        self.assertIn('Later action tasks', report)
        self.assertIn('sparse', report.lower())
        self.assertIn('Not determined', report)

    def test_missing_catalog_produces_unknown_gcp_cost_without_losing_azure(self):
        start = datetime(2030, 1, 1, tzinfo=timezone.utc)
        end = datetime(2030, 1, 2, tzinfo=timezone.utc)
        warnings = []
        with patch.object(tool.gcp, 'fetch_catalog', side_effect=OSError), patch.object(Path, 'glob', return_value=[]):
            catalog = tool.catalog_for(start, end, warnings)
        row = dict(SAMPLE, started=start.isoformat(), resolved=end.isoformat(), duration_seconds='86400')
        azure = dict(row, cloud_provider='azure', azure_runtime_estimate_usd='0.12345678')
        result = tool.gcp.price_rows([row, azure], catalog)
        self.assertEqual(result[0]['total_known_cost_usd'], '')
        self.assertEqual(result[1]['total_known_cost_usd'], '0.12345678')
        self.assertTrue(any('Not determined' in w for w in warnings))


if __name__ == '__main__':
    unittest.main()
