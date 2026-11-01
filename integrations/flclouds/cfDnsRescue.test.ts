import assert from 'node:assert/strict';
import { rescueCandidate, rescueSwitch, CloudflareRescue, type DnsRecord, type RescueClient } from './cfDnsRescue.js';

const name = 'edge.example.com', v4 = '172.64.229.194', v6 = '2606:4700:ff00::2';
function row(type: 'A' | 'AAAA' = 'A', content = v4, id = '1'): DnsRecord {
    return { id: id.repeat(32), type, name, content, ttl: 120, proxied: false, comment: 'keep', tags: ['owner:test'] };
}
class Fake implements RescueClient {
    name = name; rows: DnsRecord[]; writes: any[] = []; reads = 0;
    constructor(rows = [row()]) { this.rows = structuredClone(rows); }
    async records() { this.reads++; return structuredClone(this.rows); }
    async batch(body: any) {
        this.writes.push(structuredClone(body));
        this.rows = this.rows.filter(r => !body.deletes.some((x: any) => x.id === r.id));
        for (const patch of body.patches || []) Object.assign(this.rows.find(r => r.id === patch.id)!, patch);
        for (const post of body.posts || []) this.rows.push({ ...post, id: 'f'.repeat(32) });
    }
}
async function main() {
    assert.equal(rescueCandidate('2606:4700:ff00:0:0:0:0:2'), v6);
    assert.equal(rescueCandidate(v4), v4);
    for (const ip of ['localhost', '127.0.0.1', '192.168.1.1', '8.8.8.8', '::1', 'fe80::1%eth0',
        '::ffff:172.64.229.194', '2606:4700::1%eth0', '2606:4701::1', '[2606:4700::1]:443']) {
        assert.throws(() => rescueCandidate(ip), undefined, ip);
    }
    for (const [before, next, type] of [[row(), v6, 'AAAA'], [row('AAAA', v6), v4, 'A']] as const) {
        const client = new Fake([before]);
        const reply = await rescueSwitch(next, client);
        assert.match(reply, /已切换/); assert.match(reply, /未验证家中线路/);
        assert.equal(client.writes.length, 1);
        assert.deepEqual(client.rows, [{ id: 'f'.repeat(32), type, name, content: next, ttl: 120,
            proxied: false, comment: 'keep', tags: ['owner:test'] }]);
    }
    const multi = new Fake([row('A', '172.64.229.91'), row('AAAA', v6, '2')]);
    await rescueSwitch(v4, multi);
    assert.deepEqual(multi.writes[0], { deletes: [{ id: '2'.repeat(32) }], patches: [{ id: '1'.repeat(32), content: v4 }] });
    assert.equal(multi.rows[0].ttl, 120); assert.equal(multi.rows[0].comment, 'keep');
    const emptyMetadata = new Fake([{ ...row(), comment: null, tags: null } as any]);
    await rescueSwitch(v6, emptyMetadata);
    assert.equal('comment' in emptyMetadata.writes[0].posts[0], false);
    assert.equal('tags' in emptyMetadata.writes[0].posts[0], false);
    const same = new Fake([row('AAAA', '2606:4700:ff00:0:0:0:0:2')]);
    assert.match(await rescueSwitch(v6, same), /未执行写入/); assert.equal(same.writes.length, 0);
    const changed = new Fake();
    changed.records = async () => ++changed.reads === 1 ? [row()] : [row('AAAA', v6)];
    assert.match(await rescueSwitch(v6, changed), /DNS 已变化/); assert.equal(changed.writes.length, 0);
    const lost = new Fake(); lost.batch = async body => { lost.writes.push(body); throw new Error('lost secret response'); };
    const uncertain = await rescueSwitch(v6, lost);
    assert.match(uncertain, /结果未确认/); assert.match(uncertain, /不会自动重试/);
    assert.doesNotMatch(uncertain, /secret/); assert.equal(lost.writes.length, 1);
    const mismatch = new Fake(); mismatch.batch = async body => { mismatch.writes.push(body); };
    assert.match(await rescueSwitch(v6, mismatch), /结果未确认/); assert.equal(mismatch.writes.length, 1);

    const env = { CF_DNS_RESCUE_ENABLED: '1', CF_DNS_RESCUE_NAME: name,
        CF_DNS_RESCUE_ZONE_ID: 'a'.repeat(32), CF_DNS_RESCUE_TOKEN: 'unit-secret' };
    assert.throws(() => new CloudflareRescue({ ...env, CF_DNS_RESCUE_ENABLED: '0' }));
    assert.throws(() => new CloudflareRescue({ ...env, CF_DNS_RESCUE_NAME: '*.' + name }));
    const fetchOriginal = globalThis.fetch;
    try {
        const client = new CloudflareRescue(env);
        for (const records of [[{ ...row(), type: 'CNAME' }], [{ ...row(), proxied: true }],
            [{ ...row(), name: 'other.example.com' }], [row(), row()]]) {
            globalThis.fetch = (async () => new Response(JSON.stringify({ success: true, result: records, result_info: { total_pages: 1, total_count: records.length } }))) as typeof fetch;
            await assert.rejects(client.records());
        }
        globalThis.fetch = (async () => new Response(JSON.stringify({ success: true, result: [row()], result_info: { total_pages: 2, total_count: 2 } }))) as typeof fetch;
        await assert.rejects(client.records(), /响应不完整/);
        globalThis.fetch = (async (url, init) => {
            assert.match(String(url), /^https:\/\/api\.cloudflare\.com\/client\/v4\/zones\/a{32}\/dns_records\?/);
            assert.match(String(url), /name.exact=edge.example.com/);
            assert.equal(init?.redirect, 'error'); assert.equal(init?.method, 'GET');
            return new Response(JSON.stringify({ success: true, result: [row()] }));
        }) as typeof fetch;
        assert.deepEqual(await client.records(), [row()]);
        globalThis.fetch = (async () => { throw new Error('unit-secret'); }) as typeof fetch;
        await assert.rejects(client.records(), error => !String(error).includes('unit-secret') && /连接或响应失败/.test(String(error)));
    } finally { globalThis.fetch = fetchOriginal; }
    console.log('DNS rescue tests passed');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
