import copy
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import unittest
import urllib.error
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dns_under_test', ROOT / 'linux/dns_update.py')
dns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dns)

ENV = {'CF_DNS_ENABLED': '1', 'CF_DNS_NAME': 'test.example.com',
       'CLOUDFLARE_ZONE_ID': '0' * 32, 'CLOUDFLARE_API_TOKEN': 'test-secret-not-real'}
OLD, NEW, OTHER = '172.64.229.1', '162.159.44.1', '104.16.1.1'


def record(ip=OLD, number=1, **fields):
    result = dict(id='{:032x}'.format(number), type='A', name=ENV['CF_DNS_NAME'], content=ip,
                  ttl=300, proxied=False, comment='preserve this', modified_on='before')
    result.update(fields)
    return result


class FakeCloudflare:
    def __init__(self, records=None):
        self.current = copy.deepcopy(records or [record()])
        self.writes = []
        self.reads = 0
        self.edit_during_compare = False
        self.failed_readback = False

    def records(self):
        self.reads += 1
        if self.edit_during_compare and self.reads == 2:
            self.current[0]['content'] = OTHER
        if self.failed_readback and self.writes:
            raise dns.DNSProblem('Cloudflare API HTTP 503')
        return copy.deepcopy(self.current)

    def replace(self, records, ips):
        self.writes.append((copy.deepcopy(records), ips[:]))
        for item, ip in zip(self.current, ips):
            item['content'] = ip


