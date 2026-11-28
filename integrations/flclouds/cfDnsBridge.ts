import { Router, type Request, type Response } from 'express';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';
import { RescueProblem } from './cfDnsRescue.js';
import { CandidateCache, oracleDnsCommand, runPreferred, type N1Result } from './cfDnsUnified.js';
import { parseFdCommand, parseFdCheck } from './cfFdCommand.js';

type Job = { id: string; command: string; sender_id: number; chat_id: number; created_at: number; expires_at: number };
type Pending = { job: Job; claimed: boolean; finish: (reply: N1Result) => void; timer: NodeJS.Timeout };
export const DNS_MENU = [
    { command: 'dns', description: '查看 bj、tj、nj 当前 IP' },
    { command: 'usecf', description: '使用 N1 的 CF IPv4 优选第一名' },
    { command: 'fd443', description: 'FD 443 更新 tj；可加自定义 IP' },
    { command: 'fd8443', description: 'FD 8443 更新 nj；可加自定义 IP' },
    { command: 'fdcheck', description: '检测当前 FD；可指定 IP:端口，不改 DNS' },
    { command: 'use6', description: '使用 N1 的 CF IPv6 优选第一名' },
    { command: 'fdscan', description: '手动启动 N1 的 FD 优选并推送' },
];

export function normalizeDnsCommand(text: string): string | null {
    const input = text.trim().replace(/^(fd443|fd8443|fdscan|fdcheck)(?=\s|$)/i, '/$1');
    if (isIP(input) && !input.includes('%')) return `/set ${input}`;
    if (/^(fd443|fd8443|fdscan)$/i.test(input)) return `/${input.toLowerCase()}`;
    const match = input.match(/^\/(dns|set|usecf|usefd|use4|use6|rescue|tj|usetj|settj|rescuetj|fd443|fd8443|fdscan|fdcheck)(?:@\w+)?(?:\s+([\s\S]*))?$/i);
    if (!match) return null;
    let name = match[1].toLowerCase();
    const args = (match[2] || '').trim();
    if (name === 'rescuetj') name = args ? 'settj' : 'tj';
    if (name === 'rescue') name = args ? 'set' : 'dns';
    if (name === 'use4') name = 'usecf';
    if (name === 'fdcheck') {
        try {
            const parsed = parseFdCheck('/fdcheck' + (args ? ' ' + args : ''));
            return parsed ? `/fdcheck${parsed.ip ? ' '+parsed.ip+(parsed.ports.length === 1 ? ':'+parsed.ports[0] : '') : ''}` : '/invalid';
        } catch { return '/invalid'; }
    }
    if (name === 'fd443' || name === 'fd8443') {
        try {
            const parsed = parseFdCommand(`/${name}${args ? ' ' + args : ''}`);
            return parsed ? `/${name}${parsed.ip ? ' ' + parsed.ip : ''}` : '/invalid';
        } catch { return '/invalid'; }
    }
    if (name === 'settj') return isIP(args) === 4 ? `/settj ${args}` : '/invalid';
    if (name === 'usetj') {
        if (!args) return '/usefd';
        const parts = args.toUpperCase().split(/\s+/);
        if (parts.length === 1 && /^(?:[1-9]|10)$/.test(parts[0])) return `/usetj ${parts[0]}`;
        if (['HKG', 'NRT', 'SIN'].includes(parts[0]) && (parts.length === 1
            || parts.length === 2 && /^(?:[1-9]|10)$/.test(parts[1]))) return `/usetj ${parts.join(' ')}`;
        return '/invalid';
    }
    if (name === 'set') return isIP(args) && !args.includes('%') ? `/set ${args}` : '/invalid';
    return args ? '/invalid' : `/${name}`;
}

export class DnsQueue {
    private pending?: Pending;
    private waiter?: { response: Response; timer: NodeJS.Timeout };
    private lastSeen = 0;
    private lastCompleted?: string;
    constructor(private now = () => Date.now(), private ttlMs = 120_000, private claimMs = 8_000) {}

    online() { return this.now() - this.lastSeen < 75_000 && this.lastSeen > 0; }
    busy() { return !!this.pending; }

