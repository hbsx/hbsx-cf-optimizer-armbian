#!/usr/bin/env python3
"""Private-chat DNS control. Cursor, commands and probe results stay in RAM."""
import contextlib
try:
    import fcntl
except ImportError:  # Allow platform-independent unit tests on Windows.
    fcntl = None
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import dns_update

BASE = Path(__file__).resolve().parent
HELP = ('直接发送 IPv4 或 IPv6，或使用 /set IP。\n'
        '验证通过后只保留对应的 A 或 AAAA 地址记录，移除另一类型。\n'
        '/dns 查询当前记录\n/use4 使用 RAM 中最近的 IPv4 优选第一名\n'
        '/use6 使用 RAM 中最近的 IPv6 优选第一名\n/help 查看说明\n'
        '只处理当前私聊账号的新消息；不执行任意命令。')


class Problem(RuntimeError):
    """Only credential-free messages may escape to Telegram."""


class Settings:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        if env.get('CF_TG_CONTROL_ENABLED', '0') != '1':
            raise Problem('TG DNS 控制未启用。')
        self.token = env.get('TELEGRAM_BOT_TOKEN', '')
        chat = env.get('TELEGRAM_CHAT_ID', '')
        user = env.get('CF_TG_ALLOWED_USER_ID', '') or chat
        if not self.token or not re.fullmatch(r'[1-9][0-9]*', chat) or not re.fullmatch(r'[1-9][0-9]*', user):
            raise Problem('请配置机器人和个人私聊 Chat ID / User ID。')
        self.chat, self.user = int(chat), int(user)
        self.interface = env.get('CF_TG_DIRECT_INTERFACE', 'eth0')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', self.interface):
            raise Problem('请配置直连物理网卡。')
        self.colo = env.get('CFST_COLO', 'NRT')
        self.host = env.get('CFST_IPV6_NODE_HOST', '')
        self.path = env.get('CFST_IPV6_NODE_PATH', '')
        self.port = int(env.get('CFST_IPV6_NODE_PORT', '2096'))
        if (not re.fullmatch(r'[A-Z]{3}', self.colo)
                or not re.fullmatch(r'[A-Za-z0-9.-]+', self.host)
                or not self.path.startswith('/') or any(c in self.path for c in '\r\n ')
                or not 1 <= self.port <= 65535):
            raise Problem('请配置真实节点的 TLS/WS Host、路径及端口。')
        self.proxy = env.get('TELEGRAM_PROXY_URL', 'http://127.0.0.1:7890')
        if not self.proxy or urllib.parse.urlsplit(self.proxy).scheme not in ('http', 'https'):
            raise Problem('请配置 TG HTTP(S) 代理。')
        # Explicitly enabled TG control has its own opt-in, independent of the
        # scheduled IPv4 updater's switch. Never rewrite env.sh per command.
        values = dict(env, CF_DNS_ENABLED='1')
        self.dns = dns_update.configuration(values)
        self.runtime = Path(env.get('CFST_OUTPUT_DIR', '/run/cf-optimizer'))


class Telegram:
    def __init__(self, settings):
        self.settings = settings
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': settings.proxy, 'https': settings.proxy}),
            dns_update.NoRedirect())

    def call(self, method, **parameters):
        payload = {key: json.dumps(value) if isinstance(value, (list, dict)) else value
                   for key, value in parameters.items()}
        request = urllib.request.Request('https://api.telegram.org/bot' + self.settings.token + '/' + method,
                                         data=urllib.parse.urlencode(payload).encode())
        try:
            with self.opener.open(request, timeout=55 if method == 'getUpdates' else 15) as response:
                data = json.load(response)
            if not isinstance(data, dict) or data.get('ok') is not True:
                raise Problem('Telegram 请求失败。')
            return data.get('result')
        except urllib.error.HTTPError as error:
            raise Problem('Telegram HTTP {}。'.format(error.code)) from None
        except Exception:
            raise Problem('Telegram 连接或响应失败。') from None

    def send(self, text):
        return self.call('sendMessage', chat_id=self.settings.chat, text=text[:4000],
                         disable_web_page_preview='true')


