import assert from 'node:assert/strict';
import { proxyRescueCommand, proxyRescueConfiguration } from './cfProxyRescue.js';
import { rescueCandidate } from './cfDnsRescue.js';

async function main() {
    Object.assign(process.env, { CF_PROXY_RESCUE_ENABLED: '1', CF_PROXY_RESCUE_NAME: 'tj.example.com',
        CF_PROXY_RESCUE_PORT: '443', CF_PROXY_RESCUE_IPS: '39.105.24.90,39.105.108.60',
        CF_DNS_RESCUE_NAME: 'bj.example.com', CF_DNS_RESCUE_TOKEN: 'test-secret',
        CF_DNS_RESCUE_ZONE_ID: 'a'.repeat(32) });
    assert.equal(proxyRescueConfiguration().client.name, 'tj.example.com');
    assert.throws(() => proxyRescueConfiguration({ ...process.env, CF_PROXY_RESCUE_NAME: 'bj.example.com' }));
    assert.throws(() => proxyRescueConfiguration({ ...process.env, CF_PROXY_RESCUE_ENABLED: '0' }));
    assert.throws(() => proxyRescueConfiguration({ ...process.env, CF_PROXY_RESCUE_IPS: 'hostname' }));
    assert.throws(() => rescueCandidate('39.105.108.60')); // Original official-only boundary stays.
    const saved = globalThis.fetch;
    let rows: any[] = [{ id: '1'.repeat(32), type: 'A', name: 'tj.example.com', content: '39.105.24.90',
        ttl: 120, proxied: false, comment: 'keep' }];
    let writes = 0, requests = 0;
    try {
        globalThis.fetch = (async (url, init) => {
            requests++;
            assert.ok(String(url).startsWith('https://api.cloudflare.com/client/v4/zones/'));
            if (init?.method === 'POST') {
                writes++;const body = JSON.parse(String(init.body));
                assert.equal(body.patches[0].content, '39.105.108.60');
                rows[0].content = body.patches[0].content;
            } else assert.ok(String(url).includes('name.exact=tj.example.com'));
            return new Response(JSON.stringify({ success: true, result: structuredClone(rows) }));
        }) as typeof fetch;
        assert.match(await proxyRescueCommand('/rescuetj'), /甲骨文直接查询（反代）：tj.example.com/);
        assert.equal(writes, 0);
        assert.match(await proxyRescueCommand('/rescuetj 39.105.108.60'), /已切换 tj.example.com/);
        assert.equal(writes, 1);assert.equal(rows[0].ttl, 120);assert.equal(rows[0].comment, 'keep');
        assert.match(await proxyRescueCommand('/rescuetj 39.105.108.60'), /未执行写入/);assert.equal(writes, 1);
        const before = requests;
        for (const ip of ['127.0.0.1', '8.8.8.8', '39.105.108.60:8443', '2606:4700::1']) {
            await assert.rejects(proxyRescueCommand('/rescuetj ' + ip));
        }
        assert.equal(requests, before);
        rows[0].content = '39.105.24.90';let calls = 0;
        globalThis.fetch = (async (_url, init) => {
            if (init?.method === 'POST') { writes++;throw new Error('lost test-secret'); }
            calls++;return new Response(JSON.stringify({ success: true, result: structuredClone(rows) }));
        }) as typeof fetch;
        const uncertain = await proxyRescueCommand('/rescuetj 39.105.108.60');
        assert.match(uncertain, /结果未确认/);assert.match(uncertain, /rescuetj/);assert.doesNotMatch(uncertain, /test-secret/);
        assert.equal(writes, 2);assert.equal(calls, 2);
    } finally { globalThis.fetch = saved; }
    console.log('Proxy rescue tests passed');
}
main().catch(error => { console.error(error);process.exitCode = 1; });
