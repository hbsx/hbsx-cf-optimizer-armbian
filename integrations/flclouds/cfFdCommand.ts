import { BlockList, isIP } from 'node:net';
import { RescueProblem } from './cfDnsRescue.js';

const blocked = new BlockList();
for (const cidr of ['0.0.0.0/8','10.0.0.0/8','100.64.0.0/10','127.0.0.0/8',
    '169.254.0.0/16','172.16.0.0/12','192.0.0.0/24','192.0.2.0/24','192.88.99.0/24',
    '192.168.0.0/16','198.18.0.0/15','198.51.100.0/24','203.0.113.0/24','224.0.0.0/4','240.0.0.0/4']) {
    const [address, prefix] = cidr.split('/'); blocked.addSubnet(address, Number(prefix), 'ipv4');
}

export function customFdAddress(value: string): string {
    if (isIP(value) !== 4 || blocked.check(value, 'ipv4')) {
        throw new RescueProblem('FD 自定义地址须为公网 IPv4，不接受内网、保留地址或域名。');
    }
    return value;
}

// Bare commands select the measured winner; an explicit address authorizes only
// this one DNS change. It never enlarges a scanner/cache/rescue allowlist.
export function parseFdCommand(command: string): { port: 443 | 8443; ip?: string } | null {
    const match = command.match(/^\/fd(443|8443)(?:\s+([^\s]+))?$/);
    if (!match) return null;
    const port = Number(match[1]) as 443 | 8443;
    if (!match[2]) return { port };
    const parts = match[2].split(':');
    if (parts.length > 2 || parts.length === 2 && parts[1] !== String(port)) {
        throw new RescueProblem('IP 后的端口必须与命令一致：/fd443 IP:443 或 /fd8443 IP:8443。');
    }
    return { port, ip: customFdAddress(parts[0]) };
}