class ControlDNS(dns_update.Cloudflare):
    def records(self):
        query = urllib.parse.urlencode({'name.exact': self.name, 'per_page': 100})
        data = self.request('GET', '?' + query)
        rows = data.get('result')
        info = data.get('result_info', {})
        if (not isinstance(rows, list) or info.get('total_pages', 1) != 1
                or info.get('total_count', len(rows)) != len(rows) or len(rows) > 100):
            raise Problem('DNS 记录响应不完整，未修改。')
        addresses, ids = [], set()
        for row in rows:
            if not isinstance(row, dict) or row.get('name', '').lower().rstrip('.') != self.name:
                raise Problem('DNS 响应域名不匹配，未修改。')
            kind = row.get('type')
            if kind == 'CNAME':
                raise Problem('目标域名存在 CNAME，未修改。')
            if kind not in ('A', 'AAAA'):
                continue
            try:
                address = ipaddress.ip_address(row['content'])
                valid = (address.version == (4 if kind == 'A' else 6)
                         and re.fullmatch(r'[a-fA-F0-9]{32}', row['id'])
                         and row['id'] not in ids and row.get('proxied') is False
                         and isinstance(row.get('ttl'), int) and row['ttl'] > 0)
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise Problem('只管理目标域名的合法灰云 A / AAAA 记录，未修改。')
            ids.add(row['id'])
            addresses.append(row)
        if len(addresses) > 20:
            raise Problem('目标地址记录过多，未修改。')
        return sorted(addresses, key=lambda row: row['id'])


def candidate(value):
    if not isinstance(value, str) or '%' in value:
        raise Problem('请输入一个不带端口的 IPv4 或 IPv6。')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise Problem('请输入一个不带端口的 IPv4 或 IPv6。') from None
    if address.version == 4:
        nets = [ipaddress.ip_network(line.strip()) for line in (BASE / 'ip.txt').read_text().splitlines() if line.strip()]
    else:
        nets = [ipaddress.ip_network(s) for s in ('2400:cb00::/32', '2606:4700::/32', '2803:f800::/32',
                '2405:b500::/32', '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32')]
    if not any(address in net for net in nets):
        raise Problem('只接受 Cloudflare 官方范围内的 IP。')
    return address


def https_probe(address, host, port, path, interface, websocket=False):
    """Bind every probe to the physical device; never use DNS or proxy sockets."""
    start = time.monotonic()
    with socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM) as raw:
        raw.settimeout(3)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b'\0')
        raw.connect((str(address), port))
        tcp_ms = (time.monotonic() - start) * 1000
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
            if websocket:
                request = ('GET ' + path + ' HTTP/1.1\r\nHost: ' + host
                           + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           + 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n')
            else:
                request = 'GET ' + path + ' HTTP/1.1\r\nHost: ' + host + '\r\nConnection: close\r\n\r\n'
            connection.sendall(request.encode())
            data, deadline = b'', time.monotonic() + 5
            while len(data) < 16384 and time.monotonic() < deadline:
                connection.settimeout(min(3, max(0.1, deadline - time.monotonic())))
                part = connection.recv(4096)
                if not part:
                    break
                data += part
                if websocket and b'\r\n\r\n' in data:
                    break
    text = data.decode('iso-8859-1')
    lines = text.split('\r\n')
    if websocket:
        headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip()
                   for line in lines[1:] if ':' in line}
        ok = (lines[0].split()[1:2] == ['101'] and headers.get('upgrade', '').lower() == 'websocket'
              and headers.get('sec-websocket-accept') == 's3pPLMBiTxaQ9kYGzzhZRbK+xOo=')
        colo = headers.get('cf-ray', '').rsplit('-', 1)[-1]
    else:
        matches = re.findall(r'^colo=([A-Z]{3})\s*$', text.split('\r\n\r\n', 1)[-1], re.M)
        ok = lines[0].split()[1:2] == ['200'] and len(matches) == 1
        colo = matches[0] if matches else ''
    return ok, colo, tcp_ms


