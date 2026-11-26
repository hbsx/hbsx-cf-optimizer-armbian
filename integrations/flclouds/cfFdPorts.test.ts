import assert from 'node:assert/strict';
import { normalizeDnsCommand } from './cfDnsBridge.js';
import { CandidateCache, oracleDnsCommand, runPreferred } from './cfDnsUnified.js';
import { proxyRescueConfiguration } from './cfProxyRescue.js';

async function main() {
    const env = { CF_DNS_RESCUE_ENABLED:'1',CF_DNS_RESCUE_NAME:'bj.example.com',
        CF_DNS_RESCUE_ZONE_ID:'a'.repeat(32),CF_DNS_RESCUE_TOKEN:'ports-unit-only',
        CF_PROXY_RESCUE_ENABLED:'1',CF_PROXY_RESCUE_NAME:'tj.example.com',CF_PROXY_RESCUE_PORT:'443',
        CF_PROXY_RESCUE_IPS:'39.105.24.90,39.105.108.60',
        CF_PROXY_RESCUE_8443_NAME:'nj.example.com',CF_PROXY_RESCUE_8443_IPS:'166.88.14.71' };
    Object.assign(process.env,env);
    for (const cmd of ['fd443','fd8443','fdscan']) {
        assert.equal(normalizeDnsCommand(cmd),'/'+cmd);
        assert.equal(normalizeDnsCommand('/'+cmd+'@OriginalBot'),'/'+cmd);
        assert.equal(normalizeDnsCommand('/'+cmd+' unexpected'),'/invalid');
    }
    assert.equal(proxyRescueConfiguration(env,443).client.name,'tj.example.com');
    assert.equal(proxyRescueConfiguration(env,8443).client.name,'nj.example.com');
    assert.throws(()=>proxyRescueConfiguration({...env,CF_PROXY_RESCUE_8443_NAME:''},8443));
    assert.throws(()=>proxyRescueConfiguration({...env,CF_PROXY_RESCUE_8443_NAME:'tj.example.com'},8443));
    const cache=new CandidateCache(env);
    const stamp=new Date().toISOString();
    const snapshot={cf4:{ip:'172.64.229.167',generated_at:stamp},
        fd:{ip:'39.105.108.60',port:443,generated_at:stamp},
        fd8443:{ip:'166.88.14.71',port:8443,generated_at:stamp}};
    cache.replace(snapshot);
    assert.equal(cache.get('fd').port,443);assert.equal(cache.get('fd8443').port,8443);
    for(const candidate of [{...snapshot.fd8443,port:443},{...snapshot.fd8443,ip:snapshot.fd.ip}]) {
        cache.replace({...snapshot,fd8443:candidate});
        assert.throws(()=>cache.get('fd8443'));
        assert.equal(cache.get('fd').ip,snapshot.fd.ip);
    }
    cache.replace(snapshot);
    const records: Record<string,any[]>={
        'bj.example.com':[{id:'b'.repeat(32),type:'A',name:'bj.example.com',content:'172.64.229.91',ttl:120,proxied:false}],
        'tj.example.com':[{id:'c'.repeat(32),type:'A',name:'tj.example.com',content:'39.105.24.90',ttl:120,proxied:false}],
        'nj.example.com':[]};
    const original=globalThis.fetch;let writes=0;
    try {
        globalThis.fetch=(async(url,init)=>{
            const target=new URL(String(url));assert.equal(target.hostname,'api.cloudflare.com');
            if(init?.method==='POST') {
                writes++;const batch=JSON.parse(String(init.body));
                for(const row of batch.posts || []) records[row.name]=[{...row,id:'d'.repeat(32)}];
                for(const patch of batch.patches || []) {
                    const row=Object.values(records).flat().find(r=>r.id===patch.id);assert.ok(row);row.content=patch.content;
                }
                return new Response(JSON.stringify({success:true,result:[]}));
            }
            const name=target.searchParams.get('name.exact');assert.ok(name && name in records);
            return new Response(JSON.stringify({success:true,result:structuredClone(records[name])}));
        }) as typeof fetch;
        const query=await oracleDnsCommand('/dns',cache);
        for(const name of Object.keys(records))assert.ok(query.includes(name));
        assert.equal(writes,0);
        await oracleDnsCommand('/fd8443',cache);
        assert.equal(writes,1);assert.equal(records['nj.example.com'][0].content,'166.88.14.71');
        assert.equal(records['tj.example.com'][0].content,'39.105.24.90');
        assert.equal(records['bj.example.com'][0].content,'172.64.229.91');
        await oracleDnsCommand('/fd443',cache);
        assert.equal(writes,2);assert.equal(records['tj.example.com'][0].content,'39.105.108.60');
        assert.equal(records['nj.example.com'][0].content,'166.88.14.71');
        assert.match(await oracleDnsCommand('/fdscan',cache),/N1/);assert.equal(writes,2);
    } finally {globalThis.fetch=original;}
    let reads=0,backups=0;
    const normal=async()=>({state:'uncertain' as const,reply:'unknown'});
    const reply=await runPreferred('/fdscan',normal,async()=>{backups++;return 'backup';},async()=>{reads++;return 'DNS';});
    assert.match(reply,/未重复/);assert.equal(reads,0);assert.equal(backups,0);
    console.log('FD port isolation, three-domain DNS and manual scan command tests passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
