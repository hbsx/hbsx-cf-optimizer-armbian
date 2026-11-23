#!/usr/bin/env python3
"""Owner-only reverse-proxy DNS commands; snapshots and probes remain in RAM."""
import copy
import contextlib
import datetime as dt
import ipaddress
import json
import math
import os
import re
import stat

import dns_update
import proxy_scan
import telegram_control as control

USAGE = '/tj 查询反代 DNS 和候选\n/usetj [HKG|NRT|SIN] [名次] 切换优选\n/settj IPv4 指定本机允许列表内的反代 IP'


def parse(text):
    words = text.strip().split()
    if not words or words[0] not in ('/tj', '/usetj', '/settj'):
        return None
    command, args = words[0], words[1:]
    if command == '/tj' and not args:
        return command, '', 1
    if command == '/usetj' and len(args) <= 2:
        region, rank = '', 1
        if args and args[0] in proxy_scan.COLOS:
            region, args = args[0], args[1:]
        if args:
            if len(args) != 1 or not re.fullmatch(r'[1-9]|10', args[0]):
                raise control.Problem(USAGE)
            rank = int(args[0])
        return command, region, rank
    if command == '/settj' and len(args) == 1:
        try:
            address = ipaddress.IPv4Address(args[0])
            if not address.is_global or address.is_multicast:
                raise ValueError()
            return command, str(address), 1
        except ValueError:
            pass
    raise control.Problem(USAGE)


def configuration(settings):
    env = settings.proxy_control_env
    name = env.get('CF_PROXY_DNS_NAME', '').lower().rstrip('.')
    if not name or name == settings.dns[2]:
        raise control.Problem('请配置独立的 CF_PROXY_DNS_NAME；原优选域名不会被修改。')
    values = dict(env, CF_DNS_ENABLED='1', CF_DNS_NAME=name)
    dns = dns_update.configuration(values)
    port_text = env.get('CF_PROXY_DNS_PORT', '443')
    if not re.fullmatch(r'[1-9][0-9]{0,4}', port_text) or not 1 <= int(port_text) <= 65535:
        raise control.Problem('反代 DNS 端口配置不合法。')
    config = proxy_scan.Settings(dict(env, CF_PROXY_VERIFY_NODE='1'), base=control.BASE)
    return dns, int(port_text), config


def snapshot(config, port, now=None):
    path = config.output / 'best-proxy-ips.json'
    try:
        with path.open('r', encoding='utf-8') as f:
            text = f.read(65537)
        if len(text) > 65536:
            raise ValueError()
        data = json.loads(text)
        generated = dt.datetime.fromisoformat(data['generated_at'])
        if generated.tzinfo is None:
            raise ValueError()
        age = (now or dt.datetime.now(dt.timezone.utc)) - generated
        if not -300 <= age.total_seconds() <= 96 * 3600 or data.get('node_verified') is not True:
            raise ValueError()
        rows, seen = [], set()
        for region in proxy_scan.COLOS:
            for row in data['regions'].get(region, []):
                if (row.get('node_verified') is not True or row.get('port') != port
                        or row.get('colo') != region or proxy_scan.key(row) not in config.allowed_nodes):
                    continue
                address = ipaddress.IPv4Address(row['ip'])
                if not address.is_global or address.is_multicast:
                    continue
                if any(isinstance(row[k], bool) or not math.isfinite(row[k]) or row[k] <= 0
                       for k in ('mbps', 'tcp_ms')):
                    continue
                if proxy_scan.key(row) not in seen:
                    rows.append(dict(row))
                    seen.add(proxy_scan.key(row))
        return sorted(rows, key=lambda r: (-r['mbps'], r['tcp_ms'], r['ip'])), data['generated_at']
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise control.Problem('RAM 中没有有效的近期反代优选结果；请先运行反代优选。') from None


def verify(row, config):
    config.check_runtime()
    # Prober checks the exact local IP:port allowlist again before private WS data.
    prober = proxy_scan.Prober(config)
    try:
        public = prober.probe(row)
        if not public.get('ok') or public.get('colo') != row['colo']:
            raise control.Problem('反代公开 CF 检查未通过，保留原记录。')
        node = prober.probe(row, node=True)
        if not node.get('ok') or node.get('colo') != row['colo']:
            raise control.Problem('反代实际节点握手未通过，保留原记录。')
        return node['tcp_ms']
    except proxy_scan.Problem:
        raise control.Problem('反代验证失败或接口限流，保留原记录。') from None


