#!/usr/bin/env python3
"""Outbound-only worker for an existing bot. No Telegram update consumer."""
import collections
import datetime as dt
import ipaddress
import json
import os
import re
import signal
import sys
import time
import urllib.parse
import urllib.request

import dns_update
import telegram_control as control
import proxy_control


class Bridge:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        if env.get('CF_DNS_BRIDGE_ENABLED', '0') != '1':
            raise control.Problem('DNS bridge is disabled')
        self.key = env.get('CF_DNS_BRIDGE_KEY', '')
        self.url = env.get('CF_DNS_BRIDGE_URL', '').rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if (not re.fullmatch('[a-f0-9]{64}', self.key) or parsed.scheme != 'https'
                or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise control.Problem('Invalid bridge configuration')
        self.settings = control.Settings(dict(env, CF_TG_CONTROL_ENABLED='1'))
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': self.settings.proxy, 'https': self.settings.proxy}),
            dns_update.NoRedirect())

    def request(self, suffix, payload=None):
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.url + suffix, data=body,
            headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=40) as response:
                if response.status == 204:
                    return None
                data = response.read(16385)
                if len(data) > 16384:
                    raise ValueError('Response too large')
                return json.loads(data)
        except Exception:
            raise control.Problem('Bridge connection or response failed') from None


def accepted_job(job, settings, seen, now=None):
    now = time.time() if now is None else now
    if (not isinstance(job, dict) or not isinstance(job.get('id'), str)
            or not re.fullmatch('[a-f0-9]{32}', job['id']) or job['id'] in seen
            or job.get('sender_id') != settings.user or job.get('chat_id') != settings.chat
            or not isinstance(job.get('created_at'), int) or not isinstance(job.get('expires_at'), int)
            or not now - 120 <= job['created_at'] <= now + 30
            or not now < job['expires_at'] <= job['created_at'] + 120):
        return False
    command = job.get('command')
    if command in ('/dns', '/usecf', '/usefd', '/use4', '/use6'):
        return True
    if isinstance(command, str) and len(command) <= 64:
        try:
            if proxy_control.parse(command) is not None:
                return True
        except control.Problem:
            return False
    if isinstance(command, str) and command.startswith('/set ') and len(command) <= 64:
        try:
            if '%' in command[5:]:
                return False
            ipaddress.ip_address(command[5:])
            return True
        except ValueError:
            pass
    return False


def snapshot_payload(settings, now=None):
    """Publish only already-measured winners; never probe or write result files."""
    now = time.time() if now is None else now
    result = dict(cf4=None, cf6=None, fd=None)
    for key, name, family in [('cf4', 'best-ips.txt', 4), ('cf6', 'best-ips-v6.txt', 6)]:
        try:
            path = settings.runtime / name
            stamp = path.stat().st_mtime
            if not -300 <= now - stamp <= 96 * 3600:
                continue
            with path.open(encoding='utf-8') as stream:
                value = stream.readline(128).strip()
            address = control.candidate(value)
            if address.version != family:
                continue
            result[key] = dict(ip=str(address), generated_at=dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat())
        except (OSError, ValueError, control.Problem):
            pass
    try:
        _, port, config = proxy_control.configuration(settings)
        rows, stamp = proxy_control.snapshot(config, port, dt.datetime.fromtimestamp(now, dt.timezone.utc))
        if rows:
            result['fd'] = dict(ip=rows[0]['ip'], port=port, generated_at=stamp)
    except (OSError, ValueError, control.Problem, dns_update.DNSProblem):
        pass
    return result


def execute_job(job, settings):
    """Report whether fallback is safe; never replay an uncertain DNS write."""
    attempted = False

    class JobDNS(control.ControlDNS):
        def request(self, method, suffix, body=None):
            nonlocal attempted
            if method != 'GET':
                if time.time() >= job['expires_at']:
                    raise control.Problem('指令已过期，未发送 DNS 更新。')
                attempted = True
            return super().request(method, suffix, body)

    try:
        command = job['command']
        client = JobDNS(*settings.dns)
        if command in ('/dns', '/tj'):
            lines = []
            if command == '/dns':
                rows = client.records()
                lines.append('CF · ' + client.name + '\n' + ('\n'.join(r['type'] + ' ' + r['content'] for r in rows) or '暂无地址记录。'))
            dns, port, _ = proxy_control.configuration(settings)
            fd = JobDNS(*dns)
            rows = fd.records()
            lines.append('FD（反代）· ' + fd.name + '\n' + ('\n'.join(r['type'] + ' ' + r['content'] for r in rows) or '暂无地址记录。') + '\n端口：' + str(port))
            reply = '\n\n'.join(lines)
        elif command == '/usefd' or command.startswith(('/usetj', '/settj')):
            dns, _, _ = proxy_control.configuration(settings)
            reply = proxy_control.handle('/usetj' if command == '/usefd' else command, settings, client=JobDNS(*dns))
        else:
            reply = control.handle('/use4' if command == '/usecf' else command, settings, client)
        return reply + '\n线路：N1。', 'uncertain' if attempted and reply.startswith('⚠️') else 'ok'
    except dns_update.DNSProblem as error:
        return str(error), 'uncertain' if attempted else 'unavailable'
    except control.Problem as error:
        return str(error), 'uncertain' if attempted else 'failed'
    except Exception:
        return '请求执行异常，请 /dns 查询当前记录。', 'uncertain' if attempted else 'failed'


def run(bridge):
    seen = collections.deque(maxlen=128)
    last_snapshot = 0
    while True:
        try:
            if time.monotonic() - last_snapshot >= 45:
                try:
                    bridge.request('/snapshot', snapshot_payload(bridge.settings))
                except control.Problem:
                    pass
                last_snapshot = time.monotonic()
            job = bridge.request('/next')
            if job is None:
                continue
            if not accepted_job(job, bridge.settings, seen):
                continue
            seen.append(job['id'])  # Mark before any possible DNS side effect.
            reply, state = execute_job(job, bridge.settings)
            for attempt in range(3):
                try:
                    bridge.request('/result', {'id': job['id'], 'reply': reply[:4000], 'state': state})
                    break
                except control.Problem:
                    if attempt < 2:
                        time.sleep(2)
        except control.Problem:
            time.sleep(10)


if __name__ == '__main__':
    def stop(signum, frame):
        raise SystemExit(0)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, stop)
    try:
        bridge = Bridge()
        if sys.argv[1:] == ['--check']:
            print('BRIDGE_STATUS', json.dumps(bridge.request('/status')))
        elif not sys.argv[1:]:
            run(bridge)
        else:
            raise control.Problem('Invalid mode')
    except Exception:
        raise SystemExit(1) from None
