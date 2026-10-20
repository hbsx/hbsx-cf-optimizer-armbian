import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('installer_platform_under_test', ROOT / 'tools/install_platform.py')
platform = importlib.util.module_from_spec(spec)
spec.loader.exec_module(platform)


def elf_header(elf_class, machine, endian=1):
    header = bytearray(64 if elf_class == 2 else 52)
    header[:7] = b'\x7fELF' + bytes([elf_class, endian, 1])
    header[18:20] = machine.to_bytes(2, 'little')
    return header


class PlatformTests(unittest.TestCase):
    def test_supported_machine_aliases(self):
        groups = {'arm64': ('aarch64', 'arm64'), 'amd64': ('x86_64', 'amd64'), '386': ('i386', 'i486', 'i586', 'i686', 'x86')}
        for expected, machines in groups.items():
            for machine in machines:
                with self.subTest(machine=machine):
                    self.assertEqual(platform.select_platform(machine)[0], expected)

    def test_unsupported_machine_is_rejected(self):
        for machine in ('armv7l', 'riscv64', '', 'x86_64;false'):
            with self.subTest(machine=machine), self.assertRaises(ValueError):
                platform.select_platform(machine)

    def test_accepts_matching_elf_and_rejects_cross_architecture(self):
        variants = [('aarch64', 2, 183), ('x86_64', 2, 62), ('i686', 1, 3)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cfst'
            for source, bits, cpu in variants:
                path.write_bytes(elf_header(bits, cpu))
                for target, _, _ in variants:
                    with self.subTest(source=source, target=target):
                        self.assertEqual(platform.binary_matches(path, target), source == target)

    def test_rejects_missing_truncated_non_elf_and_wrong_endian(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cfst'
            self.assertFalse(platform.binary_matches(path, 'x86_64'))
            for content in (b'', b'#!/bin/sh\n', elf_header(2, 62)[:20], elf_header(2, 62, endian=2)):
                path.write_bytes(content)
                with self.subTest(content=content[:7]):
                    self.assertFalse(platform.binary_matches(path, 'x86_64'))

    def test_cli_reports_platform_and_binary_exit_status(self):
        script = str(ROOT / 'tools/install_platform.py')
        detected = subprocess.run([sys.executable, script, 'x86_64'], capture_output=True, text=True)
        self.assertEqual((detected.returncode, detected.stdout.strip()), (0, 'amd64'))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cfst'
            path.write_bytes(elf_header(1, 3))
            self.assertEqual(subprocess.run([sys.executable, script, 'i386', '--check-binary', str(path)]).returncode, 0)
            self.assertEqual(subprocess.run([sys.executable, script, 'x86_64', '--check-binary', str(path)]).returncode, 1)


if __name__ == '__main__':
    unittest.main()
