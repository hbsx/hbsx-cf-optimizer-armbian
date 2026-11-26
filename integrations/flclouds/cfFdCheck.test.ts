import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { request as httpsRequest } from 'node:https';
import { checkFdEndpoint, verifyFdPort, checkBeforeFdWrite, fdCheckCommand, FdCheckProblem } from './cfFdCheck.js';

async function main() {
    let calls=0, destroyed=0;
    function fake(status=200, body='h=speed.cloudflare.com\ncolo=NRT\n', fail=false, timeout=false) {
        return ((options:any, callback:any)=>{
            calls++;
            assert.equal(options.hostname,'82.110.234.167');assert.equal(options.port,8443);
            assert.equal(options.servername,'speed.cloudflare.com');assert.equal(options.headers.Host,'speed.cloudflare.com');
            assert.equal(options.rejectUnauthorized,true);assert.equal(options.agent,false);
            assert.equal(options.path,'/cdn-cgi/trace');assert.equal(options.method,'GET');
            assert.equal(options.checkServerIdentity('ignored',{subjectaltname:'DNS:example.com'}).code,'ERR_TLS_CERT_ALTNAME_INVALID');
            const req=new EventEmitter() as any;let onTimeout:()=>void;
            req.destroy=()=>{destroyed++;return req;};req.setTimeout=(_delay:number,fn:()=>void)=>{onTimeout=fn;return req;};
            req.end=()=>{
                if(fail){req.emit('error',new Error('certificate mismatch'));return;}
                if(timeout){onTimeout();return;}
                const res=new EventEmitter() as any;res.statusCode=status;
                callback(res);res.emit('data',Buffer.from(body));res.emit('end');
            };
            return req;
        }) as typeof httpsRequest;
    }
    assert.deepEqual(await checkFdEndpoint('82.110.234.167',8443,fake()),{colo:'NRT'});
    assert.equal(destroyed,1);
    const cases: [number,string,boolean?,boolean?][] = [[302,''],[200,'h=example.com\ncolo=NRT'],
        [200,'h=speed.cloudflare.com\ncolo=bad'],[200,'x'.repeat(8193)],[200,'',true],[200,'',false,true]];
    for(const args of cases) {
        await assert.rejects(checkFdEndpoint('82.110.234.167',8443,fake(...args)));
    }
    await assert.rejects(checkFdEndpoint('82.110.234.167',8443,fake(429)),e=>e instanceof FdCheckProblem && e.limited);
    const before=calls;
    assert.throws(()=>checkFdEndpoint('192.168.1.160',8443,fake()));assert.equal(calls,before);
    const ports:number[]=[];
    const probe=async(_ip:string,port:443|8443)=>{ports.push(port);if(port===443)throw new FdCheckProblem('closed');return {colo:'NRT'};};
    await assert.rejects(checkBeforeFdWrite('82.110.234.167',443,probe),e=>/\/fd8443 82\.110\.234\.167:8443/.test(String(e)));
    assert.deepEqual(ports,[443,8443]);ports.length=0;
    const report=await fdCheckCommand('/fdcheck 82.110.234.167',probe);
    assert.match(report,/443：未通过/);assert.match(report,/8443：两轮/);assert.match(report,/未修改 DNS/);
    assert.deepEqual(ports,[443,8443,8443]);
    let attempts=0;
    const limited=async()=>{attempts++;throw new FdCheckProblem('limited',true);};
    await assert.rejects(checkBeforeFdWrite('82.110.234.167',443,limited));assert.equal(attempts,1);
    attempts=0;await assert.rejects(fdCheckCommand('/fdcheck 82.110.234.167',limited));assert.equal(attempts,1);
    let round=0;
    await assert.rejects(verifyFdPort('82.110.234.167',8443,async()=>({colo:++round===1?'NRT':'SIN'})));
    console.log('FD TLS port checks, response limits, timeout and rate-limit tests passed');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
