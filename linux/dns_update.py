#!/usr/bin/env python3
"""Two direct TCP comparison rounds before changing existing Cloudflare A records."""
import concurrent.futures as futures
import ipaddress
import json
import math
import os
import re
import socket
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request

MIN_IMPROVEMENT = 0.10
PROBES = 5
ROUND_GAP = 30
MAX_RECORDS = 20


class DNSProblem(RuntimeError):
    """Only sanitized, credential-free messages may be reported."""


def configuration(env=None):
    env = os.environ if env is None else env
    enabled = env.get('CF_DNS_ENABLED', '0')
    if enabled not in ('0', '1'):
        raise DNSProblem('CF_DNS_ENABLED must be 0 or 1')
    if enabled == '0':
        return None
    token = env.get('CLOUDFLARE_API_TOKEN', '')
    zone = env.get('CLOUDFLARE_ZONE_ID', '')
    name = env.get('CF_DNS_NAME', '').lower().rstrip('.')
    if not token or any(c.isspace() for c in token) or not re.fullmatch(r'[a-fA-F0-9]{32}', zone):
        raise DNSProblem('Configure CLOUDFLARE_API_TOKEN and CLOUDFLARE_ZONE_ID in env.sh')
    if len(name) > 253 or '.' not in name or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in name.split('.')):
        raise DNSProblem('Configure one exact CF_DNS_NAME hostname in env.sh')
    return token, zone, name


class Cloudflare:
    def __init__(self, token, zone, name):
        self.token, self.zone, self.name = token, zone, name
        # API control traffic may use a proxy. Measurements below always connect
        # directly to numeric IPv4 addresses under the scanner's DIRECT rule.
        proxy = os.environ.get('CF_DNS_API_PROXY_URL', '')
        if proxy and (urllib.parse.urlsplit(proxy).scheme not in ('http', 'https') or not urllib.parse.urlsplit(proxy).hostname):
            raise DNSProblem('CF_DNS_API_PROXY_URL must be an HTTP(S) proxy URL or empty')
        proxies = {'http': proxy, 'https': proxy} if proxy else {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect())

    def request(self, method, suffix, body=None):
        payload = None if body is None else json.dumps(body).encode('utf-8')
        request = urllib.request.Request('https://api.cloudflare.com/client/v4/zones/' + self.zone + '/dns_records' + suffix,
                                         data=payload, method=method,
                                         headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=20) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            raise DNSProblem('Cloudflare API HTTP {}'.format(error.code)) from None
        except Exception:
            raise DNSProblem('Cloudflare API connection or response failed') from None
        if not isinstance(data, dict) or data.get('success') is not True:
            raise DNSProblem('Cloudflare API did not confirm success')
        return data

    def records(self):
        query = urllib.parse.urlencode({'type': 'A', 'name.exact': self.name, 'per_page': 100})
        data = self.request('GET', '?' + query)
        records = data.get('result')
        info = data.get('result_info', {})
        if (not isinstance(records, list) or not 1 <= len(records) <= MAX_RECORDS
                or info.get('total_pages', 1) != 1 or info.get('total_count', len(records)) != len(records)):
            raise DNSProblem('Expected 1-{} existing A records for the exact hostname'.format(MAX_RECORDS))
        ids, addresses = set(), set()
        for record in records:
            try:
                address = str(ipaddress.IPv4Address(record['content']))
                valid = (record['type'] == 'A' and record['name'].lower().rstrip('.') == self.name
                         and re.fullmatch(r'[a-fA-F0-9]{32}', record['id'])
                         and record.get('proxied') is False and address not in addresses and record['id'] not in ids)
            except (KeyError, ValueError, TypeError, AttributeError):
                valid = False
            if not valid:
                raise DNSProblem('A records must have unique IPv4 addresses and use DNS-only mode')
            ids.add(record['id'])
            addresses.add(address)
        return sorted(records, key=lambda record: record['id'])

    def replace(self, records, addresses):
        patches = [{'id': record['id'], 'content': address} for record, address in zip(records, addresses)]
        # PATCH only content, preserving record count, TTL, comments and settings.
        # Cloudflare executes the batch in one database transaction.
        self.request('POST', '/batch', {'patches': patches})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def fingerprint(records):
    return sorted(json.dumps(record, sort_keys=True) for record in records)


