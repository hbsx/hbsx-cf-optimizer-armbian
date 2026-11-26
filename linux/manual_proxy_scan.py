"""Submit a fixed RAM-only request to the root-owned FD scan dispatcher."""
import os
from pathlib import Path
import subprocess
import time

import telegram_control as control

REQUEST = Path('/run/cf-optimizer-bridge/fdscan.request')


def request_scan(expires_at, request=REQUEST, runner=subprocess.run, now=time.time):
    if now() >= expires_at:
        raise control.Problem('指令已过期，未启动优选。')
    for unit in ('cf-optimizer.service', 'cf-optimizer-proxy.service'):
        result = runner(['/usr/bin/systemctl', 'is-active', unit], capture_output=True, text=True, timeout=4)
        if result.stdout.strip() in ('active', 'activating', 'reloading'):
            return 'N1 已有优选任务在运行，请等待本轮结果；未重复启动。'
    dispatcher = runner(['/usr/bin/systemctl', 'is-active', 'cf-optimizer-fd-request.path'],
                        capture_output=True, text=True, timeout=4)
    if dispatcher.returncode or dispatcher.stdout.strip() != 'active':
        raise control.Problem('N1 手动优选入口尚未启用，未启动扫描。')
    if now() >= expires_at:
        raise control.Problem('指令已过期，未启动优选。')
    try:
        fd = os.open(str(request), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    except FileExistsError:
        return 'N1 已有 FD 优选启动请求在排队，未重复提交。'
    except OSError:
        raise control.Problem('N1 优选启动请求未提交，请稍后重试。') from None
    return '已向 N1 提交 FD 优选启动请求，完成后通过原机器人推送结果。测速直连；TG 推送走代理。'
