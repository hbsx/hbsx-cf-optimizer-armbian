import sys
import unittest
import tempfile
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'linux'))
import cf_dns_bridge as bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(user=12345, chat=12345, dns=('token', 'zone', 'name'))
        self.job = dict(id='a' * 32, command='/dns', sender_id=12345, chat_id=12345,
                        created_at=1000, expires_at=1120)

    def test_owner_freshness_dedup_and_command_validation(self):
        self.assertTrue(bridge.accepted_job(self.job, self.settings, [], 1050))
        cases = [dict(sender_id=1), dict(chat_id=-12345), dict(created_at=800),
                 dict(expires_at=1050), dict(expires_at=9999), dict(command='/help'),
                 dict(command='/set domain.example'), dict(command='/set 2606:4700::1%eth0')]
        for change in cases:
            with self.subTest(change=change):
                self.assertFalse(bridge.accepted_job(dict(self.job, **change), self.settings, [], 1050))
        self.assertFalse(bridge.accepted_job(self.job, self.settings, ['a' * 32], 1050))
        # Valid syntax reaches DNS core, including its explicit non-Cloudflare refusal.
        self.assertTrue(bridge.accepted_job(dict(self.job, command='/set 1.1.1.1'), self.settings, [], 1050))
        for text in ('/usecf', '/usefd', '/use6', '/tj', '/usetj', '/usetj HKG 2', '/usetj 2', '/settj 39.105.24.90'):
            self.assertTrue(bridge.accepted_job(dict(self.job, command=text), self.settings, [], 1050))
        for text in ('/tj extra', '/usetj HKG 0', '/usetj HKG 11', '/settj 127.0.0.1',
                     '/settj 39.105.24.90:8443', '/settj ::1'):
            self.assertFalse(bridge.accepted_job(dict(self.job, command=text), self.settings, [], 1050))

    def test_result_retry_does_not_execute_dns_twice(self):
        worker = Mock(settings=self.settings)
        worker.request.side_effect = [None, self.job, bridge.control.Problem('lost result'), None, KeyboardInterrupt]
        with patch.object(bridge, 'snapshot_payload', return_value={}), patch.object(bridge, 'execute_job', return_value=('ok', 'ok')) as handle, \
                patch.object(bridge.time, 'time', return_value=1050), patch.object(bridge.time, 'sleep'):
            with self.assertRaises(KeyboardInterrupt):
                bridge.run(worker)
        handle.assert_called_once()
        self.assertEqual([c.args[0] for c in worker.request.call_args_list], ['/snapshot', '/next', '/result', '/result', '/next'])

    def test_snapshot_publishes_only_fresh_pretested_winners_without_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(runtime=Path(directory))
            path = settings.runtime / 'best-ips.txt'
            path.write_text('172.64.229.167\n172.64.229.91\n')
            os.utime(path, (1000, 1000))
            with patch.object(bridge.control, 'BASE', Path(__file__).resolve().parents[1]), \
                    patch.object(bridge.proxy_control, 'configuration', side_effect=bridge.control.Problem('not configured')):
                self.assertEqual(bridge.snapshot_payload(settings, 1001)['cf4']['ip'], '172.64.229.167')
                self.assertIsNone(bridge.snapshot_payload(settings, 1001)['cf6'])
                self.assertIsNone(bridge.snapshot_payload(settings, 1000+96*3600+1)['cf4'])
                self.assertIsNone(bridge.snapshot_payload(settings, 600)['cf4'])

    def test_default_cf_command_and_failover_are_safe_before_or_after_write(self):
        def run(action):
            with patch.object(bridge.control, 'handle', side_effect=action), patch.object(bridge.time, 'time', return_value=1050):
                return bridge.execute_job(dict(self.job, command='/usecf'), self.settings)
        def before(command, settings, client):
            self.assertEqual(command, '/use4')
            raise bridge.dns_update.DNSProblem('network unavailable')
        self.assertEqual(run(before)[1], 'unavailable')
        def after(command, settings, client):
            client.request('POST', '/batch', {})
        with patch.object(bridge.control.ControlDNS, 'request', side_effect=bridge.dns_update.DNSProblem('lost reply')):
            self.assertEqual(run(after)[1], 'uncertain')
        self.assertEqual(run(lambda *_: (_ for _ in ()).throw(bridge.control.Problem('node probe failed')))[1], 'failed')

    def test_expired_job_never_starts_a_dns_write(self):
        def mutate(command, settings, client):
            client.request('POST', '/batch', {})
        with patch.object(bridge.control, 'handle', side_effect=mutate), \
                patch.object(bridge.time, 'time', return_value=1121), \
                patch.object(bridge.control.ControlDNS, 'request') as request:
            self.assertEqual(bridge.execute_job(dict(self.job, command='/usecf'), self.settings)[1], 'failed')
        request.assert_not_called()

    def test_reverse_proxy_cache_uses_existing_sorted_snapshot_and_fixed_port(self):
        settings = SimpleNamespace(runtime=Path('not-present'))
        with patch.object(bridge.proxy_control, 'configuration', return_value=(('token', 'zone', 'tj.example.com'), 443, 'config')), \
                patch.object(bridge.proxy_control, 'snapshot', return_value=([{'ip': '39.105.24.90'}], '2026-11-23T03:00:00+08:00')):
            payload = bridge.snapshot_payload(settings, 1000)
        self.assertEqual(payload['fd'], dict(ip='39.105.24.90', port=443, generated_at='2026-11-23T03:00:00+08:00'))


if __name__ == '__main__':
    unittest.main()
