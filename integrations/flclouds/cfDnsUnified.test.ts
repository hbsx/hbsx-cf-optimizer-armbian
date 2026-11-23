import assert from 'node:assert/strict';
import { CandidateCache, runPreferred, oracleDnsCommand, type N1Result } from './cfDnsUnified.js';

async function main() {
    const env = { CF_DNS_RESCUE_ENABLED: '1', CF_DNS_RESCUE_NAME: 'bj.example.com',
        CF_DNS_RESCUE_ZONE_ID: 'a'.repeat(32), CF_DNS_RESCUE_TOKEN: 'unit-only',
        CF_PROXY_RESCUE_ENABLED: '1', CF_PROXY_RESCUE_NAME: 'tj.example.com',
        CF_PROXY_RESCUE_PORT: '443', CF_PROXY_RESCUE_IPS: '39.105.24.90' };
    Object.assign(process.env, env);
    let now = Date.now();
    const cache = new CandidateCache(env, () => now);
    const generated_at = new Date(now).toISOString();
    const snapshot = { cf4: { ip: '172.64.229.167', generated_at },
        cf6: { ip: '2606:4700::1', generated_at }, fd: { ip: '39.105.24.90', port: 443, generated_at } };
    cache.replace(snapshot);
    assert.equal(cache.get('cf4').ip, snapshot.cf4.ip);
    assert.equal(cache.get('cf6').ip, snapshot.cf6.ip);
    assert.equal(cache.get('fd').port, 443);
    cache.get('cf4').ip = 'modified';
    assert.equal(cache.get('cf4').ip, snapshot.cf4.ip);
    for (const cf4 of [{ ...snapshot.cf4, ip: '1.1.1.1' }, { ...snapshot.cf4, ip: snapshot.cf6.ip },
        { ...snapshot.cf4, generated_at: 'invalid' }, { ...snapshot.cf4, generated_at: new Date(now + 301_000).toISOString() }]) {
        assert.throws(() => cache.replace({ ...snapshot, cf4 }));
        assert.equal(cache.get('cf4').ip, snapshot.cf4.ip); // Updates are atomic.
    }
    for (const fd of [{ ...snapshot.fd, port: 8443 }, { ...snapshot.fd, ip: '8.8.8.8' }]) {
        cache.replace({ ...snapshot, fd });
        assert.throws(() => cache.get('fd')); // No dynamic expansion of the independent allowlist.
        assert.equal(cache.get('cf4').ip, snapshot.cf4.ip);
    }
    cache.replace(snapshot);
    now += 96 * 3600_000 + 1;
    assert.throws(() => cache.get('cf4'));
    assert.equal(cache.status().cf4, null);
    assert.throws(() => cache.replace(snapshot));
    now -= 96 * 3600_000 + 1;
    cache.replace(snapshot);
    let backups = 0, reads = 0;
    const backup = async () => { backups++; return 'oracle'; };
    const readback = async () => { reads++; return 'current DNS'; };
    const normal = (state: N1Result['state']) => async () => ({ state, reply: 'N1 reply' });
    for (const state of ['ok', 'busy', 'failed'] as const) {
        assert.equal(await runPreferred('/usecf', normal(state), backup, readback), 'N1 reply');
    }
    assert.equal(backups, 0);
    for (const cmd of ['/dns', '/usecf', '/usefd', '/use6']) {
        assert.equal(await runPreferred(cmd, normal('unavailable'), backup, readback), 'oracle');
    }
    assert.equal(backups, 4);
    assert.match(await runPreferred('/usecf', normal('uncertain'), backup, readback), /不会重复提交/);
    assert.equal(backups, 4); assert.equal(reads, 1);
    assert.equal(await runPreferred('/dns', normal('uncertain'), backup, readback), 'oracle');

    // An offline N1 uses its cached winner, and Oracle performs API calls only.
    const original = globalThis.fetch;
    const calls: string[] = []; let current = '172.64.229.91'; let writes = 0;
    try {
        globalThis.fetch = (async (url, init) => {
            const target = String(url); calls.push(target);
            assert.equal(new URL(target).hostname, 'api.cloudflare.com');
            if (init?.method === 'POST') {
                writes++;
                const batch = JSON.parse(String(init.body));
                current = batch.patches[0].content;
            }
            const isFd = target.includes(encodeURIComponent('tj.example.com'));
            return new Response(JSON.stringify({ success: true, result: [{ id: 'b'.repeat(32), type: 'A',
                name: isFd ? 'tj.example.com' : 'bj.example.com', content: isFd ? '39.105.24.90' : current,
                ttl: 60, proxied: false }] }));
        }) as typeof fetch;
        const query = await oracleDnsCommand('/dns', cache);
        assert.match(query, /CF · bj.example.com/); assert.match(query, /FD · tj.example.com/);
        assert.equal(writes, 0);
        const result = await oracleDnsCommand('/usecf', cache);
        assert.equal(writes, 1); assert.equal(current, snapshot.cf4.ip);
        assert.match(result, /N1 优选时间/);
        assert.equal(calls.some(url => !url.startsWith('https://api.cloudflare.com/')), false);
    } finally { globalThis.fetch = original; }
    console.log('Unified DNS failover and RAM snapshot tests passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
