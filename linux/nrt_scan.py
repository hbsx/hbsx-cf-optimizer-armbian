#!/usr/bin/env python3
"""Native TCP probes + verified CF trace colos + bounded curl throughput tests."""
import concurrent.futures as futures
import csv
import datetime as dt
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import dns_update

BASE = Path(__file__).resolve().parent
OUTPUT = Path(os.environ.get('CFST_OUTPUT_DIR', '/run/cf-optimizer'))
STARTED = time.monotonic()
MODE = sys.argv[1] if len(sys.argv) > 1 else '--force'
IPV6 = MODE == '--ipv6'
IPV6_SAMPLES = int(os.environ.get('CFST_IPV6_SAMPLES_PER_CIDR', 64))
IPV6_NODE_PORT = int(os.environ.get('CFST_IPV6_NODE_PORT', 2096))
IPV6_NODE_HOST = os.environ.get('CFST_IPV6_NODE_HOST', '')
IPV6_NODE_PATH = os.environ.get('CFST_IPV6_NODE_PATH', '')
COLO = os.environ.get('CFST_COLO', 'NRT')
AUTO = os.environ.get('CFST_TARGET_CIDRS', 'auto') == 'auto'
CIDRS = os.environ.get('CFST_TARGET_CIDRS', 'auto').split(',')
FINE_SUBNETS = int(os.environ.get('CFST_FINE_SUBNETS', 8))
FINE_VERIFY = int(os.environ.get('CFST_FINE_VERIFY', 200))
PORT = int(os.environ.get('CFST_TEST_PORT', 443))
THREADS = int(os.environ.get('CFST_THREADS', 50))
PINGS = int(os.environ.get('CFST_PING_COUNT', 4))
DOWNLOADS = int(os.environ.get('CFST_DOWNLOAD_COUNT', 5))
ATTEMPTS = int(os.environ.get('CFST_DOWNLOAD_ATTEMPTS', 20))
SECONDS = int(os.environ.get('CFST_DOWNLOAD_SECONDS', 5))
MIN_SPEED = float(os.environ.get('FAST_IP_EXIT_MBPS', 5))
MAX_LATENCY = float(os.environ.get('FAST_IP_MAX_LATENCY_MS', 250))
MAX_LOSS = float(os.environ.get('FAST_IP_MAX_LOSS', 0.1))
POOL_SIZE = int(os.environ.get('FAST_POOL_MAX_SIZE', 20))
TRACE = os.environ.get('CFST_TRACE_URL', 'https://www.cloudflare.com/cdn-cgi/trace')
DOWNLOAD_URL = os.environ['CFST_TEST_URL']
HEADER = ['IP 地址', '已发送', '已接收', '丢包率', '平均延迟', '下载速度(MB/s)', '地区码']

class RateLimited(RuntimeError):
    pass


def log(message):
    print('[{}] {}'.format(dt.datetime.now().astimezone().isoformat(timespec='seconds'), message), flush=True)
    notify = os.environ.get('NOTIFY_SOCKET')
    if notify:
        try:
            address = '\0' + notify[1:] if notify.startswith('@') else notify
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.sendto(('STATUS=' + message).encode(), address)
        except OSError:
            pass
    if MODE != '--check':
        temporary = OUTPUT / '.status.new'
        temporary.write_text(json.dumps(dict(updated_at=dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                                              message=message, elapsed_seconds=round(time.monotonic() - STARTED, 1)), ensure_ascii=False))
        os.replace(str(temporary), str(OUTPUT / 'status.json'))


