from pathlib import Path
from types import SimpleNamespace
import tempfile
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'linux'))
import manual_proxy_scan as manual


class ManualScanTests(unittest.TestCase):
    def runner(self, args, **kwargs):
        active = args[-1] == 'cf-optimizer-fd-request.path'
        return SimpleNamespace(returncode=0 if active else 3, stdout='active\n' if active else 'inactive\n')

    def test_only_fixed_empty_ram_request_and_duplicate_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'fdscan.request'
            self.assertIn('提交',manual.request_scan(200,request=path,runner=self.runner,now=lambda:100))
            self.assertEqual(path.read_bytes(),b'')
            self.assertIn('未重复',manual.request_scan(200,request=path,runner=self.runner,now=lambda:100))
            self.assertEqual(list(Path(folder).iterdir()),[path])

    def test_expired_busy_or_missing_dispatcher_never_start_scan(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'fdscan.request';runner=Mock()
            with self.assertRaises(manual.control.Problem):
                manual.request_scan(99,request=path,runner=runner,now=lambda:100)
            runner.assert_not_called()
            busy=Mock(return_value=SimpleNamespace(returncode=0,stdout='activating\n'))
            self.assertIn('已有优选',manual.request_scan(200,request=path,runner=busy,now=lambda:100))
            missing=Mock(return_value=SimpleNamespace(returncode=3,stdout='inactive\n'))
            with self.assertRaises(manual.control.Problem):
                manual.request_scan(200,request=path,runner=missing,now=lambda:100)
            self.assertFalse(path.exists())
