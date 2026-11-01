import { BlockList, isIP } from 'node:net';

// Cloudflare's published address ranges. Rescue accepts numeric CF addresses only.
const ranges4 = ['173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22',
    '141.101.64.0/18', '108.162.192.0/18', '190.93.240.0/20', '188.114.96.0/20',
    '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
    '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22'];
const ranges6 = ['2400:cb00::/32', '2606:4700::/32', '2803:f800::/32', '2405:b500::/32',
    '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32'];
function blocks(ranges: string[], family: 'ipv4' | 'ipv6') {
    const result = new BlockList();
    for (const cidr of ranges) {
        const [address, prefix] = cidr.split('/'); result.addSubnet(address, Number(prefix), family);
    }
    return result;
}
const allowed4 = blocks(ranges4, 'ipv4'), allowed6 = blocks(ranges6, 'ipv6');
export class RescueProblem extends Error {}
function canonical(value: string): string {
    const family = isIP(value);
    if (!family || value.includes('%')) throw new RescueProblem('请输入一个不带端口的数字 IPv4 或 IPv6。');
    return family === 4 ? value : new URL(`http://[${value}]/`).hostname.slice(1, -1);
}
export function rescueCandidate(value: string): string {
    const address = canonical(value), family = isIP(address);
    if (!(family === 4 ? allowed4.check(address, 'ipv4') : allowed6.check(address, 'ipv6'))) {
        throw new RescueProblem('备用入口只接受 Cloudflare 官方范围内的 IP。');
    }
    return address;
}
export type DnsRecord = { id: string; type: 'A' | 'AAAA'; name: string; content: string;
    ttl: number; proxied: false; comment?: string; tags?: string[] };
type Batch = { deletes: { id: string }[]; patches?: { id: string; content: string }[];
    posts?: { name: string; type: string; content: string; ttl: number; proxied: false; comment?: string; tags?: string[] }[] };
export interface RescueClient { name: string; records(): Promise<DnsRecord[]>; batch(body: Batch): Promise<void> }

