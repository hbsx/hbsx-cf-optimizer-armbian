#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import socket
import time
import urllib.parse
import urllib.request

def notify_status(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        try:
            if address.startswith('@'): address = '\0' + address[1:]
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.sendto(('STATUS=' + message).encode(), address)
        except OSError:
            pass

token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
chat = os.environ.get('TELEGRAM_CHAT_ID', '')
if not token or not chat:
    print('Telegram not configured: fill TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in env.sh')
    sys.exit(0)
report_file = Path(os.environ.get('CFST_OUTPUT_DIR', '/run/cf-optimizer')).joinpath('report.txt')
report = report_file.read_text(encoding='utf-8')
report_file.unlink()  # Keep the pending text only in memory, including retries.
body = urllib.parse.urlencode(dict(chat_id=chat, text=report, disable_web_page_preview='true')).encode()
proxy = os.environ.get('TELEGRAM_PROXY_URL', 'http://127.0.0.1:7890')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
for attempt in range(3):
    try:
        request = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage', data=body)
        with opener.open(request, timeout=15) as response:
            data = json.load(response)
        if data.get('ok'):
            print('Telegram report sent successfully')
            notify_status('Completed; Telegram sent through Mihomo; local report deleted')
            sys.exit(0)
    except Exception:
        pass
    if attempt < 2:
        time.sleep(2 ** attempt)
print('Telegram send failed; no local report was retained.', file=sys.stderr)
notify_status('IP selection ready; Telegram delivery failed; local report deleted')
sys.exit(1)
