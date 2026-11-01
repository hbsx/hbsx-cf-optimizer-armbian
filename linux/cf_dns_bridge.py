#!/usr/bin/env python3
"""Outbound-only worker for an existing bot. No Telegram update consumer."""
import collections
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
    if command in ('/dns', '/use4', '/use6'):
        return True
    if isinstance(command, str) and command.startswith('/set ') and len(command) <= 64:
        try:
            if '%' in command[5:]:
                return False
            ipaddress.ip_address(command[5:])
            return True
        except ValueError:
            pass
    return False


def run(bridge):
    client = control.ControlDNS(*bridge.settings.dns)
    seen = collections.deque(maxlen=128)
    while True:
        try:
            job = bridge.request('/next')
            if job is None:
                continue
            if not accepted_job(job, bridge.settings, seen):
                continue
            seen.append(job['id'])  # Mark before any possible DNS side effect.
            try:
                reply = control.handle(job['command'], bridge.settings, client)
            except (control.Problem, dns_update.DNSProblem) as error:
                reply = str(error)
            except Exception:
                reply = '请求执行异常，未自动重试；请 /dns 查询。'
            for attempt in range(3):
                try:
                    bridge.request('/result', {'id': job['id'], 'reply': reply[:4000]})
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
