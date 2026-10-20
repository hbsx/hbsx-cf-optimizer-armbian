#!/bin/bash
# Install/update the scan-only Armbian edition. Preserve existing credentials.
set -euo pipefail
umask 027
[[ $EUID == 0 ]] || { echo 'Please run with sudo.' >&2; exit 1; }
[[ $(uname -m) == aarch64 ]] || { echo 'This installer currently supports ARM64/aarch64.' >&2; exit 1; }
[[ $(stat -f -c %T /run) == tmpfs ]] || { echo '/run must be a RAM filesystem.' >&2; exit 1; }
src=$(cd "$(dirname "$0")" && pwd)
dst=/opt/cf-optimizer
for command in curl python3 ip flock runuser install tar systemctl useradd; do
    command -v "$command" >/dev/null || { echo "Missing dependency: $command" >&2; exit 1; }
done
for file in cf-optimizer nrt_scan.py run_scan_linux.sh telegram_notify.py telegram_notify.sh env.linux.sh.example cf-optimizer.service cf-optimizer.timer; do
    test -f "$src/linux/$file" || { echo "Missing source: linux/$file" >&2; exit 1; }
done
tmp=$(mktemp -d /run/cf-optimizer-install.XXXXXXXX)
trap 'rm -rf -- "$tmp"' EXIT
if [[ ! -x "$dst/cfst" ]]; then
    curl -fL --connect-timeout 10 --max-time 180 --retry 2 \
        https://github.com/XIU2/CloudflareSpeedTest/releases/download/v2.3.5/cfst_linux_arm64.tar.gz -o "$tmp/cfst.tar.gz"
    tar -xzf "$tmp/cfst.tar.gz" -C "$tmp" cfst_linux_arm64/cfst
fi
systemctl stop cf-optimizer.service 2>/dev/null || true
getent passwd cfoptimizer >/dev/null || useradd --system --home-dir /run/cf-optimizer --shell /usr/sbin/nologin cfoptimizer
install -d -o root -g cfoptimizer -m 750 "$dst"
if [[ -f "$tmp/cfst_linux_arm64/cfst" ]]; then
    install -o root -g root -m 755 "$tmp/cfst_linux_arm64/cfst" "$dst/cfst"
fi
for file in nrt_scan.py run_scan_linux.sh telegram_notify.py telegram_notify.sh env.linux.sh.example; do
    install -o root -g cfoptimizer -m 640 "$src/linux/$file" "$dst/$file"
done
install -o root -g cfoptimizer -m 640 "$src/ip.txt" "$dst/ip.txt"
if [[ ! -f "$dst/env.sh" ]]; then
    install -o cfoptimizer -g cfoptimizer -m 600 "$src/linux/env.linux.sh.example" "$dst/env.sh"
fi
install -o root -g root -m 755 "$src/linux/cf-optimizer" /usr/local/sbin/cf-optimizer
install -o root -g root -m 644 "$src/linux/cf-optimizer.service" /etc/systemd/system/cf-optimizer.service
install -o root -g root -m 644 "$src/linux/cf-optimizer.timer" /etc/systemd/system/cf-optimizer.timer
systemd-analyze verify /etc/systemd/system/cf-optimizer.service /etc/systemd/system/cf-optimizer.timer
systemctl daemon-reload
cf-optimizer --check
systemctl enable --now cf-optimizer.timer
echo 'Installed. Configure /opt/cf-optimizer/env.sh. Daily schedule: 03:00 Asia/Shanghai.'
echo 'Manual run: systemctl start --no-block cf-optimizer'
echo 'Runtime IP list (RAM only): /run/cf-optimizer/best-ips.txt'