class DNSTests(unittest.TestCase):
    def run_comparison(self, rounds, candidates=None, client=None):
        client = client or FakeCloudflare()
        measurement = Mock(side_effect=rounds)
        sleeper = Mock()
        with patch.dict(os.environ, ENV, clear=True):
            report = dns.compare_and_update(candidates or [NEW], client=client, measure=measurement, sleep=sleeper)
        return '\n'.join(report), client, measurement, sleeper

    def test_disabled_does_not_contact_cloudflare_or_probe(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(dns, 'Cloudflare') as api, patch.object(dns, 'measure_round') as measure:
            report = dns.compare_and_update([NEW])
        self.assertIn('未启用', report[0])
        api.assert_not_called()
        measure.assert_not_called()

    def test_requires_same_candidate_ten_percent_faster_in_both_rounds(self):
        report, client, measure, sleep = self.run_comparison([{OLD: 100, NEW: 90}, {OLD: 100, NEW: 89}])
        self.assertIn('DNS 已更新', report)
        self.assertEqual(client.current[0]['content'], NEW)
        self.assertEqual(client.reads, 3)
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(measure.call_count, 2)
        sleep.assert_called_once_with(30)

    def test_one_failed_round_prevents_writes_even_if_second_is_fast(self):
        report, client, measure, _ = self.run_comparison([{OLD: 100, NEW: 90.01}, {OLD: 100, NEW: 70}])
        self.assertIn('保留现有解析', report)
        self.assertFalse(client.writes)
        self.assertEqual(measure.call_count, 2)

    def test_different_round_winners_do_not_count_as_two_confirmations(self):
        _, client, _, _ = self.run_comparison([{OLD: 100, NEW: 80, OTHER: 95}, {OLD: 100, NEW: 95, OTHER: 80}], [NEW, OTHER])
        self.assertFalse(client.writes)

    def test_multiple_records_use_fastest_old_and_preserve_count(self):
        old2, new2 = '172.64.229.2', '162.159.44.2'
        client = FakeCloudflare([record(), record(old2, 2)])
        rounds = [{OLD: 100, old2: 80, NEW: 70, new2: 71}] * 2
        report, client, _, _ = self.run_comparison(rounds, [NEW, new2], client)
        self.assertIn('2 条 A 记录', report)
        self.assertEqual(len(client.current), 2)
        self.assertEqual({r['content'] for r in client.current}, {NEW, new2})
        self.assertTrue(all(r['ttl'] == 300 and r['comment'] == 'preserve this' for r in client.current))
        client = FakeCloudflare([record(), record(old2, 2)])
        rounds = [{OLD: 100, old2: 80, NEW: 75, new2: 76}] * 2
        _, client, _, _ = self.run_comparison(rounds, [NEW, new2], client)
        self.assertFalse(client.writes)

    def test_insufficient_candidates_or_existing_ips_never_replaced_with_duplicates(self):
        for candidates in ([OLD], [NEW, NEW]):
            client = FakeCloudflare([record(), record('172.64.229.2', 2)])
            _, client, measure, _ = self.run_comparison([], candidates, client)
            self.assertFalse(client.writes)
            measure.assert_not_called()

    def test_incomplete_old_measurements_prevent_updates(self):
        for value in (None, math.nan, math.inf, 0):
            with self.subTest(value=value):
                _, client, _, _ = self.run_comparison([{OLD: value, NEW: 50}])
                self.assertFalse(client.writes)

    def test_incomplete_new_measurements_do_not_satisfy_threshold(self):
        for value in (None, math.nan, math.inf, 0):
            with self.subTest(value=value):
                _, client, _, _ = self.run_comparison([{OLD: 100, NEW: value}, {OLD: 100, NEW: 70}])
                self.assertFalse(client.writes)

    def test_concurrent_dns_edit_is_not_overwritten(self):
        client = FakeCloudflare()
        client.edit_during_compare = True
        report, client, _, _ = self.run_comparison([{OLD: 100, NEW: 80}] * 2, client=client)
        self.assertIn('记录已变化', report)
        self.assertFalse(client.writes)

    def test_failed_readback_reports_uncertain_update_without_retry(self):
        client = FakeCloudflare()
        client.failed_readback = True
        report, client, _, _ = self.run_comparison([{OLD: 100, NEW: 80}] * 2, client=client)
        self.assertIn('更新结果未确认', report)
        self.assertNotIn('DNS 已更新', report)
        self.assertEqual(len(client.writes), 1)

    def test_readback_with_wrong_address_is_not_reported_as_success(self):
        client = FakeCloudflare()
        replace = client.replace
        def wrong_write(records, ips):
            replace(records, ips)
            client.current[0]['content'] = OTHER
        client.replace = wrong_write
        report, client, _, _ = self.run_comparison([{OLD: 100, NEW: 80}] * 2, client=client)
        self.assertIn('更新结果未确认', report)
        self.assertNotIn('DNS 已更新', report)
        self.assertEqual(len(client.writes), 1)

    def test_only_one_qualified_replacement_for_two_records_does_not_partially_update(self):
        old2 = '172.64.229.2'
        client = FakeCloudflare([record(), record(old2, 2)])
        rounds = [{OLD: 100, old2: 105, NEW: 80, OTHER: 95}] * 2
        report, client, _, _ = self.run_comparison(rounds, [NEW, OTHER], client)
        self.assertIn('保留现有解析', report)
        self.assertFalse(client.writes)
        self.assertEqual([r['content'] for r in client.current], [OLD, old2])

    def test_unknown_api_error_does_not_expose_credentials(self):
        client = FakeCloudflare()
        client.records = Mock(side_effect=RuntimeError(ENV['CLOUDFLARE_API_TOKEN']))
        report, _, _, _ = self.run_comparison([], client=client)
        self.assertNotIn(ENV['CLOUDFLARE_API_TOKEN'], report)
        self.assertIn('未提交更新', report)

    def test_configuration_rejects_incomplete_or_ambiguous_names(self):
        for updates in ({'CLOUDFLARE_ZONE_ID': ''}, {'CF_DNS_NAME': '*.example.com'}, {'CF_DNS_NAME': 'a.example.com,b.example.com'}, {'CF_DNS_ENABLED': 'yes'}):
            with self.subTest(updates=updates), self.assertRaises(dns.DNSProblem):
                dns.configuration(dict(ENV, **updates))

    def test_cloudflare_filters_exact_name_disables_proxy_and_patches_content_only(self):
        with patch.dict(os.environ, {}, clear=True), patch('urllib.request.build_opener') as opener:
            client = dns.Cloudflare(*dns.configuration(ENV))
        self.assertEqual(opener.call_args.args[0].proxies, {})
        client.request = Mock(return_value={'success': True, 'result': [record()]})
        self.assertEqual(client.records()[0]['content'], OLD)
        self.assertIn('name.exact=test.example.com', client.request.call_args.args[1])
        client.replace([record()], [NEW])
        self.assertEqual(client.request.call_args.args, ('POST', '/batch', {'patches': [{'id': '0' * 31 + '1', 'content': NEW}]}))

    def test_optional_control_api_proxy_does_not_change_numeric_tcp_probe(self):
        with patch.dict(os.environ, {'CF_DNS_API_PROXY_URL': 'http://127.0.0.1:7890'}, clear=True), patch('urllib.request.build_opener') as opener:
            dns.Cloudflare(*dns.configuration(ENV))
        self.assertEqual(opener.call_args.args[0].proxies['https'], 'http://127.0.0.1:7890')

    def test_actual_request_encodes_batch_and_keeps_token_out_of_url_and_body(self):
        client = dns.Cloudflare(*dns.configuration(ENV))
        client.opener = Mock()
        client.opener.open.return_value = io.BytesIO(b'{"success":true,"result":{}}')
        body = {'patches': [{'id': '1' * 32, 'content': NEW}]}
        client.request('POST', '/batch', body)
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(json.loads(request.data), body)
        self.assertTrue(request.full_url.endswith('/dns_records/batch'))
        self.assertEqual(request.get_header('Authorization'), 'Bearer ' + ENV['CLOUDFLARE_API_TOKEN'])
        self.assertNotIn(ENV['CLOUDFLARE_API_TOKEN'], request.full_url)
        self.assertNotIn(ENV['CLOUDFLARE_API_TOKEN'].encode(), request.data)
        self.assertEqual(client.opener.open.call_count, 1)

    def test_auth_and_transport_errors_are_sanitized_and_not_retried(self):
        errors = [urllib.error.HTTPError('https://api.cloudflare.com/', 403, ENV['CLOUDFLARE_API_TOKEN'], {}, None),
                  urllib.error.URLError(ENV['CLOUDFLARE_API_TOKEN'])]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                client = dns.Cloudflare(*dns.configuration(ENV))
                client.opener = Mock()
                client.opener.open.side_effect = error
                with self.assertRaises(dns.DNSProblem) as raised:
                    client.request('POST', '/batch', {'patches': []})
                self.assertNotIn(ENV['CLOUDFLARE_API_TOKEN'], str(raised.exception))
                self.assertEqual(client.opener.open.call_count, 1)

    def test_malformed_api_responses_do_not_count_as_confirmed_success(self):
        for content in (b'not-json', b'[]', b'{"success":false}', b'{"success":"true"}'):
            with self.subTest(content=content):
                client = dns.Cloudflare(*dns.configuration(ENV))
                client.opener = Mock()
                client.opener.open.return_value = io.BytesIO(content)
                with self.assertRaises(dns.DNSProblem):
                    client.request('POST', '/batch', {'patches': []})

    def test_api_redirects_are_refused(self):
        self.assertIsNone(dns.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example.com'))

    def test_wrong_name_or_proxied_or_paginated_records_are_rejected(self):
        client = dns.Cloudflare(*dns.configuration(ENV))
        invalid = [{'result': [record(proxied=True)]}, {'result': [record(name='other.example.com')]},
                   {'result': []}, {'result': [record(), record()]},
                   {'result': [record()], 'result_info': {'total_pages': 2}}]
        for data in invalid:
            with self.subTest(data=data), patch.object(client, 'request', return_value=data), self.assertRaises(dns.DNSProblem):
                client.records()

    def test_tcp_probe_uses_ipv4_literal_and_median_and_rejects_loss(self):
        connection = Mock()
        connection.__enter__ = Mock(return_value=connection)
        connection.__exit__ = Mock(return_value=False)
        times = [value for ms in (100, 200, 80, 90, 1000) for value in (0, ms / 1000)]
        with patch.object(dns.socket, 'socket', return_value=connection) as constructor, patch.object(dns.time, 'monotonic', side_effect=times), patch.object(dns.time, 'sleep'):
            self.assertEqual(dns.latency(NEW, 443), 100)
        constructor.assert_called_with(dns.socket.AF_INET, dns.socket.SOCK_STREAM)
        connection.connect.assert_called_with((NEW, 443))
        connection.connect.side_effect = OSError('unreachable')
        with patch.object(dns.socket, 'socket', return_value=connection):
            self.assertIsNone(dns.latency(NEW, 443))


if __name__ == '__main__':
    unittest.main()
