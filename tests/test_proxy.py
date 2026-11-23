import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'linux'))
import proxy_scan as proxy


class ProxyTests(unittest.TestCase):
    def settings(self, directory, **overrides):
        seed = Path(directory) / 'candidates.csv'
        seed.write_text('ip,port,colo,provider\n39.105.24.90,443,HKG,Alibaba\n'
                        '39.106.84.178,443,NRT,Alibaba\n111.119.223.194,10008,SIN,Huawei\n')
        env = dict(CF_PROXY_CANDIDATES_FILE=str(seed), CFST_OUTPUT_DIR=directory,
                   CF_PROXY_VERIFY_NODE='1', CFST_IPV6_NODE_HOST='private.example.com',
                   CFST_IPV6_NODE_PATH='/private-path', CF_PROXY_NODE_ROUNDS='2',
                   CF_PROXY_DOWNLOAD_ROUNDS='2', CF_PROXY_DOWNLOAD_PER_COLO='1')
        env.update(overrides)
        return proxy.Settings(env)

    def test_seeds_keep_endpoint_ports_and_reject_non_public_addresses(self):
        self.assertEqual(proxy.seed_rows('ip,port,colo,provider\n111.119.223.194,10008,SIN,Huawei\n')[0]['port'], 10008)
        for ip, port, colo in [('127.0.0.1', '443', 'HKG'), ('192.168.1.1', '443', 'HKG'),
                               ('100.64.0.1', '443', 'HKG'), ('224.0.0.1', '443', 'HKG'),
                               ('example.com', '443', 'HKG'), ('39.105.24.90', '65536', 'HKG'),
                               ('39.105.24.90', '443', 'LAX')]:
            with self.subTest(ip=ip, port=port, colo=colo), self.assertRaises(proxy.Problem):
                proxy.seed_rows('ip,port,colo,provider\n{},{},{},Alibaba\n'.format(ip, port, colo))

    def test_source_filters_vendor_tls_failure_region_and_keeps_ports(self):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['ip', 'port', 'tls', 'colo', 'asn', 'isp', 'fail_count'])
        writer.writerows([
            ['39.106.84.178', '443', 'true', 'NRT', 'AS37963 Alibaba', 'Alibaba', '0'],
            ['39.106.84.178', '8443', 'true', 'NRT', 'AS37963 Alibaba', 'Alibaba', '0'],
            ['8.134.67.173', '443', 'false', 'HKG', 'AS37963 Alibaba', 'Alibaba', '0'],
            ['43.155.82.88', '443', 'true', 'HKG', 'AS132203 Tencent', 'Tencent', '1'],
            ['185.194.54.122', '443', 'true', 'LAX', 'AS6233 xTom', 'xTom', '0'],
            ['8.8.8.8', '443', 'true', 'HKG', 'AS0 Unknown', 'Unknown', '0'],
            ['127.0.0.1', '443', 'true', 'HKG', 'AS37963 Alibaba', 'Alibaba', '0'],
        ])
        rows = proxy.remote_rows(output.getvalue())
        self.assertEqual({proxy.key(row) for row in rows}, {('39.106.84.178', 443), ('39.106.84.178', 8443)})
        with self.assertRaises(proxy.Problem):
            proxy.remote_rows(output.getvalue(), limit=1)
        with self.assertRaises(proxy.Problem):
            proxy.remote_rows('<html>not a CSV</html>')

    def test_disclosure_requires_local_exact_ip_and_port_before_any_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            prober = proxy.Prober(settings)
            with patch.object(prober, 'connect') as connect:
                for row in [dict(ip='43.155.82.88', port=443), dict(ip='39.105.24.90', port=8443)]:
                    with self.assertRaises(proxy.Problem):
                        prober.probe(row, node=True)
                settings.verify_node = False
                with self.assertRaises(proxy.Problem):
                    prober.probe(settings.seeds[0], node=True)
                connect.assert_not_called()

    def test_new_and_other_identified_providers_are_included_but_cf_edges_are_not(self):
        self.assertEqual(proxy.provider_name('AS400000', 'ByteVirt LLC'), 'ByteVirt')
        self.assertEqual(proxy.provider_name('AS400001', 'LUCIDACLOUD LIMITED'), 'LucidaCloud')
        self.assertEqual(proxy.provider_name('AS400002', 'Other Hosting Ltd.'), 'Other Hosting Ltd.')
        self.assertIsNone(proxy.provider_name('AS0', 'Unknown'))
        self.assertIsNone(proxy.provider_name('AS13335', 'Cloudflare Inc.'))
        header = 'ip,port,tls,colo,asn,isp,fail_count\n'
        body = ('43.153.156.248,443,true,NRT,AS400002,Other Hosting Ltd.,0\n'
                '104.16.0.1,443,true,HKG,AS400002,Other Hosting Ltd.,0\n')
        rows = proxy.remote_rows(header + body)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['brand'], 'Other Hosting Ltd.')

    def test_public_shortlist_cannot_be_taken_by_one_provider(self):
        rows = [proxy.endpoint('39.105.1.' + str(i), 443, 'HKG', 'Alibaba') for i in range(1, 15)]
        rows += [proxy.endpoint('43.153.1.' + str(i), 443, 'HKG', 'Tencent') for i in range(1, 4)]
        for index, row in enumerate(rows): row['tcp_ms'] = index + 1
        selected = proxy.choose(rows, 20, lambda row: row['tcp_ms'], per_brand=3)
        self.assertEqual(sum(row['brand'] == 'Alibaba' for row in selected), 3)
        self.assertEqual(sum(row['brand'] == 'Tencent' for row in selected), 3)

    def test_downloads_require_every_round_full_same_colo_and_minimum_speed(self):
        settings = types.SimpleNamespace(download_rounds=2, min_mbps=8)
        row = dict(colo='NRT')
        valid = dict(ok=True, colo='NRT', mbps=20)
        self.assertTrue(proxy.qualified_downloads(row, [valid, valid], settings))
        for samples in [[valid], [valid, dict(valid, ok=False)], [valid, dict(valid, colo='HKG')],
                        [valid, dict(valid, mbps=7)], [valid, dict(valid, mbps=float('nan'))]]:
            self.assertFalse(proxy.qualified_downloads(row, samples, settings))

    def test_stage_pipeline_never_verifies_remote_addresses_and_has_no_disk_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            other_lists = {}
            for filename in ['best-ips.txt', 'best-ips-v6.txt']:
                path = Path(directory) / filename
                path.write_text('unchanged')
                other_lists[filename] = path
            remote = [proxy.endpoint('43.155.82.88', 443, 'HKG', 'Tencent')]
            class Fake:
                def __init__(self): self.nodes = []
                def tcp(self, row): return dict(row, tcp_ms=30)
                def probe(self, row, node=False, download=False):
                    if node: self.nodes.append(proxy.key(row))
                    return dict(ok=True, colo=row['colo'], tcp_ms=30, ready_ms=100, mbps=50)
            fake = Fake()
            with patch('builtins.print'):
                payload = proxy.scan(settings, fake, lambda _: remote)
            self.assertEqual(set(fake.nodes), settings.allowed_nodes)
            self.assertNotIn(('43.155.82.88', 443), fake.nodes)
            self.assertEqual(payload['unverified'][0]['ip'], '43.155.82.88')
            self.assertEqual(payload['regions']['SIN'][0]['port'], 10008)
            report = (Path(directory) / 'report.txt').read_text(encoding='utf-8')
            self.assertNotIn('/private-path', report)
            self.assertNotIn('private.example.com', report)
            for path in other_lists.values(): self.assertEqual(path.read_text(), 'unchanged')
            self.assertFalse(any(path.name.startswith('.proxy-') for path in Path(directory).iterdir()))

    def test_source_failure_falls_back_to_current_local_seeds_without_old_report(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            fake = types.SimpleNamespace(tcp=lambda row: None, probe=lambda *args, **kwargs: None)
            def failed(_): raise OSError('private-secret-that-must-not-be-shown')
            with patch('builtins.print'):
                proxy.scan(settings, fake, failed)
            report = (Path(directory) / 'report.txt').read_text(encoding='utf-8')
            self.assertIn('仅测试本机候选', report)
            self.assertNotIn('private-secret', report)
            self.assertTrue(all(not rows for rows in json.loads((Path(directory) / 'best-proxy-ips.json').read_text())['regions'].values()))

    def test_rate_limit_aborts_without_overwriting_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory, CF_PROXY_REFRESH_POOL='0')
            snapshot = Path(directory) / 'best-proxy-ips.json'
            snapshot.write_text('old snapshot')
            def limited(*args, **kwargs): raise proxy.RateLimited('limited')
            fake = types.SimpleNamespace(tcp=lambda row: dict(row, tcp_ms=30), probe=limited)
            with patch('builtins.print'), self.assertRaises(proxy.RateLimited):
                proxy.scan(settings, fake)
            self.assertEqual(snapshot.read_text(), 'old snapshot')
            self.assertFalse((Path(directory) / 'report.txt').exists())

    def test_success_calls_auto_after_fresh_snapshot_and_reports_dns_result(self):
        import proxy_control
        with tempfile.TemporaryDirectory() as directory:
            settings=self.settings(directory,CF_PROXY_REFRESH_POOL='0',CF_PROXY_DNS_AUTO_ENABLED='1')
            fake=types.SimpleNamespace(tcp=lambda row:dict(row,tcp_ms=30),
                probe=lambda row,**_:dict(ok=True,colo=row['colo'],tcp_ms=30,ready_ms=100,mbps=50))
            def update(cfg,payload):
                self.assertEqual(json.loads((Path(directory)/'best-proxy-ips.json').read_text()),payload)
                return 'DNS_UPDATE_RESULT'
            with patch.object(proxy_control,'auto_after_scan',side_effect=update) as auto,patch('builtins.print'):
                proxy.scan(settings,fake)
            auto.assert_called_once()
            self.assertIn('DNS_UPDATE_RESULT',(Path(directory)/'report.txt').read_text(encoding='utf-8'))
            self.assertNotIn('本模式不修改 DNS',(Path(directory)/'report.txt').read_text(encoding='utf-8'))

    def test_private_node_budget_is_global_across_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory, CF_PROXY_MAX_NODE_CANDIDATES='3')
            for index in range(10):
                settings.seeds.extend(proxy.endpoint('39.105.{}.{}'.format(index, 10 + i), 443, colo, 'Alibaba') for i, colo in enumerate(proxy.COLOS))
            settings.allowed_nodes = frozenset(proxy.key(row) for row in settings.seeds)
            seen = set()
            def probe(row, node=False, download=False):
                if node: seen.add(proxy.key(row))
                return dict(ok=True, colo=row['colo'], tcp_ms=30, ready_ms=100, mbps=50)
            fake = types.SimpleNamespace(tcp=lambda row: dict(row, tcp_ms=30), probe=probe)
            with patch('builtins.print'):
                proxy.scan(settings, fake, lambda _: [])
            self.assertEqual(len(seen), 3)

    def test_fast_443_addresses_do_not_exclude_other_port_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory, CF_PROXY_MAX_NODE_CANDIDATES='3', CF_PROXY_REFRESH_POOL='0')
            settings.seeds = [proxy.endpoint('39.105.1.' + str(i), 443, 'HKG', 'Alibaba') for i in range(1, 8)]
            settings.seeds += [proxy.endpoint('43.153.1.1', 8443, 'HKG', 'Tencent'),
                               proxy.endpoint('111.119.223.194', 10008, 'HKG', 'Huawei')]
            settings.allowed_nodes = frozenset(proxy.key(r) for r in settings.seeds)
            seen = set()
            def probe(row, node=False, download=False):
                if node: seen.add(row['port'])
                return dict(ok=True, colo=row['colo'], tcp_ms=10 if row['port']==443 else 50,
                            ready_ms=10 if row['port']==443 else 80, mbps=50)
            fake=types.SimpleNamespace(tcp=lambda row:dict(row,tcp_ms=10 if row['port']==443 else 50),probe=probe)
            with patch('builtins.print'):payload=proxy.scan(settings,fake)
            self.assertEqual(seen,{443,8443,10008})
            self.assertEqual({r['port'] for r in payload['regions']['HKG']},{443,8443,10008})
            report=(Path(directory)/'report.txt').read_text(encoding='utf-8')
            self.assertIn('443 端口',report);self.assertIn('8443 端口',report);self.assertIn('其他端口',report)

    def test_other_ports_share_slots_across_distinct_ports(self):
        rows=[proxy.endpoint('39.105.1.'+str(i),2053,'HKG','Alibaba') for i in range(1,8)]
        rows += [proxy.endpoint('43.153.1.1',10086,'HKG','Tencent'),proxy.endpoint('111.119.223.194',10008,'HKG','Huawei')]
        for i,row in enumerate(rows):row['ready_ms']=i+1
        selected=proxy.choose_ports(rows,3,lambda row:row['ready_ms'],per_brand=3)
        self.assertEqual({r['port'] for r in selected},{2053,10086,10008})


if __name__ == '__main__':
    unittest.main()
