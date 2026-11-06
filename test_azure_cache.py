from datetime import datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import price_azure as tool
from test_price_azure import ROW, SUBSCRIPTION, billing


NOW = datetime(2026, 11, 6, 1, tzinfo=timezone.utc)
COMPLETE = dict(ROW, name='test-windows11-64', resolved='2026-11-04T17:30:00Z')
ITEM = {'currencyCode': 'USD', 'armSkuName': ROW['vm_size'], 'armRegionName': 'eastus',
        'unitOfMeasure': '1 Hour', 'type': 'DevTestConsumption', 'productName': 'Virtual Machines Faldsv7 Series Windows',
        'meterName': 'F8alds v7 Spot', 'isPrimaryMeterRegion': True, 'tierMinimumUnits': 0,
        'retailPrice': 0.11, 'effectiveStartDate': '2026-08-01T00:00:00Z'}


class Frozen(datetime):
    current = NOW

    @classmethod
    def now(cls, tz=None):
        return cls.current


class AzureCacheTests(unittest.TestCase):
    def test_cache_is_valid_less_than_24_hours_and_does_not_renew_on_hits(self):
        fetch = Mock(side_effect=[{'rate': '0.1'}, {'rate': '0.2'}])
        with tempfile.TemporaryDirectory() as folder, patch.object(tool, 'datetime', Frozen):
            Frozen.current = NOW
            first = tool.cached_price({'scope': 'one'}, fetch, folder)
            Frozen.current = NOW + timedelta(hours=24) - timedelta(microseconds=1)
            hit = tool.cached_price({'scope': 'one'}, fetch, folder)
            self.assertEqual(hit[:2], first[:2])
            self.assertEqual(hit[2], 'hit')
            self.assertEqual(fetch.call_count, 1)
            Frozen.current = NOW + timedelta(hours=24)
            expired = tool.cached_price({'scope': 'one'}, fetch, folder)
            self.assertEqual(expired[0]['rate'], '0.2')
            self.assertEqual(fetch.call_count, 2)
        Frozen.current = NOW

    def test_corrupt_or_future_cache_is_not_used_and_query_scope_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            fetch = Mock(return_value={'value': 1})
            key = {'source': 'one'}
            tool.cached_price(key, fetch, folder)
            path = next(Path(folder).glob('*.json'))
            path.write_text('{broken')
            self.assertEqual(tool.cached_price(key, fetch, folder)[2], 'miss')
            data = json.loads(path.read_text()); data['retrieved_at'] = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
            path.write_text(json.dumps(data))
            self.assertEqual(tool.cached_price(key, fetch, folder)[2], 'miss')
            self.assertIsNone(tool.cached_price({'source': 'other'}, cache_dir=folder))
            self.assertNotEqual(tool.billing_cache_key([ROW], SUBSCRIPTION),
                                tool.billing_cache_key([dict(ROW, worker_pool='gecko-1/other')], SUBSCRIPTION))
            self.assertNotEqual(tool.billing_cache_key([ROW], SUBSCRIPTION),
                                tool.billing_cache_key([dict(ROW, started='2026-11-05T00:00:00Z')], SUBSCRIPTION))
            self.assertNotEqual(tool.billing_cache_key([ROW], SUBSCRIPTION),
                                tool.billing_cache_key([ROW], '00000000-0000-0000-0000-000000000001'))

    def test_retry_honors_longest_server_delay_and_preserves_auth(self):
        error = HTTPError('https://management.azure.com/query', 429, 'throttled',
                          {'Retry-After': '2', 'x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after': '46'}, BytesIO(b''))
        response = Mock(); response.__enter__ = Mock(return_value=BytesIO(b'{"ok": true}')); response.__exit__ = Mock(return_value=False)
        with patch.object(tool, 'urlopen', side_effect=[error, response]) as request, patch.object(tool.time, 'sleep') as sleep:
            self.assertEqual(tool.request_json('https://management.azure.com/query', {}, 'secret-token'), {'ok': True})
            sleep.assert_called_once_with(47)
            self.assertEqual(request.call_args.args[0].get_header('Authorization'), 'Bearer secret-token')
        for url in ('https://evil.example/query', 'https://management.azure.com.evil.example/query'):
            with self.assertRaises(ValueError):
                tool.request_json(url, {}, 'secret-token')
        self.assertIsNone(tool.NoRedirect().redirect_request(None,None,302,'',{},'https://evil.example/'))

    def test_retries_are_bounded_and_nontransient_errors_not_retried(self):
        for code, delay in ((403, '1'), (429, '999')):
            error = HTTPError('https://management.azure.com/query', code, 'error', {'Retry-After': delay}, BytesIO(b''))
            with patch.object(tool, 'urlopen', side_effect=error) as request, patch.object(tool.time, 'sleep') as sleep:
                with self.assertRaises(HTTPError):tool.request_json('https://management.azure.com/query', {}, 'token')
                self.assertEqual(request.call_count, 1)
                sleep.assert_not_called()
        errors = [HTTPError('https://management.azure.com/query',429,'throttled',{},BytesIO(b'')) for _ in range(3)]
        with patch.object(tool,'urlopen',side_effect=errors) as request, patch.object(tool.time,'sleep'):
            with self.assertRaises(HTTPError):tool.request_json('https://management.azure.com/query',{},'token')
            self.assertEqual(request.call_count,3)

    def test_retry_after_http_date_is_honored(self):
        error = HTTPError('https://management.azure.com/query', 503, 'unavailable',
                          {'Retry-After': 'Fri, 06 Nov 2026 01:00:30 GMT'}, BytesIO(b''))
        response = Mock(); response.__enter__ = Mock(return_value=BytesIO(b'{}')); response.__exit__ = Mock(return_value=False)
        Frozen.current = NOW
        with patch.object(tool, 'datetime', Frozen), patch.object(tool, 'urlopen', side_effect=[error, response]), \
                patch.object(tool.time, 'sleep') as sleep:
            tool.request_json('https://management.azure.com/query', {}, 'token')
            sleep.assert_called_once_with(31)

    def test_retail_matches_devtest_windows_and_rejects_ambiguity(self):
        alternatives = [dict(ITEM, type='Consumption', retailPrice=0.9),
                        dict(ITEM, productName='Virtual Machines Faldsv7 Series'), dict(ITEM, currencyCode='EUR'), ITEM]
        with tempfile.TemporaryDirectory() as folder, patch.object(tool,'request_json',return_value={'Items': alternatives}) as request:
            item, _, status = tool.retail_rate(ROW['vm_size'],'eastus','DevTestConsumption',folder)
            self.assertEqual(item['retailPrice'],0.11)
            self.assertEqual(status,'miss')
            self.assertEqual(tool.retail_rate(ROW['vm_size'],'eastus','DevTestConsumption',folder)[2],'hit')
            self.assertEqual(request.call_count,1)
        for items in ([dict(ITEM, unitOfMeasure='1 Month')], [ITEM, dict(ITEM, retailPrice=0.12)]):
            with tempfile.TemporaryDirectory() as folder, patch.object(tool,'request_json',return_value={'Items': items}):
                with self.assertRaises(ValueError):tool.retail_rate(ROW['vm_size'],'eastus','DevTestConsumption',folder)

    def test_fallback_populates_costs_and_second_run_avoids_all_provider_queries(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(tool,'fetch_billing',side_effect=HTTPError('',429,'throttled',{},None)) as billing_request, \
                patch.object(tool,'request_json',return_value={'Items':[ITEM]}) as public:
            warnings=[]
            result=tool.estimate_rows([COMPLETE,dict(COMPLETE,task_id='another')],SUBSCRIPTION,warnings,folder)
            self.assertEqual(result[0]['azure_runtime_estimate_usd'],'0.05500000')
            self.assertIn('NOT historical',result[0]['azure_price_note'])
            self.assertEqual(result[0]['azure_rate_cache_status'],'miss')
            self.assertTrue(any('HTTP 429' in note for note in warnings))
            self.assertEqual(billing_request.call_count,1);self.assertEqual(public.call_count,1)
            result=tool.estimate_rows([COMPLETE],SUBSCRIPTION,[],folder)
            self.assertEqual(result[0]['azure_rate_cache_status'],'hit')
            self.assertEqual(billing_request.call_count,1);self.assertEqual(public.call_count,1)
            for path in Path(folder).glob('*.json'):
                self.assertNotIn('token',path.read_text().lower())

    def test_fresh_subscription_cache_wins_over_retail_and_missing_data_stays_unknown(self):
        data=billing([[1,10,20261104,'F8alds v7 Spot','us east','USD']])
        with tempfile.TemporaryDirectory() as folder, patch.object(tool,'fetch_billing',return_value=data) as fetch, \
                patch.object(tool,'request_json',return_value={'Items':[ITEM]}):
            tool.cached_billing_rates([COMPLETE],SUBSCRIPTION,folder)
            tool.retail_rate(ROW['vm_size'],'eastus','DevTestConsumption',folder)
            rows=tool.estimate_rows([COMPLETE],SUBSCRIPTION,[],folder)
            self.assertEqual(rows[0]['azure_rate_source'],'Azure Cost Management')
            self.assertEqual(rows[0]['azure_runtime_estimate_usd'],'0.05000000')
            self.assertEqual(rows[0]['azure_rate_cache_status'],'hit')
            self.assertEqual(fetch.call_count,1)
        with tempfile.TemporaryDirectory() as folder, patch.object(tool,'fetch_billing',side_effect=OSError), \
                patch.object(tool,'request_json',return_value={'Items': []}):
            rows=tool.estimate_rows([COMPLETE],SUBSCRIPTION,[],folder)
            self.assertEqual(rows[0]['total_known_cost_usd'],'')
            self.assertEqual(rows[0]['total_cost_status'],'Not determined')


if __name__ == '__main__':
    unittest.main()
