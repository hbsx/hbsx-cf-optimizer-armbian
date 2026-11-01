#!/usr/bin/env python3
"""Quiet six-hour ICMP health checks; escalate only confirmed faults."""
import datetime as dt
import ipaddress
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time

import dns_update

OUTPUT = Path(os.environ.get('CFST_OUTPUT_DIR', '/run/cf-optimizer'))
NEED_SCAN = 75


def status(message):
    print(message, flush=True)
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        try:
            if address.startswith('@'):
                address = '\0' + address[1:]
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.sendto(('STATUS=' + message).encode(), address)
        except OSError:
            pass


def cached_ips(old_ips, nets):
    path = OUTPUT / 'best-ips.txt'
    values = path.read_text().splitlines() if path.exists() else []
    selected = []
    for value in values:
        try:
            ip = ipaddress.IPv4Address(value.strip())
        except ValueError:
            continue
        text = str(ip)
        if text not in old_ips and text not in selected and any(ip in net for net in nets):
            selected.append(text)
        if len(selected) == 20:
            break
    return selected


def verified_backups(old_ips, needed):
    # Load the throughput/colo machinery only after an actual ping failure.
    import nrt_scan as scanner
    scanner.MODE = '--check'  # Validate cached ranges without refreshing them.
    nets = scanner.validate()
    candidates = cached_ips(old_ips, nets)
    if len(candidates) < needed:
        return []
    selected = []
    with tempfile.TemporaryDirectory(prefix='.health.', dir=str(OUTPUT)) as directory:
        for ip in candidates:
            status('Verifying cached NRT backup {}'.format(ip))
            delay = dns_update.latency(ip, scanner.PORT)
            if not dns_update.valid_latency(delay) or delay > scanner.MAX_LATENCY:
                continue
            row = [ip, '5', '5', '0', str(delay), '0', scanner.COLO]
            if not scanner.identify(row):
                continue
            try:
                speed = scanner.throughput(ip, Path(directory))
            except scanner.RateLimited:
                raise dns_update.DNSProblem('备用测速接口限流，本轮停止，不重复启动全量扫描') from None
            if speed >= scanner.MIN_SPEED:
                selected.append(ip)
            if len(selected) >= needed:
                break
    return selected


def check(client=None, probe=None, sleep=None, backups=None, compare=None):
    """Return (exit code, optional RAM-only report lines). Healthy checks are quiet."""
    settings = dns_update.configuration()
    if settings is None:
        status('Health check skipped; DNS management disabled')
        return 0, None
    client = client or dns_update.Cloudflare(*settings)
    probe = probe or dns_update.ping_round
    sleep = sleep or time.sleep
    backups = backups or verified_backups
    compare = compare or dns_update.compare_and_update
    records = client.records(allow_empty=True) if isinstance(client, dns_update.Cloudflare) else client.records()
    if not records:
        status('No A records; IPv4 health check skipped without report or scan')
        return 0, None
    old_ips = [record['content'] for record in records]
    first = probe(old_ips)
    failed_once = [ip for ip in old_ips if first.get(ip) is False]
    if not failed_once:
        if all(first.get(ip) is True for ip in old_ips):
            status('Healthy; old IPs replied to ping; no report or speed test')
            return 0, None
        return 1, ['⚠️ CF 健康检查异常', 'ping 执行异常，未将其判断为 IP 故障；保留现有解析。']
    status('Old IP ping failed; rechecking in 30 seconds')
    sleep(dns_update.ROUND_GAP)
    second = probe(failed_once)
    failed = [ip for ip in failed_once if second.get(ip) is False]
    if not failed:
        status('Old IP failure not confirmed in second round; DNS unchanged')
        if any(second.get(ip) is None for ip in failed_once):
            return 1, ['⚠️ CF 健康检查异常', '第二轮 ping 执行异常，保留现有解析。']
        return 0, None
    status('Confirmed ping failure; validating cached NRT backups')
    candidates = backups(old_ips, len(failed))
    if len(candidates) < len(failed):
        status('No verified RAM backup; requesting a fresh full scan')
        return NEED_SCAN, None
    if dns_update.fingerprint(client.records()) != dns_update.fingerprint(records):
        return 0, ['CF 健康检查：复测期间 DNS 记录已变化，本轮不更新。']
    lines = compare(candidates, port=int(os.environ.get('CFST_TEST_PORT', '443')), log=status,
                    client=client, failover_only=True)
    return 0, ['🛟 CF 健康检查与故障切换', '初检连续两次 ping 不通：' + ' / '.join(failed), ''] + lines


def main():
    try:
        code, lines = check()
    except Exception as error:
        reason = str(error) if isinstance(error, dns_update.DNSProblem) else '健康检查或备用验证失败'
        code, lines = 1, ['⚠️ CF 健康检查未完成', '保留现有解析。', '原因：' + reason]
    if lines:
        timestamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec='seconds')
        (OUTPUT / 'report.txt').write_text('\n'.join(lines + ['检查时间：' + timestamp]) + '\n', encoding='utf-8')
    return code


if __name__ == '__main__':
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, lambda number, frame: sys.exit(128 + number))
    sys.exit(main())