def verify(address, settings):
    if not (Path('/sys/class/net') / settings.interface / 'device').is_dir():
        raise Problem('直连网卡不是物理设备，未修改。')
    try:
        trace = https_probe(address, 'www.cloudflare.com', 443, '/cdn-cgi/trace', settings.interface)
        node = https_probe(address, settings.host, settings.port, settings.path, settings.interface, True)
    except (OSError, ValueError):
        raise Problem('新 IP 的直连 TCP / TLS / WebSocket 检查失败，保留原记录。') from None
    if not all(row[0] and row[1] == settings.colo for row in (trace, node)):
        raise Problem('新 IP 未通过 {} 及节点握手验证，保留原记录。'.format(settings.colo))
    return node[2]


@contextlib.contextmanager
def update_lock():
    # The root scan launcher creates this 0644 file. flock on a read-only fd
    # works on local Linux filesystems and does not write or truncate it.
    if fcntl is None:
        raise Problem('TG DNS 控制仅支持 Linux。')
    with open('/run/lock/cf-optimizer.lock', 'r') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Problem('正在运行优选或其他更新，请稍后再发。') from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def switch(value, settings, client, probe=None, sleep=None, lock=None):
    address = candidate(value)
    kind = 'A' if address.version == 4 else 'AAAA'
    value = str(address)
    probe, sleep, lock = probe or verify, sleep or time.sleep, lock or update_lock
    attempted = False
    try:
        with lock():
            before = client.records()
            delay = probe(address, settings)
            sleep(2)
            delay = (delay + probe(address, settings)) / 2
            if dns_update.fingerprint(client.records()) != dns_update.fingerprint(before):
                return '复测期间 DNS 已变化，本次未覆盖；请 /dns 查询后重发。'
            same = [row for row in before if row['type'] == kind]
            keep = same[0] if same else None
            deletes = [{'id': row['id']} for row in before if keep is None or row['id'] != keep['id']]
            batch = {'deletes': deletes}
            if keep:
                if ipaddress.ip_address(keep['content']) != address:
                    batch['patches'] = [{'id': keep['id'], 'content': value}]
            else:
                record = {'type': kind, 'name': client.name, 'content': value,
                          'ttl': before[0]['ttl'] if before else 60, 'proxied': False}
                if before:
                    for field in ('comment', 'tags'):
                        if before[0].get(field) is not None:
                            record[field] = before[0][field]
                batch['posts'] = [record]
            if not deletes and not batch.get('patches') and not batch.get('posts'):
                return '{} 已是 {} {}；两轮节点检查正常（约 {:.0f} ms）。'.format(client.name, kind, value, delay)
            attempted = True
            client.request('POST', '/batch', batch)  # One transaction; never retry a write.
            after = client.records()
            if len(after) != 1 or after[0]['type'] != kind or ipaddress.ip_address(after[0]['content']) != address:
                raise Problem('读回结果不匹配。')
            old = '、'.join(row['type'] + ' ' + row['content'] for row in before) or '无地址记录'
            return ('✅ {} 已切换\n原记录：{}\n新记录：{} {}\n'
                    '两轮 {} 节点检查通过，约 {:.0f} ms。\n客户端可能需要刷新 DNS 缓存。').format(
                        client.name, old, kind, value, settings.colo, delay)
    except (Problem, dns_update.DNSProblem):
        if attempted:
            return '⚠️ 更新已提交但结果未确认，不会自动重试；请 /dns 查询当前记录。'
        raise


def authorized(update, settings, started):
    message = update.get('message', {}) if isinstance(update, dict) else {}
    chat, user = message.get('chat', {}), message.get('from', {})
    return (chat.get('type') == 'private' and chat.get('id') == settings.chat
            and user.get('id') == settings.user and user.get('is_bot') is False
            and isinstance(message.get('date'), int) and started <= message['date'] <= time.time() + 30
            and time.time() - message['date'] <= 120
            and not any(key in message for key in ('forward_origin', 'forward_from', 'forward_from_chat', 'via_bot', 'sender_chat'))
            and isinstance(message.get('text'), str) and len(message['text']) <= 512)


