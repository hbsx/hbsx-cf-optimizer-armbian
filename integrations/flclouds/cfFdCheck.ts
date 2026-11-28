import { request as httpsRequest, type RequestOptions } from 'node:https';
import type { ClientRequest } from 'node:http';
import { checkServerIdentity } from 'node:tls';
import { customFdAddress, parseFdCheck, fdCheckPort } from './cfFdCommand.js';
import { RescueProblem } from './cfDnsRescue.js';

const HOST = 'speed.cloudflare.com';
export class FdCheckProblem extends RescueProblem {
    constructor(message: string, readonly limited = false) { super(message); }
}
export type FdProbe = (ip: string, port: number) => Promise<{ colo: string }>;
export type FdCheckTarget = { ip: string; port: number; label?: string };

// Numeric public destination only, pinned public Host/SNI, no redirects, proxy,
// credentials, private node path, download measurement or persistent files.
export function checkFdEndpoint(ip: string, port: number,
    request: typeof httpsRequest = httpsRequest): Promise<{ colo: string }> {
    const address = customFdAddress(ip);
    fdCheckPort(port);
    return new Promise((resolve, reject) => {
        let req: ClientRequest | undefined, settled = false;
        const finish = (error?: FdCheckProblem, value?: { colo: string }) => {
            if (settled) return;
            settled = true; clearTimeout(deadline); req?.destroy();
            if (error) reject(error); else resolve(value!);
        };
        const deadline = setTimeout(() => finish(new FdCheckProblem('CF/TLS 检测超时。')), 6_000);
        const options: RequestOptions = {
            hostname: address, port, method: 'GET', path: '/cdn-cgi/trace',
            servername: HOST, rejectUnauthorized: true, minVersion: 'TLSv1.2',
            checkServerIdentity: (_hostname, cert) => checkServerIdentity(HOST, cert),
            agent: false, maxHeaderSize: 8192,
            headers: { Host: HOST, 'User-Agent': 'cf-optimizer-port-check/1',
                'Accept-Encoding': 'identity', Connection: 'close' },
        };
        try {
            req = request(options, response => {
                response.once('error', () => finish(new FdCheckProblem('CF 响应中断。')));
                response.once('aborted', () => finish(new FdCheckProblem('CF 响应不完整。')));
                if (response.statusCode !== 200) {
                    finish(new FdCheckProblem(response.statusCode === 429 ? 'CF 检测限流，已停止。' : 'CF HTTP 检查未通过。', response.statusCode === 429));
                    return;
                }
                const parts: Buffer[] = []; let size = 0;
                response.on('data', (chunk: Buffer | string) => {
                    if (settled) return;
                    const block = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
                    size += block.length;
                    if (size > 8192) return finish(new FdCheckProblem('CF 响应过大。'));
                    parts.push(block);
                });
                response.once('end', () => {
                    if (settled) return;
                    const fields = Object.fromEntries(Buffer.concat(parts).toString('utf8').split(/\r?\n/)
                        .filter(line => line.includes('=')).map(line => {
                            const index = line.indexOf('='); return [line.slice(0,index), line.slice(index+1)];
                        }));
                    if (fields.h !== HOST || !/^[A-Z]{3}$/.test(fields.colo || '')) {
                        return finish(new FdCheckProblem('不是有效的 CF trace 响应。'));
                    }
                    finish(undefined, { colo: fields.colo });
                });
            });
            req.once('error', () => finish(new FdCheckProblem('TCP/TLS 连接或证书检查失败。')));
            req.setTimeout(4_000, () => finish(new FdCheckProblem('CF/TLS 检测超时。')));
            req.end();
        } catch { finish(new FdCheckProblem('CF/TLS 检测失败。')); }
    });
}

export async function verifyFdPort(ip: string, port: number, probe: FdProbe = checkFdEndpoint) {
    customFdAddress(ip);
    fdCheckPort(port);
    const first = await probe(ip,port), second = await probe(ip,port);
    if (!/^[A-Z]{3}$/.test(first.colo) || second.colo !== first.colo) {
        throw new FdCheckProblem('两轮 CF 检测机房变化，未确认端口可用。');
    }
    return first;
}

export async function checkBeforeFdWrite(ip: string, port: 443 | 8443, probe: FdProbe = checkFdEndpoint) {
    customFdAddress(ip);
    if (port !== 443 && port !== 8443) throw new FdCheckProblem('只检测 443 / 8443。');
    try { return await verifyFdPort(ip,port,probe); }
    catch (error) {
        if (error instanceof FdCheckProblem && error.limited) throw error;
        const other = port === 443 ? 8443 : 443;
        let hint = `${other} 也未检测通过，请核对 IP 和端口。`;
        try {
            const sample = await probe(ip,other);
            if (/^[A-Z]{3}$/.test(sample.colo)) {
                hint = `${other} 公开检查通过；若客户端使用 ${other}，可发送 /fd${other} ${ip}:${other} 更新 ${other === 443 ? 'tj' : 'nj'}。不会自动改端口或域名。`;
            }
        } catch (otherError) {
            if (otherError instanceof FdCheckProblem && otherError.limited) throw otherError;
        }
        throw new FdCheckProblem(`${ip}:${port} 未通过两轮 CF/TLS 端口检查，DNS 未修改。\n${hint}`);
    }
}

export async function fdCheckCommand(command: string, probe: FdProbe = checkFdEndpoint,
    current: FdCheckTarget[] = [], notices: string[] = []): Promise<string> {
    const parsed = parseFdCheck(command);
    if (!parsed) throw new FdCheckProblem('用法：/fdcheck 或 /fdcheck IPv4:TLS端口。');
    const targets: FdCheckTarget[] = parsed.ip ? parsed.ports.map(port=>({ ip:parsed.ip!,port })) : current;
    const lines = [parsed.ip ? `FD 端口检测：${parsed.ip}` : '当前 FD IP:端口检测（读取 DNS 当前记录）', ...notices];
    const deadline = Date.now()+85_000;
    if(!targets.length) lines.push('没有可检测的当前 FD IPv4 记录。');
    for (const target of targets.slice(0,8)) {
        const {ip,port}=target;
        if(Date.now()>=deadline){lines.push('检测时间已用尽，其余地址未检测。');break;}
        if(target.label)lines.push(target.label);
        try {
            const sample = await verifyFdPort(ip,port,probe);
            lines.push(`${port}：两轮公开 CF/TLS 检查通过（甲骨文观测 ${sample.colo}）；${ip}:${port} 可连通。`);
        } catch (error) {
            if (error instanceof FdCheckProblem && error.limited) throw error;
            lines.push(`${port}：未通过两轮公开 CF/TLS 检查；${ip}:${port}。`);
        }
    }
    if(targets.length>8)lines.push('本次最多检测 8 个地址，其余未检测。');
    return lines.join('\n') + '\n仅检测，未修改 DNS；未测速、未验证实际节点，不能代表家庭线路。\n线路：甲骨文（自动备用）。';
}
