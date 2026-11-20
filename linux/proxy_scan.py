#!/usr/bin/env python3
"""Manual, RAM-only third-party CF endpoint selection. Never writes DNS."""
import concurrent.futures as futures
import csv
import datetime as dt
import http.client
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
COLOS = ('HKG', 'NRT', 'SIN')
SOURCE = 'https://raw.githubusercontent.com/skfoa/tg-proxy-fetcher/main/data/scan_ips.csv'
SOURCE_LIMIT = 24 * 1024 * 1024
BRANDS = (
    ('Tencent', ('tencent',)), ('Alibaba', ('alibaba', 'aliyun')),
    ('Huawei', ('huawei',)), ('AWS', ('amazon',)), ('Oracle', ('oracle',)),
    ('Azure', ('microsoft',)), ('Google', ('google',)), ('IT7', ('it7', 'bandwagon')),
    ('DMIT', ('dmit',)), ('Vultr', ('vultr',)), ('DigitalOcean', ('digitalocean',)),
    ('Linode', ('linode', 'akamai')), ('OVH', ('ovh',)), ('xTom', ('xtom',)),
    ('Akile', ('akile',)), ('UCloud', ('ucloud',)), ('Evoxt', ('evoxt',)),
)


class Problem(RuntimeError):
    """Only fixed, credential-free messages may reach reports."""


class RateLimited(Problem):
    pass


def key(row):
    return row['ip'], row['port']


def endpoint(ip, port, colo, brand):
    address = ipaddress.IPv4Address(ip)
    if not address.is_global or address.is_multicast or not re.fullmatch(r'[0-9]{1,5}', str(port)) or not 1 <= int(port) <= 65535:
        raise ValueError('Invalid public IPv4 endpoint')
    if colo not in COLOS or not re.fullmatch(r'[A-Za-z0-9 ._-]{1,40}', brand):
        raise ValueError('Invalid endpoint metadata')
    return dict(ip=str(address), port=int(port), colo=colo, brand=brand)


