import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith('linux') and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'Root Linux shell tests with an isolated executable stub')
class ScheduledScansTests(unittest.TestCase):
    def run_schedule(self, official=0, proxy=0):
        with tempfile.TemporaryDirectory(prefix='cf-combined-test.') as directory:
            root = Path(directory)
            record = root / 'order'
            stub = root / 'runner'
            stub.write_text('#!/bin/bash\nprintf "%s\\n" "$1" >> "' + str(record) + '"\n'
                            'case "$1" in --scheduled) exit ' + str(official) + ';; '
                            '--proxy-scheduled) exit ' + str(proxy) + ';; *) exit 99;; esac\n')
            stub.chmod(0o755)
            try:
                subprocess.run([str(stub), '--check-exec'], capture_output=True, timeout=3)
            except PermissionError:
                self.skipTest('Schedule stub requires an executable temporary filesystem')
            record.unlink(missing_ok=True)
            source = (ROOT / 'linux/scheduled_scans.sh').read_text().replace('/usr/local/sbin/cf-optimizer', str(stub))
            script = root / 'schedule'
            script.write_text(source)
            result = subprocess.run(['bash', str(script)], capture_output=True, text=True, timeout=5)
            return result.returncode, record.read_text().splitlines()

    def test_official_then_proxy_in_order(self):
        self.assertEqual(self.run_schedule(), (0, ['--scheduled', '--proxy-scheduled']))

    def test_proxy_still_runs_after_official_failure(self):
        self.assertEqual(self.run_schedule(official=7), (7, ['--scheduled', '--proxy-scheduled']))

    def test_proxy_failure_is_reported_after_successful_official_scan(self):
        self.assertEqual(self.run_schedule(proxy=9), (9, ['--scheduled', '--proxy-scheduled']))


if __name__ == '__main__':
    unittest.main()