function validateRows(rows: unknown, info: any, name: string): DnsRecord[] {
    if (!Array.isArray(rows) || info?.total_pages > 1 || (info?.total_count ?? rows.length) !== rows.length || rows.length > 100) {
        throw new RescueProblem('DNS 记录响应不完整，未修改。');
    }
    const addresses: DnsRecord[] = [], ids = new Set<string>();
    for (const row of rows) {
        if (!row || typeof row !== 'object' || typeof row.name !== 'string'
            || row.name.toLowerCase().replace(/\.$/, '') !== name) throw new RescueProblem('DNS 响应域名不匹配，未修改。');
        if (row.type === 'CNAME') throw new RescueProblem('域名存在 CNAME，备用入口未修改。');
        if (row.type !== 'A' && row.type !== 'AAAA') continue;
        if (!/^[a-fA-F0-9]{32}$/.test(row.id || '') || ids.has(row.id) || row.proxied !== false
            || !Number.isInteger(row.ttl) || row.ttl < 1 || typeof row.content !== 'string'
            || isIP(row.content) !== (row.type === 'A' ? 4 : 6) || row.content.includes('%')) {
            throw new RescueProblem('只处理合法的灰云 A / AAAA 记录，未修改。');
        }
        ids.add(row.id); addresses.push({ ...row, content: canonical(row.content) });
    }
    if (addresses.length > 20) throw new RescueProblem('域名地址记录过多，未修改。');
    return addresses.sort((a, b) => a.id.localeCompare(b.id));
}
export class CloudflareRescue implements RescueClient {
    readonly name: string;
    private token: string;
    private zone: string;
    constructor(env: NodeJS.ProcessEnv = process.env) {
        if (env.CF_DNS_RESCUE_ENABLED !== '1') throw new RescueProblem('甲骨文备用 DNS 入口尚未配置。');
        this.name = (env.CF_DNS_RESCUE_NAME || '').toLowerCase().replace(/\.$/, '');
        this.zone = env.CF_DNS_RESCUE_ZONE_ID || '';
        this.token = env.CF_DNS_RESCUE_TOKEN || '';
        if (!/^[a-fA-F0-9]{32}$/.test(this.zone) || !this.token || /[\r\n]/.test(this.token)
            || this.name.length > 253 || !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$/.test(this.name)) {
            throw new RescueProblem('备用入口配置不完整，DNS 未修改。');
        }
    }
    private async request(suffix: string, body?: Batch): Promise<any> {
        try {
            const response = await fetch(`https://api.cloudflare.com/client/v4/zones/${this.zone}/dns_records${suffix}`, {
                method: body ? 'POST' : 'GET', redirect: 'error', signal: AbortSignal.timeout(12_000),
                headers: { Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json' },
                ...(body ? { body: JSON.stringify(body) } : {}),
            });
            const reader = response.body?.getReader();
            if (!reader) throw new Error('empty response');
            const parts: Uint8Array[] = []; let size = 0;
            try {
                while (true) {
                    const { done, value } = await reader.read(); if (done) break;
                    size += value.length; if (size > 1_048_576) throw new Error('response too large'); parts.push(value);
                }
            } finally { await reader.cancel().catch(() => {}); }
            const data = JSON.parse(Buffer.concat(parts).toString('utf8'));
            if (!response.ok || data?.success !== true) throw new Error('request rejected');
            return data;
        } catch { throw new RescueProblem('Cloudflare 连接或响应失败；未自动重试。'); }
    }
    async records(): Promise<DnsRecord[]> {
        const query = new URLSearchParams({ 'name.exact': this.name, per_page: '100' });
        const data = await this.request(`?${query}`);
        return validateRows(data.result, data.result_info, this.name);
    }
    async batch(body: Batch) { await this.request('/batch', body); }
}
function fingerprint(rows: DnsRecord[]) {
    return JSON.stringify(rows.map(row => [row.id, row.type, row.name, canonical(row.content),
        row.ttl, row.proxied, row.comment ?? null, [...(row.tags || [])].sort()]).sort((a, b) => String(a[0]).localeCompare(String(b[0]))));
}
export async function rescueSwitch(value: string, client: RescueClient): Promise<string> {
    const address = rescueCandidate(value), kind = isIP(address) === 4 ? 'A' : 'AAAA';
    let attempted = false;
    try {
        const before = await client.records();
        const same = before.filter(row => row.type === kind), keep = same[0];
        const deletes = before.filter(row => !keep || row.id !== keep.id).map(row => ({ id: row.id }));
        const batch: Batch = { deletes };
        if (keep) {
            if (canonical(keep.content) !== address) batch.patches = [{ id: keep.id, content: address }];
        } else {
            const record: NonNullable<Batch['posts']>[number] = { type: kind, name: client.name,
                content: address, ttl: before[0]?.ttl || 60, proxied: false };
            if (typeof before[0]?.comment === 'string') record.comment = before[0].comment;
            if (Array.isArray(before[0]?.tags)) record.tags = before[0].tags;
            batch.posts = [record];
        }
        if (fingerprint(await client.records()) !== fingerprint(before)) {
            return '检查期间 DNS 已变化，备用入口未覆盖；请 /rescue 查询后重发。';
        }
        if (!deletes.length && !batch.patches && !batch.posts) {
            return `${client.name} 已是 ${kind} ${address}，未执行写入。\n备用入口未验证家中线路、NRT 或节点握手。`;
        }
        attempted = true;
        await client.batch(batch); // Exactly one write; never retry an uncertain update.
        const after = await client.records();
        if (after.length !== 1 || after[0].type !== kind || canonical(after[0].content) !== address) throw new Error('readback mismatch');
        return `✅ 甲骨文备用入口已切换 ${client.name}\n新记录：${kind} ${address}\n仅保留这一条地址记录。\n未验证家中线路、NRT 或节点握手；请自行测试，DNS 缓存可能延迟生效。`;
    } catch (error) {
        if (attempted) return '⚠️ 备用更新请求已发送，但结果未确认；不会自动重试，请用 /rescue 查询当前记录。';
        if (error instanceof RescueProblem) throw error;
        throw new RescueProblem('备用入口检查失败，未发送 DNS 更新。');
    }
}
export async function rescueCommand(command: string): Promise<string> {
    const client = new CloudflareRescue();
    if (command === '/rescue') {
        const rows = await client.records();
        return `甲骨文直接查询：${client.name}\n${rows.map(row => `${row.type} ${row.content}`).join('\n') || '暂无地址记录。'}\n切换用 /rescue IP；此入口不依赖 N1，也不验证家中线路。`;
    }
    return rescueSwitch(command.slice('/rescue '.length), client);
}
