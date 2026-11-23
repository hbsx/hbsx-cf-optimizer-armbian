import { isIP } from 'node:net';
import { CloudflareRescue, rescueCandidate, rescueSwitch, RescueProblem } from './cfDnsRescue.js';
import { proxyRescueConfiguration } from './cfProxyRescue.js';

export type N1Result = { state: 'ok' | 'unavailable' | 'failed' | 'uncertain' | 'busy'; reply: string };
type Slot = 'cf4' | 'cf6' | 'fd';
type Candidate = { ip: string; generated_at: string; port?: number };
const slots: Slot[] = ['cf4', 'cf6', 'fd'];

// Only three N1-measured winners are retained in process memory. No remote tests.
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
            if (key === 'fd') {
                const config = proxyRescueConfiguration(this.env);
                if (row.port !== Number(config.port) || isIP(row.ip) !== 4 || !config.ips.includes(row.ip)) {
                    // A snapshot can never enlarge the independently configured rescue allowlist.
                    continue;
                }
            } else if (isIP(rescueCandidate(row.ip)) !== (key === 'cf4' ? 4 : 6)) {
                throw new RescueProblem('Candidate family mismatch');
            }
            next[key] = { ip: row.ip, generated_at: row.generated_at, ...(key === 'fd' ? { port: row.port } : {}) };
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
    if (command === '/dns' || command === '/tj') {
        const queries = command === '/tj' ? ['fd'] : ['cf', 'fd'];
        const replies = await Promise.all(queries.map(async kind => {
            try {
                const client = kind === 'cf' ? new CloudflareRescue() : proxyRescueConfiguration().client;
                return `${kind === 'cf' ? 'CF' : 'FD'} · ${display(client.name, await client.records())}`;
            } catch (error) {
                return `${kind.toUpperCase()}：${error instanceof RescueProblem ? error.message : '查询失败'}`;
            }
        }));
        return replies.join('\n\n') + '\n线路：甲骨文（自动备用）。';
    }
    let address: string, timestamp = '';
    if (command === '/usecf' || command === '/use6' || command === '/usefd') {
        const row = cache.get(command === '/usecf' ? 'cf4' : command === '/use6' ? 'cf6' : 'fd');
        address = row.ip; timestamp = `\nN1 优选时间：${row.generated_at}`;
    } else if (command.startsWith('/set ') || command.startsWith('/settj ')) {
        address = command.split(' ')[1];
    } else throw new RescueProblem('备用切换请使用 /usecf、/usefd 或 /use6。');
    if (command === '/usefd' || command.startsWith('/settj ')) {
        const { client, ips, port } = proxyRescueConfiguration();
        const validate = (value: string) => {
            if (isIP(value) !== 4 || !ips.includes(value)) throw new RescueProblem('反代 IP 不在备用允许列表中，DNS 未修改。');
            return value;
        };
        return await rescueSwitch(address, client, validate, '/dns') + timestamp + `\n反代端口：${port}（保持不变）。`;
    }
    return await rescueSwitch(address, new CloudflareRescue(), rescueCandidate, '/dns') + timestamp;
}

export async function runPreferred(command: string, normal: () => Promise<N1Result>,
    backup: () => Promise<string>, readback: () => Promise<string>): Promise<string> {
    const result = await normal();
    if (result.state === 'unavailable' || result.state === 'uncertain' && (command === '/dns' || command === '/tj')) {
        return backup();
    }
    if (result.state === 'uncertain') {
        return `⚠️ N1 的更新结果暂未确认，先查询当前 DNS；本次不会重复提交更新。\n\n${await readback()}`;
    }
    return result.reply;
}
