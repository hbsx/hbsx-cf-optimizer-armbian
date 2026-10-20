import importlib.util
import ipaddress
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

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

    def test_official_list_is_validated_without_writing_cache(self):
        text = (ROOT / 'ip.txt').read_text()
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.m.subprocess, 'run', return_value=types.SimpleNamespace(stdout=text)):
            self.m.OUTPUT = Path(tmp)
            nets = self.m.official_ranges(True)
            self.assertEqual(sum(n.num_addresses for n in nets), 1524736)
            self.assertEqual(sum(max(1, n.num_addresses // 256) for n in nets), 5956)
            self.assertFalse(list(Path(tmp).iterdir()))

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