def seed_rows(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if not {'ip', 'port', 'colo', 'provider'}.issubset(reader.fieldnames or []):
        raise Problem('反代候选文件缺少 ip、port、colo、provider 字段。')
    rows = {}
    for index, row in enumerate(reader):
        if index >= 256:
            raise Problem('本机反代候选最多允许 256 条。')
        try:
            item = endpoint(row['ip'], row['port'], row['colo'], row['provider'])
        except (ValueError, TypeError, KeyError):
            raise Problem('本机反代候选包含不合法的地址、端口或地区。') from None
        rows[key(item)] = item
    if not rows:
        raise Problem('本机反代候选文件为空。')
    return list(rows.values())


def remote_rows(text, limit=5000):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    required = {'ip', 'port', 'tls', 'colo', 'asn', 'isp', 'fail_count'}
    if not required.issubset(reader.fieldnames or []):
        raise Problem('远程候选池格式不完整。')
    rows = {}
    for index, row in enumerate(reader):
        if index >= 100000:
            raise Problem('远程候选池行数超出限制。')
        if row.get('tls') != 'true' or row.get('fail_count') != '0' or row.get('colo') not in COLOS:
            continue
        label = (row.get('asn', '') + ' ' + row.get('isp', '')).lower()
        brand = next((name for name, aliases in BRANDS if any(alias in label for alias in aliases)), None)
        if not brand:
            continue
        try:
            item = endpoint(row['ip'], row['port'], row['colo'], brand)
        except (ValueError, TypeError, KeyError):
            continue
        rows[key(item)] = item
        if len(rows) > limit:
            raise Problem('远程候选数量超出本轮配置限制。')
    return list(rows.values())


class Settings:
    def __init__(self, env=None, base=BASE):
        env = os.environ if env is None else env
        def integer(name, default, low, high):
            try:
                value = int(env.get(name, default))
            except (ValueError, TypeError):
                raise Problem('反代优选配置中的数字不合法。') from None
            if not low <= value <= high:
                raise Problem('反代优选配置超出允许范围。')
            return value
        self.output = Path(env.get('CFST_OUTPUT_DIR', '/run/cf-optimizer')).resolve()
        self.seed_file = Path(env.get('CF_PROXY_CANDIDATES_FILE', str(base / 'proxy-candidates.csv')))
        self.interface = env.get('CF_TG_DIRECT_INTERFACE', 'eth0')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', self.interface):
            raise Problem('请配置合法的直连物理网卡。')
        self.refresh = env.get('CF_PROXY_REFRESH_POOL', '1') == '1'
        self.verify_node = env.get('CF_PROXY_VERIFY_NODE', '0') == '1'
        if env.get('CF_PROXY_REFRESH_POOL', '1') not in ('0', '1') or env.get('CF_PROXY_VERIFY_NODE', '0') not in ('0', '1'):
            raise Problem('CF_PROXY_REFRESH_POOL / VERIFY_NODE 只能为 0 或 1。')
        self.host = env.get('CFST_IPV6_NODE_HOST', '')
        self.path = env.get('CFST_IPV6_NODE_PATH', '')
        if self.verify_node and (not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', self.host)
                                 or not self.path.startswith('/') or len(self.path) > 2048
                                 or any(ord(c) < 33 or ord(c) > 126 for c in self.path)):
            raise Problem('反代节点验证需要本机配置合法的节点 Host 和 WS 路径。')
        self.proxy = env.get('TELEGRAM_PROXY_URL', 'http://127.0.0.1:7890')
        parsed = urlsplit(self.proxy)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise Problem('候选池获取需要配置 HTTP(S) 代理。')
        self.max_candidates = integer('CF_PROXY_MAX_CANDIDATES', 5000, 9, 10000)
        self.public_per_colo = integer('CF_PROXY_PUBLIC_PER_COLO', 20, 3, 60)
        self.max_nodes = integer('CF_PROXY_MAX_NODE_CANDIDATES', 30, 3, 30)
        self.node_rounds = integer('CF_PROXY_NODE_ROUNDS', 5, 2, 10)
        self.download_rounds = integer('CF_PROXY_DOWNLOAD_ROUNDS', 3, 2, 3)
        self.download_count = integer('CF_PROXY_DOWNLOAD_PER_COLO', 3, 1, 5)
        self.download_bytes = integer('CF_PROXY_DOWNLOAD_BYTES', 4194304, 1048576, 8388608)
        self.download_timeout = integer('CF_PROXY_DOWNLOAD_TIMEOUT', 8, 3, 20)
        self.workers = integer('CF_PROXY_TCP_WORKERS', 24, 1, 32)
        try:
            self.min_mbps = float(env.get('CF_PROXY_MIN_MBPS', '8'))
        except (ValueError, TypeError):
            raise Problem('反代最低速度配置不合法。') from None
        if not math.isfinite(self.min_mbps) or not 0 < self.min_mbps <= 10000:
            raise Problem('反代最低速度配置不合法。')
        try:
            self.seeds = seed_rows(self.seed_file.read_text(encoding='utf-8'))
        except OSError:
            raise Problem('无法读取本机反代候选文件。') from None
        # A remote CSV can never add permission to disclose the private node path.
        self.allowed_nodes = frozenset(key(p) for p in self.seeds)

    def check_runtime(self):
        if not str(self.output).startswith('/run/') or self.output.is_symlink():
            raise Problem('反代运行目录必须位于 /run 内存文件系统。')
        result = subprocess.run(['stat', '-f', '-c', '%T', str(self.output)],
                                capture_output=True, text=True, timeout=3)
        if result.returncode or result.stdout.strip() != 'tmpfs':
            raise Problem('反代运行目录不是 tmpfs。')
        if not (Path('/sys/class/net') / self.interface / 'device').is_dir():
            raise Problem('反代测速网卡不是物理设备。')
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.interface.encode() + b'\0')
        except OSError:
            raise Problem('无法绑定物理网卡；未退回代理测速。') from None


