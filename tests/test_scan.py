import importlib.util
import ipaddress
import io
import json
import os
from pathlib import Path
import runpy
import random
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'linux'))

def scanner():
    spec = importlib.util.spec_from_file_location('nrt_scan_under_test', ROOT / 'linux/nrt_scan.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {'CFST_TEST_URL': 'https://speed.cloudflare.com/__down?bytes=25000000'}, clear=True), patch('sys.argv', ['nrt_scan.py', '--check']):
        spec.loader.exec_module(module)
    return module

def row(ip='172.64.229.1', latency='65', speed='10', colo='NRT'):
    return [ip, '4', '4', '0.00', latency, speed, colo]

class ScannerTests(unittest.TestCase):
    def setUp(self): self.m = scanner()

    def test_identify_requires_success_and_exact_colo(self):
        for body, code, accepted in [('colo=NRT\n\nCURL_META:200', 0, True), ('colo=HKG\n\nCURL_META:200', 0, False), ('colo=NRT\n\nCURL_META:403', 0, False), ('colo=NRT\n\nCURL_META:200', 28, False)]:
            result = types.SimpleNamespace(stdout=body, returncode=code)
            with self.subTest(body=body, code=code), patch.object(self.m.subprocess, 'run', return_value=result):
                self.assertEqual(self.m.identify(row()) is not None, accepted)

    def test_download_rate_limit_stops_after_one_request(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.m.subprocess, 'run', return_value=types.SimpleNamespace(stdout='429,0', returncode=0)) as run:
            with self.assertRaises(self.m.RateLimited): self.m.throughput('172.64.229.1', Path(tmp))
            self.assertEqual(run.call_count, 1)

    def test_partial_timed_download_counts_bytes_but_rejects_wrong_colo(self):
        self.m.SECONDS = 5
        for colo, expected in [('NRT', 2.0), ('HKG', 0.0)]:
            def transfer(cmd, **kwargs):
                Path(cmd[cmd.index('-D') + 1]).write_text('HTTP/1.1 200 OK\nCF-RAY: abc-' + colo + '\n')
                return types.SimpleNamespace(stdout='200,10485760', returncode=28)
            with self.subTest(colo=colo), tempfile.TemporaryDirectory() as tmp, patch.object(self.m.subprocess, 'run', side_effect=transfer), patch.object(self.m.time, 'monotonic', side_effect=[0, 0, 0, 5, 5]):
                self.assertEqual(self.m.throughput('172.64.229.1', Path(tmp)), expected)

    def test_candidate_budget_spans_subnets(self):
        rows = [row('172.64.229.' + str(i), str(i)) for i in range(1, 6)] + [row('104.16.1.1', '100')]
        selected = self.m.diverse_candidates(rows)
        self.assertEqual([r[0] for r in selected[:2]], ['172.64.229.1', '104.16.1.1'])

    def test_smoke_prefers_current_ips_and_rejects_addresses_outside_cf(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.m.OUTPUT = Path(tmp)
            (self.m.OUTPUT / 'best-ips.txt').write_text('192.168.1.1\n162.159.44.241\ninvalid\n172.64.229.19\n')
            nets = [ipaddress.IPv4Network(line) for line in (ROOT / 'ip.txt').read_text().splitlines()]
            ips = self.m.smoke_target_ips(nets)
            self.assertEqual(ips[:2], ['162.159.44.241', '172.64.229.19'])
            self.assertEqual(len(ips), 5)
            self.assertNotIn('192.168.1.1', ips)

    def test_failed_selection_preserves_existing_ip_and_creates_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.m.OUTPUT = Path(tmp)
            original = self.m.OUTPUT / 'best-ips.txt'
            original.write_text('172.64.229.1\n')
            with self.assertRaises(RuntimeError):
                self.m.publish(Path(tmp), [row(speed='0')], [row()], 256, [], None, 1)
            self.assertEqual(original.read_text(), '172.64.229.1\n')
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['best-ips.txt'])

    def test_publish_compares_only_qualified_ips_and_includes_dns_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.m.OUTPUT = Path(tmp)
            work = Path(tmp) / 'work'
            work.mkdir()
            rows = [row(), row('172.64.229.2', speed='1')]
            with patch.object(self.m.dns_update, 'compare_and_update', return_value=['DNS: test decision']) as compare, patch('builtins.print'):
                self.m.publish(work, rows, rows, 256, [], None, 2)
            self.assertEqual(compare.call_args.args[0], ['172.64.229.1'])
            self.assertIn('DNS: test decision', (Path(tmp) / 'report.txt').read_text(encoding='utf-8'))
            self.assertEqual((Path(tmp) / 'best-ips.txt').read_text(), '172.64.229.1\n')

    def test_smoke_does_not_compare_or_update_dns(self):
        self.m.MODE = '--smoke'
        nets = [ipaddress.IPv4Network('172.64.229.0/24')]
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.m, 'tcp_probe', return_value=[row()]), patch.object(self.m, 'check_colos', return_value=[row()]), patch.object(self.m, 'throughput', return_value=10), patch.object(self.m, 'log'), patch.object(self.m.dns_update, 'compare_and_update') as compare:
            self.m.OUTPUT = Path(tmp)
            self.m.scan(nets, Path(tmp))
            compare.assert_not_called()

    def test_official_list_is_validated_without_writing_cache(self):
        text = (ROOT / 'ip.txt').read_text()
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.m.subprocess, 'run', return_value=types.SimpleNamespace(stdout=text)):
            self.m.OUTPUT = Path(tmp)
            nets = self.m.official_ranges(True)
            self.assertEqual(sum(n.num_addresses for n in nets), 1524736)
            self.assertEqual(sum(max(1, n.num_addresses // 256) for n in nets), 5956)
            self.assertFalse(list(Path(tmp).iterdir()))

    def test_ipv6_pool_sampling_is_bounded_unique_and_covers_each_network(self):
        nets = self.m.ipv6_ranges()
        samples = self.m.sample_ipv6(nets, 64, random.Random(42))
        self.assertEqual(len(samples), 256)
        self.assertEqual(len(set(samples)), 256)
        for net in nets:
            self.assertEqual(sum(ipaddress.ip_address(ip) in net for ip in samples), 64)
        for count in (0, 3, 257):
            with self.assertRaises(ValueError): self.m.sample_ipv6(nets, count)

    def test_ipv6_pool_rejects_non_cf_broad_and_duplicate_networks(self):
        for text in ('2606:4700::/32', '2001:db8::/48', '2606:4700:52::/48\n2606:4700:52::/48'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                self.m.BASE = Path(tmp)
                (Path(tmp) / 'ipv6-cidrs.txt').write_text(text)
                with self.assertRaises(ValueError): self.m.ipv6_ranges()

    def test_ipv6_resolve_uses_brackets_and_ipv6_without_changing_ipv4(self):
        cmd = self.m.curl_base(self.m.TRACE, '2606:4700:52::1', 5)
        self.assertIn('-6', cmd)
        self.assertNotIn('-4', cmd)
        self.assertEqual(cmd[-1], 'www.cloudflare.com:443:[2606:4700:52::1]')
        self.assertIn('-4', self.m.curl_base(self.m.TRACE, '172.64.229.1', 5))

    def test_ipv6_node_failure_rejects_otherwise_valid_trace(self):
        self.m.IPV6 = True
        response = types.SimpleNamespace(stdout='colo=NRT\n\nCURL_META:200', returncode=0)
        with patch.object(self.m.subprocess, 'run', return_value=response), patch.object(self.m, 'ipv6_node_works', return_value=False):
            self.assertIsNone(self.m.identify(row('2606:4700:52::1')))

    def test_ipv6_ws_requires_verified_upgrade_and_nrt(self):
        self.m.IPV6_NODE_HOST = 'node.example.com'
        self.m.IPV6_NODE_PATH = '/websocket'
        for status, colo, accept, accepted in [('101', 'NRT', 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=', True),
                ('200', 'NRT', 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=', False),
                ('101', 'SIN', 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=', False), ('101', 'NRT', 'wrong', False)]:
            context = MagicMock()
            context.wrap_socket.return_value.__enter__.return_value.recv.return_value = (
                'HTTP/1.1 ' + status + ' status\r\nUpgrade: websocket\r\nCF-Ray: test-' + colo
                + '\r\nSec-WebSocket-Accept: ' + accept + '\r\n\r\n').encode()
            with self.subTest(status=status, colo=colo, accept=accept), patch.object(self.m.socket, 'socket'), patch.object(self.m.ssl, 'create_default_context', return_value=context):
                self.assertEqual(self.m.ipv6_node_works('2606:4700:52::1'), accepted)
                self.assertEqual(context.wrap_socket.call_args.kwargs['server_hostname'], 'node.example.com')

    def test_ipv6_publication_never_calls_dns_or_replaces_ipv4_list(self):
        self.m.IPV6 = True
        with tempfile.TemporaryDirectory() as tmp:
            self.m.OUTPUT = Path(tmp)
            (Path(tmp) / 'best-ips.txt').write_text('172.64.229.1\n')
            work = Path(tmp) / 'work'; work.mkdir()
            value = row('2606:4700:52::1')
            with patch.object(self.m.dns_update, 'compare_and_update') as compare, patch('builtins.print'):
                self.m.publish(work, [value], [value], 64, [ipaddress.ip_network('2606:4700:52::/48')], None, 1)
            compare.assert_not_called()
            self.assertEqual((Path(tmp) / 'best-ips.txt').read_text(), '172.64.229.1\n')
            self.assertEqual((Path(tmp) / 'best-ips-v6.txt').read_text(), '2606:4700:52::1\n')

    def test_ipv6_candidate_budget_rotates_across_48_networks(self):
        values = [row('2606:4700:52::1', '80'), row('2606:4700:52::2', '81'), row('2606:4700:57::1', '99')]
        self.assertEqual([r[0] for r in self.m.diverse_candidates(values)[:2]], ['2606:4700:52::1', '2606:4700:57::1'])

    def test_ipv6_scan_samples_hosts_and_skips_ipv4_discovery(self):
        self.m.IPV6 = True
        self.m.MODE = '--ipv6'
        self.m.IPV6_SAMPLES = 4
        nets = self.m.ipv6_ranges()
        value = row('2606:4700:52::1')
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.m, 'tcp_probe', return_value=[value]) as probe, patch.object(self.m, 'check_colos', return_value=[value]), patch.object(self.m, 'throughput', return_value=10), patch.object(self.m, 'publish'), patch.object(self.m, 'log'):
            self.m.scan(nets, Path(tmp))
            probe.assert_called_once()
            self.assertEqual(len(probe.call_args.args[0]), 16)
            self.assertEqual(probe.call_args.args[1], 16)

class TelegramTests(unittest.TestCase):
    def test_explicit_proxy_and_no_report_retention_on_success_or_failure(self):
        for successful in (True, False):
            with self.subTest(successful=successful), tempfile.TemporaryDirectory() as tmp:
                report = Path(tmp) / 'report.txt'
                report.write_text('Test report', encoding='utf-8')
                def response(*args, **kwargs):
                    self.assertFalse(report.exists())
                    if not successful: raise OSError('network unavailable')
                    return io.BytesIO(json.dumps({'ok': True}).encode())
                fake = types.SimpleNamespace(open=response)
                env = {'TELEGRAM_BOT_TOKEN': 'test-token', 'TELEGRAM_CHAT_ID': 'test-chat', 'CFST_OUTPUT_DIR': tmp, 'TELEGRAM_PROXY_URL': 'http://127.0.0.1:7890'}
                with patch.dict(os.environ, env, clear=True), patch('urllib.request.build_opener', return_value=fake) as opener, patch('time.sleep'), self.assertRaises(SystemExit) as exited:
                    runpy.run_path(str(ROOT / 'linux/telegram_notify.py'), run_name='__main__')
                self.assertEqual(exited.exception.code, 0 if successful else 1)
                self.assertEqual(opener.call_args.args[0].proxies['https'], env['TELEGRAM_PROXY_URL'])
                self.assertFalse(list(Path(tmp).iterdir()))

if __name__ == '__main__': unittest.main()