    async submit(command: string, sender: number): Promise<string> {
        return (await this.submitResult(command, sender)).reply;
    }

    submitResult(command: string, sender: number): Promise<N1Result> {
        if (!this.online()) return Promise.resolve({ state: 'unavailable', reply: 'N1 尚未连接。' });
        if (this.pending) return Promise.resolve({ state: 'busy', reply: '正在处理另一条 DNS 指令，请稍后重发。' });
        return new Promise(resolve => {
            const job: Job = { id: randomBytes(16).toString('hex'), command, sender_id: sender, chat_id: sender,
                created_at: Math.floor(this.now() / 1000), expires_at: Math.floor((this.now() + this.ttlMs) / 1000) };
            const timer = setTimeout(() => {
                if (this.pending?.job.id !== job.id) return;
                this.pending = undefined;
                resolve({ state: 'unavailable', reply: 'N1 未领取任务，已取消。' });
            }, Math.min(this.claimMs, this.ttlMs));
            timer.unref();
            this.pending = { job, claimed: false, finish: resolve, timer };
            if (this.waiter) {
                const waiter = this.waiter;
                this.waiter = undefined;
                clearTimeout(waiter.timer);
                this.claim(waiter.response);
            }
        });
    }

    private claim(response: Response) {
        if (this.pending && !this.pending.claimed) {
            const pending = this.pending;
            pending.claimed = true; // Never redeliver, even if the HTTP reply is lost.
            clearTimeout(pending.timer);
            pending.timer = setTimeout(() => {
                if (this.pending !== pending) return;
                this.pending = undefined;
                pending.finish({ state: 'uncertain', reply: 'N1 处理超时，结果尚未确认。' });
            }, Math.max(1, pending.job.expires_at * 1000 - this.now()));
            pending.timer.unref();
            response.json(pending.job);
        } else response.status(204).end();
    }

    poll(request: Request, response: Response) {
        this.lastSeen = this.now();
        response.setHeader('Cache-Control', 'no-store');
        if (this.pending && !this.pending.claimed) return this.claim(response);
        if (this.waiter) {
            clearTimeout(this.waiter.timer);
            this.waiter.response.status(204).end();
            this.waiter = undefined;
        }
        const timer = setTimeout(() => {
            if (this.waiter?.response === response) this.waiter = undefined;
            if (!response.writableEnded) response.status(204).end();
        }, 25_000);
        timer.unref();
        this.waiter = { response, timer };
        response.on('close', () => {
            clearTimeout(timer);
            if (this.waiter?.response === response) this.waiter = undefined;
        });
    }

    complete(id: string, reply: string, state: N1Result['state'] = 'ok'): boolean {
        if (this.lastCompleted === id) return true; // Result delivery is idempotent.
        if (!this.pending || !this.pending.claimed || this.pending.job.id !== id) return false;
        clearTimeout(this.pending.timer);
        this.pending.finish({ state, reply });
        this.pending = undefined;
        this.lastCompleted = id;
        return true;
    }
}

const queue = new DnsQueue();
const candidates = new CandidateCache();
let controllerBusy = false;
function configured() {
    return process.env.CF_DNS_BRIDGE_ENABLED === '1'
        && /^[a-f0-9]{64}$/.test(process.env.CF_DNS_BRIDGE_KEY || '')
        && /^[1-9][0-9]*$/.test(process.env.CF_DNS_OWNER_ID || '');
}

export async function syncOwnerDnsMenu(commands: { command: string; description: string }[]) {
    if (!configured()) return;
    const token = process.env.TELEGRAM_BOT_TOKEN;
    if (!token) throw new Error('Missing bot credentials for owner menu');
    for (const language_code of ['', 'zh']) {
        try {
            const response = await fetch(`https://api.telegram.org/bot${token}/setMyCommands`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: AbortSignal.timeout(15_000),
                body: JSON.stringify({ scope: { type: 'chat', chat_id: Number(process.env.CF_DNS_OWNER_ID) },
                    language_code, commands: commands.map(({ command, description }) => ({ command, description })) }),
            });
            const data = await response.json() as { ok?: boolean };
            if (!response.ok || data.ok !== true) throw new Error('Menu registration rejected');
        } catch { throw new Error('Unable to register complete owner command menu'); }
    }
}