def atomic_write(path, text):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=str(path.parent),
                                         prefix='.proxy-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def status(settings, message):
    print(message, flush=True)
    atomic_write(settings.output / 'status.json', json.dumps({'message': message,
                 'updated_at': dt.datetime.now().astimezone().isoformat(timespec='seconds')}, ensure_ascii=False))
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.sendto(('STATUS=' + message).encode(), '\0' + address[1:] if address.startswith('@') else address)
        except OSError:
            pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_pool(settings):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': settings.proxy, 'https': settings.proxy}), NoRedirect())
    with opener.open(SOURCE, timeout=35) as response:
        payload = response.read(SOURCE_LIMIT + 1)
    if len(payload) > SOURCE_LIMIT:
        raise Problem('远程候选池过大。')
    return remote_rows(payload.decode('utf-8-sig'), settings.max_candidates)


class Prober:
    def __init__(self, settings):
        self.settings = settings
        self.context = ssl.create_default_context()
        self.stopped = threading.Event()

    def connect(self, row, timeout=4):
        if self.stopped.is_set():
            raise RateLimited('测速接口限流，本轮停止。')
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.settimeout(timeout)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.settings.interface.encode() + b'\0')
            raw.connect(key(row))
            return raw
        except BaseException:
            raw.close()
            raise

    def tcp(self, row):
        started = time.monotonic()
        try:
            with self.connect(row, 1.8):
                return dict(row, tcp_ms=(time.monotonic() - started) * 1000)
        except OSError:
            return None

    def probe(self, row, node=False, download=False):
        # Check membership BEFORE accessing/sending the private hostname/path.
        if node and (not self.settings.verify_node or key(row) not in self.settings.allowed_nodes):
            raise Problem('该 IP:端口未列入本机节点验证允许列表。')
        host = self.settings.host if node else 'speed.cloudflare.com'
        path = self.settings.path if node else ('/__down?bytes=' + str(self.settings.download_bytes) if download else '/cdn-cgi/trace')
        started = time.monotonic()
        try:
            with self.connect(row) as raw:
                tcp = time.monotonic()
                with self.context.wrap_socket(raw, server_hostname=host) as conn:
                    conn.settimeout(5)
                    extra = ('Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n' if node else 'Connection: close\r\n')
                    conn.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: ' + host + '\r\n' + extra + 'User-Agent: cf-optimizer-proxy/1\r\nAccept-Encoding: identity\r\n\r\n').encode('ascii'))
                    response = http.client.HTTPResponse(conn)
                    response.begin()
                    headers_at = time.monotonic()
                    if response.status == 429:
                        self.stopped.set()
                        raise RateLimited('测速接口返回 429，本轮停止；未更新候选结果。')
                    result = dict(ok=False, status=response.status,
                                  colo=(response.getheader('cf-ray') or '').rsplit('-', 1)[-1],
                                  tcp_ms=round((tcp - started) * 1000, 2),
                                  ready_ms=round((headers_at - started) * 1000, 2))
                    if node:
                        result['ok'] = (response.status == 101
                                        and response.getheader('upgrade', '').lower() == 'websocket'
                                        and response.getheader('sec-websocket-accept') == 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')
                        return result
                    if download and response.status == 200:
                        size = 0
                        deadline = headers_at + self.settings.download_timeout
                        while size < self.settings.download_bytes:
                            left = deadline - time.monotonic()
                            if left <= 0:
                                break
                            conn.settimeout(min(3, left))
                            block = response.read1(min(65536, self.settings.download_bytes - size))
                            if not block:
                                break
                            size += len(block)
                        elapsed = max(time.monotonic() - headers_at, 0.000001)
                        result.update(ok=size == self.settings.download_bytes, bytes=size,
                                      mbps=round(size * 8 / elapsed / 1000000, 2))
                        return result
                    if download:
                        return result
                    payload = response.read(8192).decode('utf-8', 'replace')
                    fields = dict(line.split('=', 1) for line in payload.splitlines() if '=' in line)
                    result.update(ok=response.status == 200 and fields.get('h') == host and fields.get('colo') in COLOS,
                                  colo=fields.get('colo', ''))
                    return result
        except (RateLimited, Problem):
            raise
        except (OSError, ValueError, http.client.HTTPException):
            return dict(ok=False, error='连接、证书或响应检查失败')


def parallel(rows, operation, workers, progress=None):
    results = []
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(operation, row): row for row in rows}
        try:
            for index, future in enumerate(futures.as_completed(pending), 1):
                result = future.result()
                if result is not None:
                    results.append((pending[future], result))
                if progress and index % 250 == 0:
                    progress(index)
        except BaseException:
            for future in pending:
                future.cancel()
            raise
    return results


