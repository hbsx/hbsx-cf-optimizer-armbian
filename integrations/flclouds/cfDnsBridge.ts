import { Router, type Request, type Response } from 'express';
import { randomBytes, timingSafeEqual } from 'node:crypto';
import { isIP } from 'node:net';
import { rescueCommand, RescueProblem } from './cfDnsRescue.js';

type Job = { id: string; command: string; sender_id: number; chat_id: number; created_at: number; expires_at: number };
type Pending = { job: Job; claimed: boolean; finish: (reply: string) => void; timer: NodeJS.Timeout };
export const DNS_MENU = [
    { command: 'dns', description: '查询优选域名当前 DNS' },
    { command: 'set', description: '指定 IPv4 或 IPv6 并切换 DNS' },
    { command: 'use4', description: '使用 N1 最近 IPv4 优选第一名' },
    { command: 'use6', description: '使用 N1 最近 IPv6 优选第一名' },
    { command: 'rescue', description: '甲骨文备用查询/指定 IP 切换（不依赖 N1）' },
];

export function normalizeDnsCommand(text: string): string | null {
    const input = text.trim();
    if (isIP(input) && !input.includes('%')) return `/set ${input}`;
    const match = input.match(/^\/(dns|set|use4|use6|rescue)(?:@\w+)?(?:\s+([\s\S]*))?$/i);
    if (!match) return null;
    const name = match[1].toLowerCase();
    const args = (match[2] || '').trim();
    if (name === 'set') return isIP(args) && !args.includes('%') ? `/set ${args}` : '/invalid';
    if (name === 'rescue') return !args ? '/rescue' : isIP(args) && !args.includes('%') ? `/rescue ${args}` : '/invalid';
    return args ? '/invalid' : `/${name}`;
}

export class DnsQueue {
    private pending?: Pending;
    private waiter?: { response: Response; timer: NodeJS.Timeout };
    private lastSeen = 0;
    private lastCompleted?: string;
    constructor(private now = () => Date.now(), private ttlMs = 120_000) {}

    online() { return this.now() - this.lastSeen < 75_000 && this.lastSeen > 0; }
    busy() { return !!this.pending; }

    submit(command: string, sender: number): Promise<string> {
        if (!this.online()) return Promise.resolve('N1 尚未连接，请稍后重发；DNS 未修改。');
        if (this.pending) return Promise.resolve('正在处理另一条 DNS 指令，请稍后重发。');
        return new Promise(resolve => {
            const job: Job = { id: randomBytes(16).toString('hex'), command, sender_id: sender, chat_id: sender,
                created_at: Math.floor(this.now() / 1000), expires_at: Math.floor((this.now() + this.ttlMs) / 1000) };
            const timer = setTimeout(() => {
                if (this.pending?.job.id !== job.id) return;
                this.pending = undefined;
                resolve('请求超时，结果未确认；不会自动重试，请用 /dns 查询。');
            }, this.ttlMs);
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
            this.pending.claimed = true; // Never redeliver, even if the HTTP reply is lost.
            response.json(this.pending.job);
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

    complete(id: string, reply: string): boolean {
        if (this.lastCompleted === id) return true; // Result delivery is idempotent.
        if (!this.pending || !this.pending.claimed || this.pending.job.id !== id) return false;
        clearTimeout(this.pending.timer);
        this.pending.finish(reply);
        this.pending = undefined;
        this.lastCompleted = id;
        return true;
    }
}

const queue = new DnsQueue();
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
cfDnsBridgeRouter.get('/status', (_request, response) => response.json({ online: queue.online(), busy: queue.busy() }));
cfDnsBridgeRouter.post('/result', (request, response) => {
    const { id, reply } = request.body || {};
    if (!/^[a-f0-9]{32}$/.test(id || '') || typeof reply !== 'string' || reply.length < 1 || reply.length > 4000) {
        return void response.status(400).json({ error: 'invalid result' });
    }
    response.status(queue.complete(id, reply) ? 200 : 409).json({ ok: true });
});

type Message = { text: string; senderId: number; chatId: string; isPrivate: boolean;
    authenticated: boolean; forwarded: boolean; date: number; allowBareIp: boolean;
    reply: (text: string) => Promise<unknown> };

export async function handleCfDnsMessage(message: Message): Promise<boolean> {
    if (!configured()) return false;
    if (!message.allowBareIp && isIP(message.text.trim())) return false;
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
        await message.reply('用法：直接发送 IP 或 /set IP；/dns 查询，/use4、/use6 使用 N1 优选。N1 故障时用 /rescue IP 直接切换，/rescue 直接查询；备用入口不验证家中线路。');
        return true;
    }
    if (controllerBusy || queue.busy()) {
        await message.reply('正在处理另一条 DNS 指令，请等结果或超时后重发。');
        return true;
    }
    controllerBusy = true; // Reserve before the first await, across normal and rescue commands.
    try {
        if (command === '/rescue' || command.startsWith('/rescue ')) {
            await message.reply(command === '/rescue' ? '正在由甲骨文直接查询 DNS…' : '正在由甲骨文直接切换 DNS；此备用入口不验证家中线路…');
            try { await message.reply(await rescueCommand(command)); }
            catch (error) { await message.reply(error instanceof RescueProblem ? error.message : '备用入口异常，未自动重试；请 /rescue 查询。'); }
        } else if (!queue.online()) {
            await message.reply('N1 尚未连接，DNS 未修改。可用 /rescue 查询，或 /rescue IP 通过甲骨文直接切换；备用入口不验证家中线路。');
        } else {
            await message.reply(command === '/dns' ? '正在查询 N1 的 DNS 配置…' : '正在由 N1 直连验证 IP，完成后回复结果…');
            await message.reply(await queue.submit(command, message.senderId));
        }
    } finally { controllerBusy = false; }
    return true;
}
