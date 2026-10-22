import importlib.util
import ipaddress
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'linux'))
spec = importlib.util.spec_from_file_location('health_under_test', ROOT / 'linux/health_check.py')
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)
OLD, NEW = '172.64.229.1', '172.64.229.2'
ENV = {'CF_DNS_ENABLED': '1', 'CF_DNS_NAME': 'test.example.com', 'CLOUDFLARE_ZONE_ID': '0' * 32,
       'CLOUDFLARE_API_TOKEN': 'test-secret', 'CFST_TEST_URL': 'https://speed.cloudflare.com/__down?bytes=25000000'}


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.messages = patch.object(health, 'status')
        self.messages.start()
        self.addCleanup(self.messages.stop)
        self.record = {'id': '1' * 32, 'content': OLD, 'type': 'A', 'name': ENV['CF_DNS_NAME'], 'proxied': False}
        self.client = Mock()
        self.client.records.return_value = [self.record]
        self.backups = Mock(return_value=[NEW])
        self.compare = Mock(return_value=['DNS: failover result'])
        self.sleep = Mock()

    def run_check(self, pings):
        self.probe = Mock(side_effect=pings)
        return health.check(client=self.client, probe=self.probe, sleep=self.sleep, backups=self.backups, compare=self.compare)

    def test_healthy_check_uses_one_ping_and_no_download_or_report(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)):
            self.assertEqual(self.run_check([{OLD: True}]), (0, None))
            self.assertFalse(list(Path(tmp).iterdir()))
        self.client.records.assert_called_once()
        self.probe.assert_called_once_with([OLD])
        self.backups.assert_not_called()
        self.compare.assert_not_called()
        self.sleep.assert_not_called()

    def test_dns_disabled_does_not_access_api_or_ping(self):
        with patch.dict(os.environ, {'CF_DNS_ENABLED': '0'}, clear=True):
            self.assertEqual(self.run_check([]), (0, None))
        self.client.records.assert_not_called()
        self.probe.assert_not_called()

    def test_transient_failure_recovers_without_scan_or_report(self):
        self.assertEqual(self.run_check([{OLD: False}, {OLD: True}]), (0, None))
        self.sleep.assert_called_once_with(30)
        self.backups.assert_not_called()
        self.compare.assert_not_called()

    def test_local_ping_error_never_requests_full_scan(self):
        code, lines = self.run_check([{OLD: None}])
        self.assertEqual(code, 1)
        self.assertTrue(lines)
        self.backups.assert_not_called()

    def test_second_round_error_does_not_count_as_confirmed_fault(self):
        code, _ = self.run_check([{OLD: False}, {OLD: None}])
        self.assertEqual(code, 1)
        self.backups.assert_not_called()

    def test_confirmed_failure_uses_verified_backups_in_failover_only_mode(self):
        code, lines = self.run_check([{OLD: False}] * 2)
        self.assertEqual(code, 0)
        self.assertIn('DNS: failover result', lines)
        self.backups.assert_called_once_with([OLD], 1)
        self.assertTrue(self.compare.call_args.kwargs['failover_only'])
        self.assertEqual(self.compare.call_args.args[0], [NEW])

    def test_missing_ram_backup_requests_full_scan_without_writing_report(self):
        self.backups.return_value = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)):
            self.assertEqual(self.run_check([{OLD: False}] * 2), (health.NEED_SCAN, None))
            self.assertFalse(list(Path(tmp).iterdir()))
        self.compare.assert_not_called()

    def test_intervening_dns_edit_cancels_backup_switch(self):
        self.client.records.side_effect = [[self.record], [dict(self.record, content=NEW)]]
        code, lines = self.run_check([{OLD: False}] * 2)
        self.assertEqual(code, 0)
        self.assertIn('记录已变化', lines[0])
        self.compare.assert_not_called()

    def test_healthy_main_creates_no_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)), patch.object(health, 'check', return_value=(0, None)):
            self.assertEqual(health.main(), 0)
            self.assertFalse(list(Path(tmp).iterdir()))

    def test_error_report_is_ram_only_and_does_not_leak_exception_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)), patch.object(health, 'check', side_effect=RuntimeError(ENV['CLOUDFLARE_API_TOKEN'])):
            self.assertEqual(health.main(), 1)
            text = (Path(tmp) / 'report.txt').read_text(encoding='utf-8')
            self.assertNotIn(ENV['CLOUDFLARE_API_TOKEN'], text)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['report.txt'])

    def test_cached_ips_reject_invalid_old_duplicate_and_non_cf_addresses(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)):
            (Path(tmp) / 'best-ips.txt').write_text(OLD + '\n' + NEW + '\n' + NEW + '\ninvalid\n192.168.1.1\n')
            self.assertEqual(health.cached_ips([OLD], [ipaddress.IPv4Network('172.64.0.0/13')]), [NEW])

    def fake_scanner(self):
        return types.SimpleNamespace(MODE=None, validate=Mock(return_value=[ipaddress.IPv4Network('172.64.0.0/13')]),
                                     PORT=443, MAX_LATENCY=250, COLO='NRT', MIN_SPEED=5,
                                     identify=Mock(side_effect=lambda row: row), throughput=Mock(return_value=10),
                                     RateLimited=type('RateLimited', (RuntimeError,), {}))

    def test_backup_requires_fresh_nrt_and_speed_validation_and_cleans_scratch(self):
        scanner = self.fake_scanner()
        with tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)), patch.dict(sys.modules, {'nrt_scan': scanner}), patch.object(health.dns_update, 'latency', return_value=65):
            (Path(tmp) / 'best-ips.txt').write_text(NEW + '\n')
            self.assertEqual(health.verified_backups([OLD], 1), [NEW])
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['best-ips.txt'])
        self.assertEqual(scanner.MODE, '--check')
        scanner.identify.assert_called_once()
        scanner.throughput.assert_called_once()

    def test_wrong_colo_or_insufficient_download_speed_rejects_cached_backup(self):
        for wrong_colo in (True, False):
            scanner = self.fake_scanner()
            if wrong_colo:
                scanner.identify.side_effect = None
                scanner.identify.return_value = None
            else:
                scanner.throughput.return_value = 1
            with self.subTest(wrong_colo=wrong_colo), tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)), patch.dict(sys.modules, {'nrt_scan': scanner}), patch.object(health.dns_update, 'latency', return_value=65):
                (Path(tmp) / 'best-ips.txt').write_text(NEW + '\n')
                self.assertEqual(health.verified_backups([OLD], 1), [])

    def test_rate_limit_does_not_escalate_into_another_download_scan(self):
        scanner = self.fake_scanner()
        scanner.throughput.side_effect = scanner.RateLimited('HTTP 429')
        with tempfile.TemporaryDirectory() as tmp, patch.object(health, 'OUTPUT', Path(tmp)), patch.dict(sys.modules, {'nrt_scan': scanner}), patch.object(health.dns_update, 'latency', return_value=65):
            (Path(tmp) / 'best-ips.txt').write_text(NEW + '\n')
            with self.assertRaises(health.dns_update.DNSProblem):
                health.verified_backups([OLD], 1)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['best-ips.txt'])


if __name__ == '__main__':
    unittest.main()