def handle(text, settings, client=None, probe=None, sleep=None, lock=None):
    parsed = parse(text)
    if parsed is None:
        raise control.Problem(USAGE)
    command, arg, rank = parsed
    dns, port, config = configuration(settings)
    client = client or control.ControlDNS(*dns)
    if command == '/tj':
        records = client.records()
        lines = [client.name + '（反代端口 {}）'.format(port)]
        lines += [r['type'] + ' ' + r['content'] for r in records] or ['暂无地址记录。']
        try:
            rows, timestamp = snapshot(config, port)
            lines.append('最近优选：' + timestamp + '；同端口候选按下载速度排序')
            for region in proxy_scan.COLOS:
                for index, row in enumerate((r for r in rows if r['colo'] == region), 1):
                    lines.append('{} {}. {}:{} | {:.1f} Mbps | {:.0f} ms'.format(
                        region, index, row['ip'], port, row['mbps'], row['tcp_ms']))
            if not rows:
                lines.append('暂无同端口且通过节点验证的候选。')
        except control.Problem as e:
            lines.append(str(e))
        lines.append(USAGE)
        return '\n'.join(lines)
    if command == '/usetj':
        rows, _ = snapshot(config, port)
        rows = [r for r in rows if not arg or r['colo'] == arg]
        if rank > len(rows):
            raise control.Problem('该地区/名次没有同端口的有效候选；请 /tj 查询。')
        row = rows[rank - 1]
    else:
        rows = [r for r in config.seeds if r['ip'] == arg and r['port'] == port]
        if not rows:
            raise control.Problem('该 IP:端口不在本机节点验证允许列表内，未修改；请 /tj 查询候选。')
        row = rows[0]
    chosen = copy.copy(settings)
    chosen.colo = row['colo']
    def validator(value):
        if value != row['ip'] or (value, port) not in config.allowed_nodes:
            raise control.Problem('反代候选不在允许列表内，未修改。')
        return ipaddress.IPv4Address(value)
    old_records, rounds = [], []
    def observe_before(records):
        old_records[:] = records
    def probe_round(*_):
        index = len(rounds) + 1
        old_samples = []
        try:
            delay = (probe or verify)(row, config)
        except control.Problem:
            rounds.append('第 {} 轮：新 {}:{} 节点检查失败。'.format(index, row['ip'], port))
            raise
        for record in old_records:
            if record['type'] != 'A':
                continue
            ip = record['content']
            if ip == row['ip']:
                old_samples.append((delay, ip))
            elif (ip, port) in config.allowed_nodes:
                measured = proxy_scan.Prober(config).tcp(dict(ip=ip, port=port))
                if measured:
                    old_samples.append((measured['tcp_ms'], ip))
        if old_samples:
            old_delay, old_ip = min(old_samples)
            old = '旧最快 {} {:.2f} ms'.format(old_ip, old_delay)
        else:
            old = '旧 IP 未取得有效 TCP 延迟（不通或不在允许列表）'
        rounds.append('第 {} 轮：{}；新 {} {:.2f} ms；节点检查通过。'.format(
            index, old, row['ip'], delay))
        return delay
    header = ['DNS 比较：{}（端口 {}）'.format(client.name, port),
              '规则：指定反代 IP，两轮节点检查通过后更新。' if command == '/settj'
              else '规则：本轮同端口下载最快候选，两轮节点检查通过后更新。']
    try:
        result = control.switch(row['ip'], chosen, client, validator=validator, probe=probe_round,
                                sleep=sleep, lock=lock, observe_before=observe_before)
    except control.Problem as error:
        raise control.Problem('\n'.join(header + rounds + [str(error)])) from None
    return '\n'.join(header + rounds + [result.replace('/dns', '/tj'),
                '客户端端口保持 {} 不变。'.format(port)])


@contextlib.contextmanager
def scan_update_lock():
    # The root launcher holds fd 9 across the entire scan and TG notification.
    # flock on that inherited open-file description must never be unlocked here.
    try:
        inherited = os.fstat(9)
    except OSError:
        inherited = None
    actual = os.stat('/run/lock/cf-optimizer.lock')
    if (inherited is not None and stat.S_ISREG(inherited.st_mode)
            and (inherited.st_dev, inherited.st_ino) == (actual.st_dev, actual.st_ino)):
        try:
            control.fcntl.flock(9, control.fcntl.LOCK_EX | control.fcntl.LOCK_NB)
        except (OSError, AttributeError):
            raise control.Problem('自动更新无法确认优选运行锁，未提交 DNS 更新。') from None
        yield
    else:
        with control.update_lock():
            yield


def auto_after_scan(scan_settings, payload, client=None, probe=None, sleep=None, lock=None):
    """Only called after this scan successfully replaces its RAM snapshot."""
    env = scan_settings.control_env
    if env.get('CF_PROXY_DNS_AUTO_ENABLED', '0') == '0':
        return '反代 DNS：自动更新未启用。'
    if env.get('CF_PROXY_DNS_AUTO_ENABLED') != '1':
        return '反代 DNS：CF_PROXY_DNS_AUTO_ENABLED 必须为 0 或 1，未修改。'
    try:
        settings = control.Settings(dict(env, CF_TG_CONTROL_ENABLED='1'))
        with (lock or scan_update_lock)():
            # A previous or concurrently replaced snapshot is never this run's result.
            path = settings.runtime / 'best-proxy-ips.json'
            if path.stat().st_size > 65536 or json.loads(path.read_text(encoding='utf-8')) != payload:
                raise control.Problem('本轮优选快照已变化，未提交 DNS 更新。')
            result = handle('/usetj', settings, client=client, probe=probe,
                            sleep=sleep, lock=contextlib.nullcontext)
        return '反代 DNS 自动更新：\n' + result
    except (control.Problem, dns_update.DNSProblem) as e:
        return '反代 DNS 自动更新：' + str(e)
    except Exception:
        return '反代 DNS 自动更新结果未确认；不会自动重试，请 /tj 查询。'
