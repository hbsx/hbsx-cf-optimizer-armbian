#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
set -a
source ./env.sh
set +a
exec python3 ./telegram_notify.py
