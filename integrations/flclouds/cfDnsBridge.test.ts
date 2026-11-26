import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import express from 'express';
import { DnsQueue, normalizeDnsCommand, handleCfDnsMessage, cfDnsBridgeRouter, syncOwnerDnsMenu, DNS_MENU } from './cfDnsBridge.js';

class Reply extends EventEmitter {
    statusCode = 200; writableEnded = false; body: any;
    status(code: number) { this.statusCode = code; return this; }
    setHeader() {}
    json(body: any) { this.body = body; this.end(); return this; }
    end() { this.writableEnded = true; this.emit('close'); return this; }
}

async function main() {
    assert.deepEqual(DNS_MENU.map(row => row.command), ['dns', 'usecf', 'fd443', 'fd8443', 'use6', 'fdscan']);
    assert.equal(normalizeDnsCommand('2606:4700:ff00::1'), '/set 2606:4700:ff00::1');
    assert.equal(normalizeDnsCommand('/set 172.64.229.194'), '/set 172.64.229.194');
    assert.equal(normalizeDnsCommand('/dns@example_bot'), '/dns');
    assert.equal(normalizeDnsCommand('/rescue'), '/dns');
    assert.equal(normalizeDnsCommand('/usecf'), '/usecf');
    assert.equal(normalizeDnsCommand('/usefd'), '/usefd');
    assert.equal(normalizeDnsCommand('/use4'), '/usecf');
    assert.equal(normalizeDnsCommand('/rescuetj'), '/tj');
    assert.equal(normalizeDnsCommand('/rescuetj 39.105.108.60'), '/settj 39.105.108.60');
    assert.equal(normalizeDnsCommand('/rescuetj ::1'), '/invalid');
    assert.equal(normalizeDnsCommand('/tj@example_bot'), '/tj');
    assert.equal(normalizeDnsCommand('/usetj'), '/usefd');
    assert.equal(normalizeDnsCommand('/usetj nrt 2'), '/usetj NRT 2');
    assert.equal(normalizeDnsCommand('/usetj 2'), '/usetj 2');
    assert.equal(normalizeDnsCommand('/settj 39.105.24.90'), '/settj 39.105.24.90');
    for (const text of ['/tj now', '/usetj NRT 0', '/usetj NRT 11', '/usetj NRT 1 extra',
        '/settj 39.105.24.90:443', '/settj ::1', '/settj domain.example']) {
        assert.equal(normalizeDnsCommand(text), '/invalid');
    }
    assert.equal(normalizeDnsCommand('/rescue 2606:4700::1'), '/set 2606:4700::1');
    for (const input of ['/start', '/help', '/tasks', '/ytdlp https://example.com']) {
        assert.equal(normalizeDnsCommand(input), null);
    }
    for (const input of ['/set localhost', '/set 2606:4700::1%eth0', '/use4 now', '/rescue hostname', '/rescue ::1%eth0']) {
        assert.equal(normalizeDnsCommand(input), '/invalid');
    }
    const q = new DnsQueue();
    assert.match(await q.submit('/dns', 12345), /尚未连接/);
    const response = new Reply();
    q.poll({} as any, response as any);
    const first = q.submit('/dns', 12345);
    const id = response.body.id;
    assert.equal(response.body.command, '/dns');
    assert.match(await q.submit('/dns', 12345), /另一条/);
    const duplicatePoll = new Reply();
    q.poll({} as any, duplicatePoll as any);
    assert.equal(duplicatePoll.body, undefined); // A lost response must never reexecute DNS.
    duplicatePoll.end();
    assert.equal(q.complete('0'.repeat(32), 'wrong'), false);
    assert.equal(q.complete(id, 'done'), true);
    assert.equal(await first, 'done');
    assert.equal(q.complete(id, 'done'), true);
    const timeoutQueue = new DnsQueue(() => Date.now(), 20);
    const waiter = new Reply(); timeoutQueue.poll({} as any, waiter as any);
    const timeout = timeoutQueue.submit('/dns', 12345);
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.match(await timeout, /超时/);
    assert.equal(timeoutQueue.complete(waiter.body.id, 'late'), false);
    const lostWorker = new DnsQueue(() => Date.now(), 100, 5);
    const poll = new Reply(); lostWorker.poll({} as any, poll as any); poll.end();
    const notClaimed = lostWorker.submitResult('/usecf', 12345);
    await new Promise(resolve => setTimeout(resolve, 15));
    assert.equal((await notClaimed).state, 'unavailable');
    const latePoll = new Reply(); lostWorker.poll({} as any, latePoll as any);
    assert.equal(latePoll.body, undefined); latePoll.end(); // Canceled jobs cannot be delivered late.
    const uncertainQueue = new DnsQueue(() => Date.now(), 20);
    const claim = new Reply(); uncertainQueue.poll({} as any, claim as any);
    const claimed = uncertainQueue.submitResult('/usefd', 12345);
    await new Promise(resolve => setTimeout(resolve, 30));
    assert.equal((await claimed).state, 'uncertain'); // Never repeat an unconfirmed write.

    process.env.CF_DNS_BRIDGE_ENABLED = '1';
    process.env.CF_DNS_BRIDGE_KEY = 'a'.repeat(64);
    process.env.CF_DNS_OWNER_ID = '12345';
    process.env.TELEGRAM_BOT_TOKEN = '12345:unit-test-only';
    const fetchOriginal = globalThis.fetch;
    const menus: any[] = [];
    try {
        globalThis.fetch = (async (_url, init) => {
            menus.push(JSON.parse(String(init?.body)));
            return { ok: true, json: async () => ({ ok: true }) } as any;
        }) as typeof fetch;
        const complete = [{ command: 'start', description: 'Original' }, { command: 'use4', description: 'New' }];
        await syncOwnerDnsMenu(complete);
        assert.equal(menus.length, 2);
        assert.deepEqual(menus.map(menu => menu.language_code), ['', 'zh']);
        for (const menu of menus) {
            assert.deepEqual(menu.commands, complete);
            assert.deepEqual(menu.scope, { type: 'chat', chat_id: 12345 });
        }
        globalThis.fetch = (async () => { throw new Error('secret URL'); }) as typeof fetch;
        await assert.rejects(syncOwnerDnsMenu(complete), error => String(error) === 'Error: Unable to register complete owner command menu');
    } finally { globalThis.fetch = fetchOriginal; }
    const replies: string[] = [];
    const msg = { text: '/dns', senderId: 12345, chatId: '12345', isPrivate: true,
        authenticated: true, forwarded: false, date: Date.now() / 1000, allowBareIp: true,
        reply: async (value: string) => { replies.push(value); } };
    await handleCfDnsMessage({ ...msg, senderId: 1 });
    await handleCfDnsMessage({ ...msg, isPrivate: false });
    assert.deepEqual(replies, []);
    await handleCfDnsMessage({ ...msg, authenticated: false });
    assert.match(replies.pop()!, /身份验证/);
    await handleCfDnsMessage({ ...msg, forwarded: true });
    assert.match(replies.pop()!, /转发/);
    assert.equal(await handleCfDnsMessage({ ...msg, text: '/help' }), false);
    assert.equal(await handleCfDnsMessage({ ...msg, text: '1.1.1.1', allowBareIp: false }), false);
    for (const change of [{ senderId: 1 }, { isPrivate: false }, { forwarded: true }, { date: Date.now() / 1000 - 121 }]) {
        const before = replies.length;
        await handleCfDnsMessage({ ...msg, text: '/rescue 2606:4700::1', ...change });
        assert.equal(replies.slice(before).some(text => /正在由甲骨文/.test(text)), false);
    }
    // Rescue works while no N1 worker has ever polled this queue.
    const savedFetch = globalThis.fetch;
    process.env.CF_DNS_RESCUE_ENABLED = '1'; process.env.CF_DNS_RESCUE_NAME = 'edge.example.com';
    process.env.CF_DNS_RESCUE_ZONE_ID = 'b'.repeat(32); process.env.CF_DNS_RESCUE_TOKEN = 'unit-secret';
    try {
        globalThis.fetch = (async () => new Response(JSON.stringify({ success: true, result: [] }))) as typeof fetch;
        await handleCfDnsMessage({ ...msg, text: '/rescue' });
        assert.match(replies.pop()!, /线路：甲骨文/);
        process.env.CF_PROXY_RESCUE_ENABLED = '1';process.env.CF_PROXY_RESCUE_NAME = 'tj.example.com';
        process.env.CF_PROXY_RESCUE_IPS = '39.105.108.60';
        await handleCfDnsMessage({ ...msg, text: '/rescuetj' });
        assert.match(replies.pop()!, /FD 443 · tj.example.com/);
        let release!: () => void; let started!: () => void;
        const requestStarted = new Promise<void>(resolve => { started = resolve; });
        const wait = new Promise<void>(resolve => { release = resolve; });
        globalThis.fetch = (async () => { started(); await wait; return new Response(JSON.stringify({ success: true, result: [] })); }) as typeof fetch;
        const running = handleCfDnsMessage({ ...msg, text: '/rescue' });
        await requestStarted;
        await handleCfDnsMessage({ ...msg, text: '/set 172.64.229.194' });
        assert.match(replies.pop()!, /另一条 DNS 指令/);
        await handleCfDnsMessage({ ...msg, text: '/rescue' });
        assert.match(replies.pop()!, /另一条 DNS 指令/);
        release(); await running;
    } finally { globalThis.fetch = savedFetch; }

    const app = express(); app.use(express.json()); app.use('/bridge', cfDnsBridgeRouter);
    const server = app.listen(0, '127.0.0.1');
    await new Promise<void>(resolve => server.on('listening', resolve));
    const address = server.address() as { port: number };
    const url = `http://127.0.0.1:${address.port}/bridge`;
    try {
        assert.equal((await fetch(url + '/status')).status, 401);
        const headers = { Authorization: 'Bearer ' + 'a'.repeat(64), 'Content-Type': 'application/json' };
        assert.equal((await fetch(url + '/status', { headers })).status, 200);
        assert.equal((await fetch(url + '/result', { method: 'POST', headers, body: '{}' })).status, 400);
        assert.equal((await fetch(url + '/enqueue', { method: 'POST', headers, body: '{}' })).status, 404);
    } finally { server.close(); }
    console.log('DNS bridge tests passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
