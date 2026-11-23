import { isIP } from 'node:net';
import { CloudflareRescue, rescueSwitch, RescueProblem } from './cfDnsRescue.js';

export function proxyRescueConfiguration(env: NodeJS.ProcessEnv = process.env) {
    const name = (env.CF_PROXY_RESCUE_NAME || '').toLowerCase().replace(/\.$/, '');
    const original = (env.CF_DNS_RESCUE_NAME || '').toLowerCase().replace(/\.$/, '');
    const port = env.CF_PROXY_RESCUE_PORT || '443';
    const ips = [...new Set((env.CF_PROXY_RESCUE_IPS || '').split(',').map(x => x.trim()).filter(Boolean))];
    if (env.CF_PROXY_RESCUE_ENABLED !== '1' || !name || name === original || !original
        || !/^[1-9][0-9]{0,4}$/.test(port) || Number(port) > 65535 || !ips.length || ips.length > 30
        || ips.some(ip => isIP(ip) !== 4)) {
        throw new RescueProblem('甲骨文反代备用入口尚未正确配置，未修改 DNS。');
    }
    const client = new CloudflareRescue({ ...env, CF_DNS_RESCUE_ENABLED: '1', CF_DNS_RESCUE_NAME: name });
    return { client, ips, port };
}

export async function proxyRescueCommand(command: string): Promise<string> {
    const { client, ips, port } = proxyRescueConfiguration();
    if (command === '/rescuetj') {
        const rows = await client.records();
        return `甲骨文直接查询（反代）：${client.name}\n${rows.map(r => `${r.type} ${r.content}`).join('\n') || '暂无地址记录。'}\n端口：${port}\n备用允许地址：${ips.join('、')}\n切换用 /rescuetj IPv4；不依赖 N1，不验证家中线路或节点可用性。`;
    }
    const match = command.match(/^\/rescuetj ([0-9.]+)$/);
    if (!match) throw new RescueProblem('用法：/rescuetj 查询，/rescuetj IPv4 指定反代备用地址。');
    const validate = (value: string) => {
        if (isIP(value) !== 4 || !ips.includes(value)) {
            throw new RescueProblem('该地址不在甲骨文配置的同端口反代允许列表中；请 /rescuetj 查询。');
        }
        return value;
    };
    return await rescueSwitch(match[1], client, validate, '/rescuetj') + `\n反代端口：${port}（客户端保持不变）。`;
}