def latency(ip, port):
    samples = []
    for _ in range(PROBES):
        started = time.monotonic()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect((ip, port))
            samples.append((time.monotonic() - started) * 1000)
        except OSError:
            # No automatic replacement when either side has missing samples.
            return None
        time.sleep(0.1)
    result = statistics.median(samples)
    return result if math.isfinite(result) and result > 0 else None


def measure_round(ips, port):
    with futures.ThreadPoolExecutor(max_workers=min(16, len(ips))) as pool:
        jobs = {ip: pool.submit(latency, ip, port) for ip in ips}
        return {ip: job.result() for ip, job in jobs.items()}


def better(new, old):
    return (new is not None and old is not None and math.isfinite(new) and math.isfinite(old)
            and new > 0 and old > 0 and new <= old * (1 - MIN_IMPROVEMENT))


def compare_and_update(candidates, port=443, log=lambda message: None, client=None, measure=None, sleep=None):
    """Return report lines. All decisions and comparison samples stay in memory."""
    lines = []
    attempted_write = False
    try:
        settings = configuration()
        if settings is None:
            return ['DNS：自动更新未启用，保留现有解析。']
        client = client or Cloudflare(*settings)
        measure = measure or measure_round
        sleep = sleep or time.sleep
        records = client.records()
        old_ips = [record['content'] for record in records]
        # Never replace a DNS address with itself based on measurement noise.
        new_ips = list(dict.fromkeys(str(ipaddress.IPv4Address(ip)) for ip in candidates if ip not in old_ips))
        if len(new_ips) < len(records):
            return ['DNS：新候选不足以保留现有 {} 条 A 记录，本轮不更新。'.format(len(records))]
        eligible = set(new_ips)
        scores = {ip: [] for ip in new_ips}
        lines.append('DNS 比较：{}，旧记录取最快 IP；两轮均至少快 10%。'.format(settings[2]))
        for round_number in (1, 2):
            log('DNS latency comparison round {}/2'.format(round_number))
            measured = measure(old_ips + new_ips, port)
            old_values = [measured.get(ip) for ip in old_ips]
            if any(value is None or not math.isfinite(value) or value <= 0 for value in old_values):
                return lines + ['DNS：旧 IP 测量不完整，无法可靠比较，本轮不更新。']
            old_best = min(old_values)
            passed = {ip for ip in new_ips if better(measured.get(ip), old_best)}
            eligible &= passed
            for ip in new_ips:
                value = measured.get(ip)
                if value is not None and math.isfinite(value) and value > 0:
                    scores[ip].append(value)
            values = [measured.get(ip) for ip in new_ips if measured.get(ip) is not None and math.isfinite(measured[ip]) and measured[ip] > 0]
            new_text = '{:.2f} ms'.format(min(values)) if values else '不可用'
            old_best_ip = min(old_ips, key=lambda ip: measured[ip])
            lines.append('第 {} 轮：旧最快 {} {:.2f} ms；新最快 {}；本轮 {} 个达标。'.format(round_number, old_best_ip, old_best, new_text, len(passed)))
            if round_number == 1:
                sleep(ROUND_GAP)
        if len(eligible) < len(records):
            return lines + ['DNS：连续两轮达标的新 IP 不足，本轮保留现有解析。']
        selected = sorted(eligible, key=lambda ip: (statistics.mean(scores[ip]), ip))[:len(records)]
        # Refuse to overwrite edits made while the comparisons were running.
        if fingerprint(client.records()) != fingerprint(records):
            return lines + ['DNS：比较期间记录已变化，本轮不更新。']
        attempted_write = True
        client.replace(records, selected)
        expected = {record['id']: ip for record, ip in zip(records, selected)}
        actual = {record['id']: record['content'] for record in client.records()}
        if actual != expected:
            raise DNSProblem('Cloudflare readback did not match the requested addresses')
        return lines + ['✅ DNS 已更新（{} 条 A 记录）：{}'.format(len(selected), ' / '.join(selected))]
    except Exception as error:
        # Never expose raw API bodies, request headers or tokens in the report.
        reason = str(error) if isinstance(error, DNSProblem) else 'DNS 比较或请求失败'
        result = 'DNS：更新结果未确认，请检查 Cloudflare；不自动重试写入。' if attempted_write else 'DNS：本轮未提交更新，保留现有解析。'
        return lines + [result, '原因：' + reason]
