import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'linux'))
import cf_dns_bridge as bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(user=12345, chat=12345, dns=('token', 'zone', 'name', 'proxy'))
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

    def test_result_retry_does_not_execute_dns_twice(self):
        worker = Mock(settings=self.settings)
        worker.request.side_effect = [self.job, bridge.control.Problem('lost result'), None, KeyboardInterrupt]
        with patch.object(bridge.control, 'ControlDNS'), patch.object(bridge.control, 'handle', return_value='ok') as handle, \
                patch.object(bridge.time, 'time', return_value=1050), patch.object(bridge.time, 'sleep'):
            with self.assertRaises(KeyboardInterrupt):
                bridge.run(worker)
        handle.assert_called_once()
        self.assertEqual([c.args[0] for c in worker.request.call_args_list], ['/next', '/result', '/result', '/next'])


if __name__ == '__main__':
    unittest.main()
