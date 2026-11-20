#!/bin/bash
# One timer, sequential scans. A failed official scan must not skip proxy checks.
set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
[[ $EUID == 0 ]] || { echo 'Run with sudo.' >&2; exit 1; }
trap 'exit 143' TERM INT HUP
result=0
/usr/local/sbin/cf-optimizer --scheduled || result=$?
proxy_result=0
/usr/local/sbin/cf-optimizer --proxy-scheduled || proxy_result=$?
if [[ "$result" == 0 ]]; then result=$proxy_result; fi
exit "$result"
