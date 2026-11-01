#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
set -a
source ./env.sh
set +a
export PYTHONDONTWRITEBYTECODE=1
export HOME=/run/cf-optimizer-bot
export TMPDIR=/run/cf-optimizer-bot
exec python3 ./telegram_control.py "$@"