def choose(rows, limit, score, colo_field='colo', per_brand=6, preferred=()):
    """Bounded regional slots; local explicit endpoints retain priority."""
    selected = []
    preferred = set(preferred)
    for colo in COLOS:
        counts = {}
        region = sorted((row for row in rows if row[colo_field] == colo),
                        key=lambda row: (key(row) not in preferred, score(row)))
        for row in region:
            if counts.get(row['brand'], 0) >= per_brand:
                continue
            selected.append(row)
            counts[row['brand']] = counts.get(row['brand'], 0) + 1
            if sum(1 for item in selected if item[colo_field] == colo) >= limit:
                break
    return selected


def qualified_downloads(row, samples, settings):
    if len(samples) != settings.download_rounds:
        return False
    return all(r.get('ok') and r.get('colo') == row['colo'] and math.isfinite(r.get('mbps', 0))
               and r.get('mbps', 0) >= settings.min_mbps for r in samples)


def report_text(settings, selected, pending, notes, total, tested):
    now = dt.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
    lines = ['🚀 CF 反代优选报告', now, '直连测速｜候选 {}｜公开检查 {}'.format(total, tested),
             '节点验证：' + ('仅本机允许列表' if settings.verify_node else '未启用；以下仅公开 CF 测试')]
    for colo in COLOS:
        lines.extend(['', {'HKG': '🇭🇰 香港 HKG', 'NRT': '🇯🇵 东京 NRT', 'SIN': '🇸🇬 新加坡 SIN'}[colo]])
        rows = selected.get(colo, [])
        for row in rows:
            lines.append('{}:{}｜{}｜TCP {:.0f}ms｜{:.1f} Mbps'.format(row['ip'], row['port'], row['brand'], row['tcp_ms'], row['mbps']))
        if not rows:
            lines.append('本轮没有全部复测达标的候选')
    if pending:
        lines.extend(['', '🆕 公开检查通过，待本机允许后才能验证节点：'])
        for row in pending:
            lines.append('{} {}:{} ({})'.format(row['colo'], row['ip'], row['port'], row['brand']))
    lines.extend(['', 'TCP 为到入口的延迟；下载为 CF 短测。', '保留地址和端口；本模式不修改 DNS。'])
    lines.extend(notes)
    return '\n'.join(lines)[:3900]