def handle(text, settings, client, username=''):
    words = text.strip().split()
    if not words:
        return HELP
    command = words[0]
    if '@' in command:
        command, mention = command.split('@', 1)
        if mention.lower() != username.lower():
            return HELP
    if command in ('/start', '/help'):
        return HELP
    if command == '/dns' and len(words) == 1:
        rows = client.records()
        return client.name + '\n' + ('\n'.join(row['type'] + ' ' + row['content'] for row in rows) or '暂无 A / AAAA 记录。')
    if command in ('/use4', '/use6') and len(words) == 1:
        path = settings.runtime / ('best-ips.txt' if command == '/use4' else 'best-ips-v6.txt')
        values = path.read_text().splitlines() if path.exists() else []
        if not values:
            return 'RAM 中没有该类型的优选结果，请先优选，或直接发送一个 IP。'
        address = candidate(values[0].strip())
        if address.version != (4 if command == '/use4' else 6):
            raise Problem('RAM 优选结果类型不匹配，未修改。')
        return switch(str(address), settings, client)
    if command == '/set' and len(words) == 2:
        return switch(words[1], settings, client)
    if len(words) == 1 and not command.startswith('/'):
        return switch(words[0], settings, client)
    return HELP


def serve(settings, telegram=None):
    telegram = telegram or Telegram(settings)
    me = telegram.call('getMe')
    if not isinstance(me, dict) or not me.get('username'):
        raise Problem('机器人身份响应异常。')
    webhook = telegram.call('getWebhookInfo')
    if not isinstance(webhook, dict) or webhook.get('url'):
        raise Problem('机器人正在使用 Webhook，请先确认现有用途；未更改 Webhook。')
    # Discard historical commands on every restart, without saving an offset.
    latest = telegram.call('getUpdates', offset=-1, timeout=0, allowed_updates=['message'])
    if not isinstance(latest, list):
        raise Problem('Telegram 更新响应异常。')
    offset = max((row['update_id'] + 1 for row in latest if isinstance(row, dict) and isinstance(row.get('update_id'), int)), default=0)
    started = int(time.time())
    client = ControlDNS(*settings.dns)
    while True:
        try:
            updates = telegram.call('getUpdates', offset=offset, timeout=45, allowed_updates=['message'])
            if not isinstance(updates, list):
                raise Problem('Telegram 更新响应异常。')
            for update in updates:
                if not isinstance(update, dict) or not isinstance(update.get('update_id'), int) or update['update_id'] < offset:
                    continue
                offset = update['update_id'] + 1  # Advance before any side effect.
                if not authorized(update, settings, started):
                    continue
                try:
                    reply = handle(update['message']['text'], settings, client, me['username'])
                except (Problem, dns_update.DNSProblem) as error:
                    reply = str(error)
                except Exception:
                    reply = '请求执行异常，未自动重试；请 /dns 查询。'
                telegram.send(reply)
        except Problem:
            time.sleep(5)


def main():
    mode = sys.argv[1:] or ['--serve']
    if mode not in (['--serve'], ['--check'], ['--announce']):
        raise Problem('Usage: telegram_control.py [--serve|--check|--announce]')
    settings = Settings()
    telegram = Telegram(settings)
    if mode == ['--check']:
        records = ControlDNS(*settings.dns).records()
        for row in records:
            delay = verify(candidate(row['content']), settings)
            print('READ_ONLY_PROBE_OK {} {} {:.0f}ms'.format(row['type'], row['content'], delay))
        me = telegram.call('getMe')
        webhook = telegram.call('getWebhookInfo')
        if not isinstance(webhook, dict) or webhook.get('url'):
            raise Problem('Webhook configured; control listener was not started')
        print('CONFIG_OK {}'.format(me.get('username', '')))
    elif mode == ['--announce']:
        telegram.send('✅ TG DNS 控制已启用：' + settings.dns[2] + '\n\n' + HELP)
    else:
        serve(settings, telegram)


if __name__ == '__main__':
    def stop(signum, frame):
        raise SystemExit(0)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        main()
    except Exception:
        raise SystemExit(1) from None
