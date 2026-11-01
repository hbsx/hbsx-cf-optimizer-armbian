#!/bin/bash
# Install/update the Linux edition. Preserve existing credentials.
set -euo pipefail
umask 027
[[ $EUID == 0 ]] || { echo 'Please run with sudo.' >&2; exit 1; }
[[ $(stat -f -c %T /run) == tmpfs ]] || { echo '/run must be a RAM filesystem.' >&2; exit 1; }
src=$(cd "$(dirname "$0")" && pwd)
dst=/opt/cf-optimizer
for command in curl python3 ip ping flock runuser install tar systemctl systemd-tmpfiles useradd; do
    command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 1; }
done
test -f "$src/tools/install_platform.py" || { echo 'Missing source: tools/install_platform.py' >&2; exit 1; }
machine=$(uname -m)
cfst_arch=$(python3 "$src/tools/install_platform.py" "$machine")
cfst_dir="cfst_linux_${cfst_arch}"
for file in cf_dns_bridge.py cf_dns_bridge.sh cf-optimizer-bridge.service; do
    test -f "$src/linux/$file" || { echo "Missing source: linux/$file" >&2; exit 1; }
done
printf 'Detected architecture: %s; cfst platform: %s\n' "$machine" "$cfst_arch"
for file in cf-optimizer nrt_scan.py ipv6-cidrs.txt dns_update.py health_check.py schedule.py run_scan_linux.sh telegram_notify.py telegram_notify.sh telegram_control.py telegram_control.sh env.linux.sh.example cf-optimizer.service cf-optimizer.timer cf-optimizer-health.service cf-optimizer-health.timer cf-optimizer-bot.service cf-optimizer-tmpfiles.conf; do
    test -f "$src/linux/$file" || { echo "Missing source: linux/$file" >&2; exit 1; }
done
tmp=$(mktemp -d /run/cf-optimizer-install.XXXXXXXX)
trap 'rm -rf -- "$tmp"' EXIT
if [[ ! -x "$dst/cfst" ]] || ! python3 "$src/tools/install_platform.py" "$machine" --check-binary "$dst/cfst"; then
    curl -fL --connect-timeout 10 --max-time 180 --retry 2 \
        "https://github.com/XIU2/CloudflareSpeedTest/releases/download/v2.3.5/${cfst_dir}.tar.gz" -o "$tmp/cfst.tar.gz"
    tar -xzf "$tmp/cfst.tar.gz" -C "$tmp" "$cfst_dir/cfst"
    python3 "$src/tools/install_platform.py" "$machine" --check-binary "$tmp/$cfst_dir/cfst" || {
        echo 'Downloaded binary does not match the detected architecture.' >&2; exit 1;
    }
fi
systemctl stop cf-optimizer.service 2>/dev/null || true
systemctl stop cf-optimizer-health.service 2>/dev/null || true
systemctl stop cf-optimizer-bot.service 2>/dev/null || true
systemctl stop cf-optimizer-bridge.service 2>/dev/null || true
getent passwd cfoptimizer >/dev/null || useradd --system --home-dir /run/cf-optimizer --shell /usr/sbin/nologin cfoptimizer
install -d -o root -g cfoptimizer -m 750 "$dst"
if [[ -f "$tmp/$cfst_dir/cfst" ]]; then
    install -o root -g root -m 755 "$tmp/$cfst_dir/cfst" "$dst/cfst"
fi
for file in nrt_scan.py ipv6-cidrs.txt dns_update.py health_check.py schedule.py run_scan_linux.sh telegram_notify.py telegram_notify.sh telegram_control.py telegram_control.sh env.linux.sh.example; do
    install -o root -g cfoptimizer -m 640 "$src/linux/$file" "$dst/$file"
done
install -o root -g cfoptimizer -m 640 "$src/ip.txt" "$dst/ip.txt"
for file in cf_dns_bridge.py cf_dns_bridge.sh; do
    install -o root -g cfoptimizer -m 640 "$src/linux/$file" "$dst/$file"
done
if [[ ! -f "$dst/env.sh" ]]; then
    install -o cfoptimizer -g cfoptimizer -m 600 "$src/linux/env.linux.sh.example" "$dst/env.sh"
