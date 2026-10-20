#!/usr/bin/env python3
"""Select an official Linux cfst asset and validate binary compatibility."""
import argparse
from pathlib import Path
import sys

PLATFORMS = {
    'aarch64': ('arm64', 2, 183),
    'arm64': ('arm64', 2, 183),
    'x86_64': ('amd64', 2, 62),
    'amd64': ('amd64', 2, 62),
    'i386': ('386', 1, 3),
    'i486': ('386', 1, 3),
    'i586': ('386', 1, 3),
    'i686': ('386', 1, 3),
    'x86': ('386', 1, 3),
}


def select_platform(machine):
    try:
        return PLATFORMS[machine.lower()]
    except KeyError:
        raise ValueError('Unsupported architecture: {}. Supported: ARM64, x86_64 and 32-bit x86.'.format(machine)) from None


def binary_matches(path, machine):
    _, elf_class, elf_machine = select_platform(machine)
    try:
        with Path(path).open('rb') as binary:
            header = binary.read(64)
    except OSError:
        return False
    return (len(header) >= (64 if elf_class == 2 else 52)
            and header[:4] == b'\x7fELF'
            and header[4] == elf_class
            and header[5:7] == b'\x01\x01'
            and int.from_bytes(header[18:20], 'little') == elf_machine)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('machine', help='Linux uname -m value')
    parser.add_argument('--check-binary', type=Path)
    args = parser.parse_args()
    try:
        platform, _, _ = select_platform(args.machine)
    except ValueError as error:
        parser.error(str(error))
    if args.check_binary:
        return 0 if binary_matches(args.check_binary, args.machine) else 1
    print(platform)
    return 0


if __name__ == '__main__':
    sys.exit(main())
