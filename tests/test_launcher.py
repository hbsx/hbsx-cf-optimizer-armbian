import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith('linux') and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'Root Linux shell integration test; all commands use temporary stubs')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='cf-launcher-test.')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        self.lock = self.root / 'lock'
        self.program('ip', "case \"$*\" in *'rule del'*) exit 1;; *'rule show'*) exit 0;; *'route get'*) echo '104.16.0.1 dev stub0'; exit 0;; *'rule add'*) exit 0;; esac\nexit 2")
        self.program('id', 'echo 1001')
        self.program('runuser', 'case "$*" in *telegram_notify.sh*) touch ' + str(self.root / 'notify_called') + ';; esac\nexit "${FAKE_HEALTH_EXIT:-0}"')
        force = self.root / 'force'
        force.write_text('#!/bin/bash\nexec 8>' + str(self.lock) + '\nflock -n 8 || exit 93\ntouch ' + str(self.root / 'force_called') + '\n')
        force.chmod(0o755)
        source = (ROOT / 'linux/cf-optimizer').read_text()
        source = source.replace('export PATH=/usr/sbin:/usr/bin:/sbin:/bin', 'export PATH=' + str(self.bin) + ':/usr/sbin:/usr/bin:/sbin:/bin')
        source = source.replace('/run/lock/cf-optimizer.lock', str(self.lock)).replace('/run/cf-optimizer', str(self.runtime))
        source = source.replace('/opt/cf-optimizer', str(self.root / 'app')).replace('/usr/local/sbin/cf-optimizer', str(force))
        source = source.replace('install -d -m 750 -o cfoptimizer -g cfoptimizer', 'install -d -m 750')
        source = source.replace('[[ -n "$iface" && -d "/sys/class/net/$iface/device" ]]', '[[ -n "$iface" ]]')
        self.launcher = self.root / 'launcher'
        self.launcher.write_text(source)

    def program(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def run_launcher(self, mode='--health', code=0):
        return subprocess.run(['bash', str(self.launcher), mode], env=dict(os.environ, FAKE_HEALTH_EXIT=str(code)),
                              capture_output=True, text=True, timeout=10)

    def test_healthy_check_deletes_stale_report_without_sending_it(self):
        (self.runtime / 'report.txt').write_text('stale report')
        (self.runtime / 'best-ips.txt').write_text('172.64.229.1\n')
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / 'notify_called').exists())
        self.assertEqual([p.name for p in self.runtime.iterdir()], ['best-ips.txt'])

    def test_full_scan_fallback_releases_the_inherited_lock(self):
        result = self.run_launcher(code=75)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'force_called').exists())

    def test_ipv6_mode_preserves_both_ram_lists_and_cleans_temporary_data(self):
        for name in ('best-ips.txt', 'best-ips-v6.txt'):
            (self.runtime / name).write_text('backup\n')
        (self.runtime / 'scratch.csv').write_text('temporary')
        result = self.run_launcher(mode='--ipv6')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.runtime.iterdir()), ['best-ips-v6.txt', 'best-ips.txt'])

    def test_busy_scan_causes_health_and_post_cleanup_to_skip_safely(self):
        import fcntl
        (self.runtime / 'report.txt').write_text('in-progress report')
        with self.lock.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for mode in ('--health', 'cleanup-health'):
                result = self.run_launcher(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.runtime / 'report.txt').exists())
        self.assertFalse((self.root / 'force_called').exists())


if __name__ == '__main__':
    unittest.main()