export const cfDnsBridgeRouter = Router();
cfDnsBridgeRouter.use((request, response, next) => {
    if (!configured()) return void response.status(503).json({ error: 'disabled' });
    const expected = Buffer.from(`Bearer ${process.env.CF_DNS_BRIDGE_KEY}`);
    const provided = Buffer.from(request.get('authorization') || '');
    if (expected.length !== provided.length || !timingSafeEqual(expected, provided)) {
        return void response.status(401).json({ error: 'unauthorized' });
    }
    response.setHeader('Cache-Control', 'no-store');
    next();
});
cfDnsBridgeRouter.get('/next', (request, response) => queue.poll(request, response));
cfDnsBridgeRouter.get('/status', (_request, response) => response.json({ online: queue.online(), busy: queue.busy(), candidates: candidates.status() }));
cfDnsBridgeRouter.post('/snapshot', (request, response) => {
    try { candidates.replace(request.body); response.json({ ok: true }); }
    catch { response.status(400).json({ error: 'invalid snapshot' }); }
});
cfDnsBridgeRouter.post('/result', (request, response) => {
    const { id, reply, state = 'ok' } = request.body || {};
    if (!/^[a-f0-9]{32}$/.test(id || '') || typeof reply !== 'string' || reply.length < 1 || reply.length > 4000
        || !['ok', 'unavailable', 'failed', 'uncertain'].includes(state)) {
        return void response.status(400).json({ error: 'invalid result' });
    }
    response.status(queue.complete(id, reply, state) ? 200 : 409).json({ ok: true });
});

type Message = { text: string; senderId: number; chatId: string; isPrivate: boolean;
    authenticated: boolean; forwarded: boolean; date: number; allowBareIp: boolean;
    reply: (text: string) => Promise<unknown> };

export async function handleCfDnsMessage(message: Message): Promise<boolean> {
    if (!configured()) return false;
    if (!message.allowBareIp && (isIP(message.text.trim()) || /^(fd443|fd8443|fdscan|fdcheck)(?:\s|$)/i.test(message.text.trim()))) return false;
    const command = normalizeDnsCommand(message.text);
    if (command === null) return false; // Preserve all existing command dispatch.
    if (String(message.senderId) !== process.env.CF_DNS_OWNER_ID || !message.isPrivate
        || message.chatId !== String(message.senderId)) return true;
    if (!message.authenticated) {
        await message.reply('请先使用 /start 完成原机器人的身份验证。');
        return true;
    }
    const age = Date.now() / 1000 - message.date;
    if (message.forwarded || age < -30 || age > 120) {
        await message.reply('请直接发送新指令；转发或过旧消息不会修改 DNS。');
        return true;
    }
    if (command === '/invalid') {
        await message.reply('/fd443 [IP或IP:443] 更新 tj；/fd8443 [IP或IP:8443] 更新 nj；端口与命令不一致时不会更新。/fdcheck 检测当前 tj/nj；/fdcheck IP:端口 检测指定地址，均不改 DNS。/dns 查看解析；/fdscan 手动优选；/usecf、/use6 切换 CF。');
        return true;
    }
    if (controllerBusy || queue.busy()) {
        await message.reply('正在处理另一条 DNS 指令，请等结果或超时后重发。');
        return true;
    }
    controllerBusy = true; // Reserve before the first await, across normal and rescue commands.
    try {
        await message.reply(command === '/fdcheck' || command.startsWith('/fdcheck ') ? '正在检测当前或指定的 FD IP:端口，不修改 DNS…' : command === '/fdscan' ? '正在请求 N1 启动 FD 优选…' : command === '/dns' || command === '/tj' ? '正在查询当前 DNS…' : '正在检测并切换优选 IP，默认使用 N1…');
        try {
            const reply = await runPreferred(command,
                () => queue.submitResult(command, message.senderId),
                () => oracleDnsCommand(command, candidates),
                () => oracleDnsCommand('/dns', candidates));
            await message.reply(reply);
        } catch (error) {
            await message.reply(error instanceof RescueProblem ? error.message : 'DNS 处理失败，请 /dns 查询当前记录。');
        }
    } finally { controllerBusy = false; }
    return true;
}