fi
# One-time configuration writes only; no last-run date is saved at runtime.
if ! grep -q '^CFST_SCHEDULE_ANCHOR=' "$dst/env.sh"; then
    printf '\nCFST_SCHEDULE_ANCHOR="%s"\n' "$(TZ=Asia/Shanghai date +%F)" >> "$dst/env.sh"
fi
for setting in 'CFST_SCHEDULE_DAYS=3' 'CF_HEALTH_ENABLED=0' 'CF_DNS_ENABLED=0' 'CF_DNS_NAME=""' 'CLOUDFLARE_ZONE_ID=""' 'CLOUDFLARE_API_TOKEN=""' 'CF_DNS_API_PROXY_URL=""' 'CF_TG_CONTROL_ENABLED=0' 'CF_TG_ALLOWED_USER_ID=""' 'CF_TG_DIRECT_INTERFACE="eth0"'; do
    key=${setting%%=*}
    if ! grep -q "^${key}=" "$dst/env.sh"; then printf '%s\n' "$setting" >> "$dst/env.sh"; fi
done
install -o root -g root -m 755 "$src/linux/cf-optimizer" /usr/local/sbin/cf-optimizer
install -o root -g root -m 644 "$src/linux/cf-optimizer.service" /etc/systemd/system/cf-optimizer.service
install -o root -g root -m 644 "$src/linux/cf-optimizer.timer" /etc/systemd/system/cf-optimizer.timer
install -o root -g root -m 644 "$src/linux/cf-optimizer-health.service" /etc/systemd/system/cf-optimizer-health.service
install -o root -g root -m 644 "$src/linux/cf-optimizer-health.timer" /etc/systemd/system/cf-optimizer-health.timer
install -o root -g root -m 644 "$src/linux/cf-optimizer-bot.service" /etc/systemd/system/cf-optimizer-bot.service
install -o root -g root -m 644 "$src/linux/cf-optimizer-bridge.service" /etc/systemd/system/cf-optimizer-bridge.service
install -o root -g root -m 644 "$src/linux/cf-optimizer-tmpfiles.conf" /etc/tmpfiles.d/cf-optimizer.conf
systemd-tmpfiles --create /etc/tmpfiles.d/cf-optimizer.conf
systemd-analyze verify /etc/systemd/system/cf-optimizer.service /etc/systemd/system/cf-optimizer.timer /etc/systemd/system/cf-optimizer-health.service /etc/systemd/system/cf-optimizer-health.timer /etc/systemd/system/cf-optimizer-bot.service /etc/systemd/system/cf-optimizer-bridge.service
systemctl daemon-reload
cf-optimizer --check
systemctl enable cf-optimizer.timer
systemctl restart cf-optimizer.timer
health_enabled=$(runuser -u cfoptimizer -- /bin/bash -c 'source "$1"; printf "%s" "${CF_HEALTH_ENABLED:-0}"' bash "$dst/env.sh")
if [[ "$health_enabled" == 1 ]]; then
    systemctl enable --now cf-optimizer-health.timer
else
    systemctl disable --now cf-optimizer-health.timer 2>/dev/null || true
fi
bot_enabled=$(runuser -u cfoptimizer -- /bin/bash -c 'source "$1"; printf "%s" "${CF_TG_CONTROL_ENABLED:-0}"' bash "$dst/env.sh")
bridge_enabled=$(runuser -u cfoptimizer -- /bin/bash -c 'source "$1"; printf "%s" "${CF_DNS_BRIDGE_ENABLED:-0}"' bash "$dst/env.sh")
if [[ "$bridge_enabled" == 1 ]]; then
    systemctl disable --now cf-optimizer-bot.service 2>/dev/null || true
    systemctl enable --now cf-optimizer-bridge.service
elif [[ "$bot_enabled" == 1 ]]; then
    systemctl disable --now cf-optimizer-bridge.service 2>/dev/null || true
    systemctl enable --now cf-optimizer-bot.service
else
    systemctl disable cf-optimizer-bot.service 2>/dev/null || true
    systemctl disable cf-optimizer-bridge.service 2>/dev/null || true
fi
echo 'Installed. Configure /opt/cf-optimizer/env.sh. Default: every 3 days at 03:00 Asia/Shanghai.'
echo 'Manual run (bypass cadence): cf-optimizer --force'
echo 'Light health timer: disabled by default; opt in with CF_HEALTH_ENABLED=1. Manual: cf-optimizer --health'
echo 'Runtime IP list (RAM only): /run/cf-optimizer/best-ips.txt'