def scan(settings, prober=None, fetcher=fetch_pool):
    prober = prober or Prober(settings)
    notes = []
    rows = {}
    if settings.refresh:
        status(settings, '正在获取公开反代候选池')
        try:
            rows.update((key(row), row) for row in fetcher(settings))
        except Exception:
            notes.append('候选池获取失败，本轮仅测试本机候选。')
    rows.update((key(row), row) for row in settings.seeds)
    status(settings, '反代 TCP 初筛：{} 条'.format(len(rows)))
    tcp = [result for _, result in parallel(list(rows.values()), prober.tcp, settings.workers,
           lambda n: status(settings, '反代 TCP 初筛 {}/{}'.format(n, len(rows))))]
    candidates = choose(tcp, settings.public_per_colo, lambda row: row['tcp_ms'],
                        preferred=settings.allowed_nodes, per_brand=settings.public_per_colo)
    status(settings, '公开 CF 检查：{} 条'.format(len(candidates)))
    public = []
    for row, result in parallel(candidates, prober.probe, 6):
        if result.get('ok') and result.get('colo') in COLOS:
            public.append(dict(row, colo=result['colo'], tcp_ms=result['tcp_ms'], ready_ms=result['ready_ms']))
    unapproved = [row for row in public if key(row) not in settings.allowed_nodes]
    pending = choose(unapproved, 1, lambda row: row['ready_ms']) if settings.verify_node else []
    if settings.verify_node:
        # At most 30 explicitly listed endpoints, round-robin across regions.
        allowed = [row for row in public if key(row) in settings.allowed_nodes]
        by_colo = {c: sorted((r for r in allowed if r['colo'] == c), key=lambda r: r['ready_ms']) for c in COLOS}
        to_verify = []
        for index in range(max((len(r) for r in by_colo.values()), default=0)):
            for colo in COLOS:
                if index < len(by_colo[colo]) and len(to_verify) < settings.max_nodes:
                    to_verify.append(by_colo[colo][index])
        status(settings, '实际节点验证：{} 条，{} 轮'.format(len(to_verify), settings.node_rounds))
        successful = []
        history = {key(row): [] for row in to_verify}
        for _ in range(settings.node_rounds):
            for row, result in parallel(to_verify, lambda r: prober.probe(r, node=True), 6):
                history[key(row)].append(result)
        for row in to_verify:
            samples = history[key(row)]
            if len(samples) == settings.node_rounds and all(r.get('ok') and r.get('colo') == row['colo'] for r in samples):
                successful.append(dict(row, tcp_ms=statistics.median(r['tcp_ms'] for r in samples),
                                       ready_ms=statistics.median(r['ready_ms'] for r in samples)))
    else:
        successful = public
    targets = choose(successful, settings.download_count, lambda row: row['ready_ms'], per_brand=settings.download_count)
    downloads = {key(row): [] for row in targets}
    for round_number in range(settings.download_rounds):
        order = targets[round_number:] + targets[:round_number]
        for index, row in enumerate(order, 1):
            status(settings, '反代下载复测 {}/{}：第 {}/{} 条'.format(round_number + 1, settings.download_rounds, index, len(targets)))
            downloads[key(row)].append(prober.probe(row, download=True))
    selected = {colo: [] for colo in COLOS}
    for row in targets:
        samples = downloads[key(row)]
        if qualified_downloads(row, samples, settings):
            selected[row['colo']].append(dict(ip=row['ip'], port=row['port'], colo=row['colo'], brand=row['brand'],
                                            tcp_ms=round(row['tcp_ms'], 2), mbps=round(statistics.median(r['mbps'] for r in samples), 2),
                                            node_verified=settings.verify_node, node_rounds=settings.node_rounds if settings.verify_node else 0))
    for values in selected.values():
        values.sort(key=lambda row: (-row['mbps'], row['tcp_ms']))
    report = report_text(settings, selected, pending, notes, len(rows), len(candidates))
    # Only replace the previous RAM snapshot after this run finishes successfully.
    payload = dict(generated_at=dt.datetime.now().astimezone().isoformat(timespec='seconds'),
                   node_verified=settings.verify_node, regions=selected, unverified=pending)
    atomic_write(settings.output / 'best-proxy-ips.json', json.dumps(payload, ensure_ascii=False))
    atomic_write(settings.output / 'report.txt', report)
    status(settings, '反代优选完成；等待代理 TG 推送')
    print(report, flush=True)
    return payload


def main():
    settings = Settings()
    settings.check_runtime()
    if sys.argv[1:] == ['--proxy-check']:
        print('PROXY_CONFIG_OK seeds={} node_verification={} regions=HKG,NRT,SIN'.format(len(settings.seeds), int(settings.verify_node)))
        return
    if sys.argv[1:] != ['--proxy']:
        raise Problem('Usage: cf-optimizer [--proxy|--proxy-check]')
    try:
        scan(settings)
    except Exception as error:
        message = str(error) if isinstance(error, Problem) else '反代优选执行失败，未使用旧结果冒充本轮结果。'
        atomic_write(settings.output / 'report.txt', '⚠️ CF 反代优选失败\n' + message + '\nDNS 未修改；旧 RAM 候选未作为本轮结果推送。')
        raise


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, Problem) else '反代优选执行失败。', file=sys.stderr)
        sys.exit(1)