def validate():
    if MODE not in ('--force', '--smoke', '--check', '--scheduled', '--ipv6'):
        raise ValueError('Usage: cf-optimizer [--force|--smoke|--check|--scheduled]')
    if not re.fullmatch('[A-Z]{3}', COLO):
        raise ValueError('Use one exact three-letter colo')
    if not (1 <= PORT <= 65535 and 1 <= THREADS <= 100 and 1 <= PINGS <= 10 and 1 <= DOWNLOADS <= 50 and 2 <= SECONDS <= 30 and 1 <= POOL_SIZE <= 100):
        raise ValueError('Invalid scan limits')
    if not (math.isfinite(MIN_SPEED) and MIN_SPEED > 0 and math.isfinite(MAX_LATENCY) and MAX_LATENCY > 0 and 0 <= MAX_LOSS <= 1):
        raise ValueError('Invalid selection thresholds')
    if not (1 <= FINE_SUBNETS <= 32 and 20 <= FINE_VERIFY <= 1024 and DOWNLOADS <= ATTEMPTS <= 100):
        raise ValueError('Invalid discovery limits')
    if IPV6:
        if not (4 <= IPV6_SAMPLES <= 256 and 1 <= IPV6_NODE_PORT <= 65535):
            raise ValueError('IPv6: use 4-256 samples per /48 and a valid node port')
        if bool(IPV6_NODE_HOST) != bool(IPV6_NODE_PATH):
            raise ValueError('IPv6 node Host and WebSocket path must be configured together')
        if IPV6_NODE_HOST and (not re.fullmatch(r'[a-zA-Z0-9.-]+', IPV6_NODE_HOST)
                              or not IPV6_NODE_PATH.startswith('/')
                              or any(c in IPV6_NODE_PATH for c in '\r\n ')):
            raise ValueError('Invalid IPv6 node Host or WebSocket path')
        nets = ipv6_ranges()
    else:
        nets = official_ranges(refresh=MODE != '--check') if AUTO else [ipaddress.IPv4Network(c.strip(), strict=True) for c in CIDRS]
    if not IPV6 and not AUTO and sum(n.num_addresses for n in nets) > 65536:
        raise ValueError('Targeted scan limited to 65536 IPs')
    for url in (TRACE, DOWNLOAD_URL):
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.port not in (None, PORT):
            raise ValueError('Use an HTTPS endpoint on the configured port')
    return nets


def ipv6_ranges():
    """These /48s are candidates, never a promise that every address is NRT."""
    official = [ipaddress.IPv6Network(s) for s in ('2400:cb00::/32', '2606:4700::/32',
                '2803:f800::/32', '2405:b500::/32', '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32')]
    text = (BASE / 'ipv6-cidrs.txt').read_text(encoding='utf-8')
    nets = [ipaddress.IPv6Network(line.split('#', 1)[0].strip(), strict=True)
            for line in text.splitlines() if line.split('#', 1)[0].strip()]
    if not 1 <= len(nets) <= 8 or len(set(nets)) != len(nets):
        raise ValueError('IPv6 pool must contain 1-8 distinct /48 networks')
    if any(n.prefixlen != 48 or not any(n.subnet_of(parent) for parent in official) for n in nets):
        raise ValueError('IPv6 pool must use Cloudflare /48 networks')
    return nets


def sample_ipv6(nets, count, rng=None):
    """Bounded sampling; never iterate the enormous IPv6 address space."""
    if not 4 <= count <= 256 or not 1 <= len(nets) <= 8 or any(n.version != 6 or n.prefixlen != 48 for n in nets):
        raise ValueError('Invalid IPv6 sampling limits')
    rng = rng or secrets.SystemRandom()
    samples = []
    for net in nets:
        # Include previously verified suffixes, then vary both host and /64.
        offsets = {0x68120a51, 0x13081e35}
        while len(offsets) < count:
            offsets.add(rng.getrandbits(64 if len(offsets) % 2 == 0 else 80))
        samples.extend(str(net.network_address + offset) for offset in sorted(offsets))
    return samples


