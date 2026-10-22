#!/bin/bash
# DIRECT scan and optional DNS comparison. Telegram runs after DIRECT rules end.
set -euo pipefail
umask 027
cd "$(dirname "$0")"
set -a
source ./env.sh
set +a
export LC_ALL=C.UTF-8
export PYTHONDONTWRITEBYTECODE=1
if [[ "${1:-}" == --schedule-check ]]; then exec python3 ./schedule.py; fi
exec python3 ./nrt_scan.py "${1:---force}"
