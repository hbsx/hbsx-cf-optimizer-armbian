#!/bin/bash
# Scan-only: no DNS-update or messaging code is loaded in the scan process.
set -euo pipefail
umask 027
cd "$(dirname "$0")"
set -a
source ./env.sh
set +a
export LC_ALL=C.UTF-8
export PYTHONDONTWRITEBYTECODE=1
exec python3 ./nrt_scan.py "${1:---force}"