def ipv6_node_works(ip):
    """Always test the node port; optionally verify its real TLS/WS endpoint."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as raw:
            raw.settimeout(3)
            raw.connect((ip, IPV6_NODE_PORT))
            if not IPV6_NODE_HOST:
                return True
            with ssl.create_default_context().wrap_socket(raw, server_hostname=IPV6_NODE_HOST) as connection:
                request = ('GET ' + IPV6_NODE_PATH + ' HTTP/1.1\r\nHost: ' + IPV6_NODE_HOST
                           + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           + 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n')
                connection.sendall(request.encode())
                data = b''
                while b'\r\n\r\n' not in data and len(data) < 16384:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                lines = data.decode('iso-8859-1').split('\r\n')
                headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                           for line in lines[1:] if ':' in line}
                return (lines[0].split()[1:2] == ['101']
                        and headers.get('sec-websocket-accept') == 's3pPLMBiTxaQ9kYGzzhZRbK+xOo='
                        and headers.get('upgrade', '').lower() == 'websocket'
                        and headers.get('cf-ray', '').endswith('-' + COLO))
    except (OSError, ValueError):
        return False


def official_ranges(refresh):
    path = BASE / 'ip.txt'
    def parse(text):
        nets = [ipaddress.IPv4Network(line.strip(), strict=True) for line in text.splitlines() if line.strip()]
        if not (10 <= len(nets) <= 100 and sum(n.num_addresses for n in nets) <= 2**26):
            raise ValueError('Unexpected Cloudflare IPv4 list')
        return nets
    if refresh:
        try:
            p = subprocess.run(['curl', '--noproxy', '*', '-4', '-fsS', '--connect-timeout', '5', '--max-time', '20',
                                'https://www.cloudflare.com/ips-v4'], capture_output=True, text=True, check=True, timeout=25)
            nets = parse(p.stdout)
            log('Updated {} official Cloudflare IPv4 ranges'.format(len(nets)))
            return nets
        except Exception:
            log('Official list refresh failed; using validated cached ranges')
    return parse(path.read_text())


def curl_base(url, ip, timeout):
    host = urlsplit(url).hostname
    address = ipaddress.ip_address(ip)
    resolved = '[{}]'.format(address) if address.version == 6 else str(address)
    return ['curl', '--noproxy', '*', '-6' if address.version == 6 else '-4', '-sS', '--connect-timeout', '2', '--max-time', str(timeout),
            '--resolve', '{}:{}:{}'.format(host, PORT, resolved)]


def identify(row):
    cmd = curl_base(TRACE, row[0], 5) + [TRACE, '-w', '\nCURL_META:%{http_code}']
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=7)
    matches = re.findall(r'^colo=([A-Z]{3})\s*$', p.stdout, re.M)
    if p.returncode == 0 and p.stdout.rstrip().endswith('CURL_META:200') and matches == [COLO]:
        return row if not IPV6 or ipv6_node_works(row[0]) else None
    return None


def throughput(ip, work):
    """Repeat finite downloads; count all bytes over actual elapsed time."""
    start = time.monotonic()
    deadline = start + SECONDS
    total = 0
    successes = 0
    while deadline - time.monotonic() >= 0.2:
        left = deadline - time.monotonic()
        headers = work / ('headers-' + ipaddress.ip_address(ip).packed.hex())
        cmd = curl_base(DOWNLOAD_URL, ip, max(0.2, left)) + [DOWNLOAD_URL, '-D', str(headers),
                '-o', '/dev/null', '-w', '%{http_code},%{size_download}']
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=left + 3)
        parts = p.stdout.strip().split(',')
        if parts and parts[0] == '429':
            raise RateLimited('Download endpoint returned HTTP 429; stopped immediately to avoid further requests')
        if len(parts) != 2 or parts[0] != '200' or p.returncode not in (0, 28):
            log('Download rejected for {} (HTTP {}; curl {})'.format(ip, parts[0] if parts else '?', p.returncode))
            return 0.0
        # Confirm the actual DOWNLOAD endpoint also reached NRT, independently
        # of the earlier trace endpoint.
        h = headers.read_text(errors='replace') if headers.exists() else ''
        rays = re.findall(r'^cf-ray:\s*[^\r\n]*-([A-Z]{3})\s*$', h, re.I | re.M)
        if not rays or rays[-1].upper() != COLO:
            return 0.0
        total += int(parts[1])
        successes += 1
    elapsed = time.monotonic() - start
    return total / (1024 * 1024) / elapsed if successes and elapsed > 0 else 0.0


def write_csv(path, rows):
    with path.open('w', encoding='utf-8', newline='') as f:
        csv.writer(f, lineterminator='\n').writerows([HEADER] + rows)


def diverse_candidates(rows):
    """Round-robin /24s so one low-latency but unusable subnet cannot consume
    the whole verification/download budget."""
    groups = {}
    for row in sorted(rows, key=lambda r: float(r[4])):
        address = ipaddress.ip_address(row[0])
        key = str(ipaddress.ip_network(row[0] + ('/48' if address.version == 6 else '/24'), strict=False))
        groups.setdefault(key, []).append(row)
    return [group[i] for i in range(max(map(len, groups.values()), default=0)) for group in groups.values() if i < len(group)]


def smoke_target_ips(nets):
    current = OUTPUT / 'best-ips.txt'
    choices = current.read_text().splitlines() if current.exists() else []
    choices += ['172.64.229.194', '172.64.229.92', '172.64.229.85', '172.64.229.125', '172.64.229.95']
    selected = []
    for value in choices:
        try:
            ip = ipaddress.IPv4Address(value.strip())
        except ValueError:
            continue
        if any(ip in net for net in nets) and str(ip) not in selected:
            selected.append(str(ip))
        if len(selected) == 5:
            break
    return selected


def publish(work, rows, discovered, total, nets, discovery, attempted):
    qualified = [r for r in rows if float(r[5]) >= MIN_SPEED]
    if not qualified:
        raise RuntimeError('No {} IP reached {} MB/s; previous selection retained'.format(COLO, MIN_SPEED))
    speed_order = sorted(qualified, key=lambda r: (-float(r[5]), float(r[4])))[:POOL_SIZE]
    top = sorted(speed_order, key=lambda r: (float(r[4]), -float(r[5])))[:5]
    timestamp = dt.datetime.now().astimezone().isoformat(timespec='seconds')
    subnets = sorted({str(ipaddress.ip_network(r[0] + ('/48' if IPV6 else '/24'), strict=False)) for r in discovered})
    report = ['🚀 CF 优选节点报告 ({} 定向优化)'.format(COLO), '']
    if IPV6:
        report += ['IPv6 网段池：{} 个 /48，每段采样 {} 个，共 {} 个候选。'.format(len(nets), IPV6_SAMPLES, total),
                   '节点验证：{}。'.format('TLS / WebSocket（端口 {}）'.format(IPV6_NODE_PORT) if IPV6_NODE_HOST else 'TCP 端口 {}'.format(IPV6_NODE_PORT))]
    elif discovery:
        report += ['全段发现：{} 个官方 IPv4 网段，覆盖 {:,} 个地址，逐 /24 采样 {} 个。'.format(discovery['official_ranges'], discovery['covered_addresses'], discovery['sampled_ips']),
                   '发现 {} 个 NRT /24 网段；优先细扫其中 {} 个（{} 个 IP）。'.format(discovery['nrt_subnets'], len(nets), total)]
    else:
        report += ['本次对 {} 扫描（{} 个 IP）：'.format('、'.join(map(str, nets)), total)]
    report += ['✅ 细扫后对低延迟候选验证出 {} 个 {} 节点，分布于 {}'.format(len(discovered), COLO, '、'.join(subnets)),
        '下载复测 {} 个候选：{} 个成功，{} 个速度达标。'.format(attempted, len(rows), len(qualified)),
        '本轮筛选 {} 个 {} 优选节点，推荐其中 {} 个。'.format(len(speed_order), COLO, len(top)), '',
        '🇯🇵 日本 ({}) 延迟最优 Top {}'.format(COLO, len(top))]
    report += ['📍 {} | 延迟: {} ms | {} MB/s'.format(r[0], r[4], r[5]) for r in top]
    report += ['', '综合推荐：' + ' / '.join(r[0] for r in top[:2]),
               '测速方式：TCP 延迟；HTTPS 连续限时下载（含连接开销）', '']
    report += (['DNS：手动 IPv6 优选仅报告结果，不修改 A 或 AAAA 记录。'] if IPV6
               else dns_update.compare_and_update([r[0] for r in top], PORT, log))
    report += ['', '本轮耗时：{:.1f} 分钟'.format((time.monotonic() - STARTED) / 60), '更新时间：' + timestamp]
    best_file = 'best-ips-v6.txt' if IPV6 else 'best-ips.txt'
    (work / best_file).write_text(''.join(r[0] + '\n' for r in top), encoding='utf-8')
    (work / 'report.txt').write_text('\n'.join(report) + '\n', encoding='utf-8')
    for name in (best_file, 'report.txt'):
        os.replace(str(work / name), str(OUTPUT / name))
    print('\n'.join(report), flush=True)
    log('Completed: {} selected IPs; report ready'.format(len(speed_order)))


def tcp_probe(targets, total, work, name, allip):
    tcp_file = work / (name + '.csv')
    log('{}: TCP probing {} IPs; port={}, threads={}'.format(name, total, PORT, THREADS))
    if isinstance(targets, list):
        target_file = work / (name + '-targets.txt')
        target_file.write_text('\n'.join(targets) + '\n', encoding='utf-8')
        target_args = ['-f', str(target_file)]
    else:
        target_args = ['-ip', targets]
    cmd = [str(BASE / 'cfst')] + target_args + ['-allip', '-n', str(THREADS), '-t', str(PINGS), '-tp', str(PORT),
           '-dd', '-p', '0', '-tl', str(int(MAX_LATENCY)), '-tlr', str(MAX_LOSS), '-o', str(tcp_file)]
    if not allip:
        cmd.remove('-allip')
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=1800)
    if not tcp_file.exists():
        raise RuntimeError('No reachable TCP candidates')
    with tcp_file.open(encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        next(reader)
        candidates = list(reader)
    return candidates


def check_colos(candidates):
    log('Checking actual data centers for {} reachable IPs'.format(len(candidates)))
    discovered = []
    with futures.ThreadPoolExecutor(max_workers=min(12, THREADS)) as pool:
        jobs = {pool.submit(identify, r): r for r in candidates}
        for i, future in enumerate(futures.as_completed(jobs), 1):
            try:
                row = future.result()
                if row:
                    row[6] = COLO
                    discovered.append(row)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if i % 32 == 0 or i == len(jobs):
                log('Colo checks {}/{}; {} confirmed {}'.format(i, len(jobs), len(discovered), COLO))
    discovered.sort(key=lambda r: float(r[4]))
    return discovered


def scan(nets, work):
    discovery = None
    if AUTO and not IPV6 and MODE != '--smoke':
        count = sum(max(1, n.num_addresses // 256) for n in nets)
        global_rows = tcp_probe(','.join(map(str, nets)), count, work, 'global-discovery', False)
        hits = check_colos(global_rows)
        write_csv(work / 'global-discovery.csv', hits)
        subnets = {}
        for row in hits:
            subnet = ipaddress.IPv4Network(row[0] + '/24', strict=False)
            subnets[str(subnet)] = min(subnets.get(str(subnet), float('inf')), float(row[4]))
        seeds = [ipaddress.IPv4Network(s.strip(), strict=True) for s in os.environ.get('CFST_SEED_CIDRS', '').split(',') if s.strip()]
        seeds = [s for s in seeds if s.prefixlen == 24 and any(s.subnet_of(n) for n in nets)]
        ranked = sorted(subnets, key=lambda s: subnets[s])
        chosen = list(dict.fromkeys([str(s) for s in seeds] + ranked))[:FINE_SUBNETS]
        if not chosen:
            raise RuntimeError('No NRT subnets discovered in the official ranges')
        discovery = dict(official_ranges=len(nets), covered_addresses=sum(n.num_addresses for n in nets), sampled_ips=count,
                         nrt_subnets=len(subnets), chosen_subnets=chosen)
        (work / 'global-discovery-summary.json').write_text(json.dumps(discovery, indent=2))
        log('Global discovery complete: {} NRT /24 subnets; fine scan {}'.format(len(subnets), ','.join(chosen)))
        nets = [ipaddress.IPv4Network(s) for s in chosen]
    targets = ','.join(map(str, nets))
    total = sum(n.num_addresses for n in nets)
    if IPV6:
        targets = sample_ipv6(nets, IPV6_SAMPLES)
        total = len(targets)
    if MODE == '--smoke':
        smoke_ips = smoke_target_ips(nets)
        targets = ','.join(smoke_ips)
        total = len(smoke_ips)
    candidates = tcp_probe(targets, total, work, 'fine-scan', True)
    candidates = diverse_candidates(candidates)
    # Every IP in chosen /24s receives a TCP probe. Verify actual colos for a
    # bounded set of low-latency candidates before the expensive download stage.
    discovered = []
    for offset in range(0, len(candidates), FINE_VERIFY):
        discovered.extend(check_colos(candidates[offset:offset + FINE_VERIFY]))
        if len(discovered) >= ATTEMPTS:
            break
    discovered.sort(key=lambda r: float(r[4]))
    write_csv(work / 'discovery.csv', discovered)
    if not discovered:
        raise RuntimeError('No verified {} nodes in {}'.format(COLO, targets))
    selected = diverse_candidates(discovered)[:min(ATTEMPTS, 5 if MODE == '--smoke' else ATTEMPTS)]
    rows = []
    attempted = 0
    for i, r in enumerate(selected, 1):
        attempted = i
        speed = throughput(r[0], work)
        r[5] = '{:.2f}'.format(speed)
        log('Download {}/{}: {} {} MB/s; TCP {}ms; {}'.format(i, len(selected), r[0], r[5], r[4], COLO))
        if speed > 0:
            rows.append(r)
            if MODE == '--smoke':
                break
        if sum(float(row[5]) >= MIN_SPEED for row in rows) >= DOWNLOADS:
            break
    if MODE == '--smoke':
        if not rows:
            raise RuntimeError('Smoke test did not complete a verified NRT download')
        log('Smoke test passed; production results were not replaced')
        return
    publish(work, rows, discovered, total, nets, discovery, attempted)


def main():
    nets = validate()
    if IPV6:
        route = subprocess.run(['ip', '-6', 'route', 'get', str(nets[0].network_address + 0x68120a51),
                                'uid', str(os.getuid())], check=True, capture_output=True, text=True).stdout
        match = re.search(r'\bdev\s+(\S+)', route)
        if not match or not (Path('/sys/class/net') / match.group(1) / 'device').exists():
            raise RuntimeError('IPv6 route is not through a physical network device')
    if MODE == '--check':
        dns_settings = dns_update.configuration()
        log('Architecture: {}; target: {} ({} ranges, {} addresses); colo: {}; minimum {} MB/s; DNS updates: {}'.format(os.uname().machine, 'ALL official IPv4' if AUTO else ','.join(CIDRS), len(nets), sum(n.num_addresses for n in nets), COLO, MIN_SPEED, dns_settings[2] if dns_settings else 'disabled'))
        subprocess.run(['ip', '-4', 'route', 'get', '104.16.0.1', 'uid', str(os.getuid())], check=True)
        return
    with tempfile.TemporaryDirectory(prefix='.scan.', dir=str(OUTPUT)) as directory:
        scan(nets, Path(directory))


if __name__ == '__main__':
    def stop_scan(signum, frame):
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop_scan)
    try:
        main()
    except Exception as exc:
        log('FAILED: {}. Previous published selection retained.'.format(exc))
        if MODE in ('--force', '--scheduled', '--ipv6'):
            (OUTPUT / 'report.txt').write_text('⚠️ CF 优选未完成（{}）\n\n原因：{}\n未覆盖已有优选 IP。\n更新时间：{}\n'.format(
                COLO, exc, dt.datetime.now().astimezone().isoformat(timespec='seconds')), encoding='utf-8')
        sys.exit(3)
