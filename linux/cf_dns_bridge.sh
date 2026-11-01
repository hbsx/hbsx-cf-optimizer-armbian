#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
set -a
source ./env.sh
set +a
export PYTHONDONTWRITEBYTECODE=1
export HOME=/run/cf-optimizer-bridge
export TMPDIR=/run/cf-optimizer-bridge
exec python3 ./cf_dns_bridge.py "$@"
