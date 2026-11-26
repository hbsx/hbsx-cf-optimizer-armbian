import { isIP } from 'node:net';
import { CloudflareRescue, rescueCandidate, rescueSwitch, RescueProblem } from './cfDnsRescue.js';
import { proxyRescueConfiguration } from './cfProxyRescue.js';
import { customFdAddress, parseFdCommand } from './cfFdCommand.js';

export type N1Result = { state: 'ok' | 'unavailable' | 'failed' | 'uncertain' | 'busy'; reply: string };
type Slot = 'cf4' | 'cf6' | 'fd' | 'fd8443';
type Candidate = { ip: string; generated_at: string; port?: number };
const slots: Slot[] = ['cf4', 'cf6', 'fd', 'fd8443'];

// Four N1-measured winners are retained in process memory. No remote tests.
export class CandidateCache {
    private values: Partial<Record<Slot, Candidate>> = {};
    constructor(private env: NodeJS.ProcessEnv = process.env, private now = () => Date.now()) {}
    private fresh(value: Candidate) {
        const age = this.now() - Date.parse(value.generated_at);
        return Number.isFinite(age) && age >= -300_000 && age <= 96 * 3600_000;
    }
    replace(body: unknown) {
        if (!body || typeof body !== 'object' || Array.isArray(body) || JSON.stringify(body).length > 4096
            || Object.keys(body).some(key => !slots.includes(key as Slot))) throw new RescueProblem('Invalid snapshot');
        const next: Partial<Record<Slot, Candidate>> = {};
        for (const key of slots) {
            const raw = (body as Record<string, unknown>)[key];
            if (raw == null) continue;
            if (typeof raw !== 'object' || Array.isArray(raw)) throw new RescueProblem('Invalid candidate');
            const row = raw as Candidate;
            if (typeof row.ip !== 'string' || typeof row.generated_at !== 'string'
                || row.generated_at.length > 50 || !this.fresh(row)) throw new RescueProblem('Stale or invalid candidate');
            if (key === 'fd' || key === 'fd8443') {
                let config: ReturnType<typeof proxyRescueConfiguration>;
                try { config = proxyRescueConfiguration(this.env, key === 'fd' ? 443 : 8443); }
                catch { continue; }
                if (row.port !== Number(config.port) || isIP(row.ip) !== 4 || !config.ips.includes(row.ip)) {
                    // A snapshot can never enlarge the independently configured rescue allowlist.
                    continue;
                }
            } else if (isIP(rescueCandidate(row.ip)) !== (key === 'cf4' ? 4 : 6)) {
                throw new RescueProblem('Candidate family mismatch');
            }
            next[key] = { ip: row.ip, generated_at: row.generated_at, ...(key === 'fd' || key === 'fd8443' ? { port: row.port } : {}) };
        }
        this.values = next;
    }
    get(key: Slot) {
        const row = this.values[key];
        if (!row || !this.fresh(row)) throw new RescueProblem('没有 96 小时内的 N1 优选缓存，DNS 未修改。请待 N1 在线并完成相应优选后再试。');
        return { ...row };
    }
    status() {
        return Object.fromEntries(slots.map(key => [key, this.values[key] && this.fresh(this.values[key]!) ? { ...this.values[key] } : null]));
    }
}

function display(name: string, rows: { type: string; content: string }[]) {
    return `${name}\n${rows.map(row => `${row.type} ${row.content}`).join('\n') || '暂无 A / AAAA 记录。'}`;
}

export async function oracleDnsCommand(command: string, cache: CandidateCache): Promise<string> {
    if (command === '/fdscan') return '手动 FD 优选只能由 N1 执行。N1 当前未连接，未启动扫描。';
    if (command === '/dns' || command === '/tj') {
        const queries = command === '/tj' ? ['fd', 'fd8443'] : ['cf', 'fd', 'fd8443'];
        const replies = await Promise.all(queries.map(async kind => {
            try {
                const client = kind === 'cf' ? new CloudflareRescue() : proxyRescueConfiguration(process.env, kind === 'fd8443' ? 8443 : 443).client;
                return `${kind === 'cf' ? 'CF' : kind === 'fd8443' ? 'FD 8443' : 'FD 443'} · ${display(client.name, await client.records())}`;
            } catch (error) {
                return `${kind.toUpperCase()}：${error instanceof RescueProblem ? error.message : '查询失败'}`;
            }
        }));
        return replies.join('\n\n') + '\n线路：甲骨文（自动备用）。';
    }
    const fd = parseFdCommand(command);
    let address: string, timestamp = '';
    if (fd?.ip) {
        address = fd.ip;
    } else if (command === '/usecf' || command === '/use6' || command === '/usefd' || command === '/fd443' || command === '/fd8443') {
        const row = cache.get(command === '/usecf' ? 'cf4' : command === '/use6' ? 'cf6' : command === '/fd8443' ? 'fd8443' : 'fd');
        address = row.ip; timestamp = `\nN1 优选时间：${row.generated_at}`;
    } else if (command.startsWith('/set ') || command.startsWith('/settj ')) {
        address = command.split(' ')[1];
    } else throw new RescueProblem('备用切换请使用 /usecf、/usefd 或 /use6。');
    if (command === '/usefd' || fd || command.startsWith('/settj ')) {
        const { client, ips, port } = proxyRescueConfiguration(process.env, fd?.port || 443);
        const validate = (value: string) => {
            if (fd?.ip && value === fd.ip) return customFdAddress(value);
            if (isIP(value) !== 4 || !ips.includes(value)) throw new RescueProblem('反代 IP 不在备用允许列表中，DNS 未修改。');
            return value;
        };
        return await rescueSwitch(address, client, validate, '/dns') + timestamp + `\nFD 端口：${port}（保持不变）。`
            + (fd?.ip ? '\n手动指定地址；甲骨文仅更改 DNS，未测速、未验证家庭线路或实际节点。' : '');
    }
    return await rescueSwitch(address, new CloudflareRescue(), rescueCandidate, '/dns') + timestamp;
}

export async function runPreferred(command: string, normal: () => Promise<N1Result>,
    backup: () => Promise<string>, readback: () => Promise<string>): Promise<string> {
    const result = await normal();
    if (command === '/fdscan') {
        if (result.state === 'unavailable') return backup();
        if (result.state === 'uncertain') return 'N1 的优选启动结果尚未确认，未重复提交。请等待原机器人推送结果后再检查。';
        return result.reply;
    }
    if (result.state === 'unavailable' || result.state === 'uncertain' && (command === '/dns' || command === '/tj')) {
        return backup();
    }
    if (result.state === 'uncertain') {
        return `⚠️ N1 的更新结果暂未确认，先查询当前 DNS；本次不会重复提交更新。\n\n${await readback()}`;
    }
    return result.reply;
}
