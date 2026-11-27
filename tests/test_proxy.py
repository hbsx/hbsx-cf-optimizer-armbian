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
    def test_mileet_import_rejects_http_proxies_bad_ports_and_non_apac(self):
        text='ip,port,tls_ok,kind,colo,asn,org,fail_count\n'
        text+='43.155.153.147,8443,1,sni-proxy,NRT,132203,Tencent,0\n'
        text+='43.155.153.148,443,1,http-proxy,NRT,132203,Tencent,0\n'
        text+='43.155.153.149,8443,0,sni-proxy,NRT,132203,Tencent,0\n'
        text+='43.155.153.150,80,1,sni-proxy,NRT,132203,Tencent,0\n'
        text+='43.155.153.151,443,1,sni-proxy,LAX,132203,Tencent,0\n'
        text+='43.155.153.152,443,1,sni-proxy,NRT,132203,Tencent,1\n'
        self.assertEqual([proxy.key(r) for r in proxy.mileet_rows(text)],[('43.155.153.147',8443)])

    def test_multiple_sources_are_deduplicated_and_partial_failure_keeps_valid_source(self):
        first=b'ip,port,tls,colo,asn,isp,fail_count\n43.155.153.147,443,true,NRT,AS132203,Tencent,0\n'
        second=first+b'43.155.153.148,8443,true,NRT,AS132203,Tencent,0\n'
        class Response:
            def __init__(self,body):self.body=body
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def read(self,_):return self.body
        class Opener:
            def open(self,url,timeout):
                url = getattr(url, 'full_url', url)
                if url==proxy.SOURCES[0][1]:return Response(first)
                if url==proxy.SOURCES[1][1]:return Response(second)
                raise OSError('unavailable')
        settings=types.SimpleNamespace(proxy='http://127.0.0.1:7890',max_candidates=10000)
        with patch.object(proxy.urllib.request,'build_opener',return_value=Opener()):
            rows=proxy.fetch_pool(settings)
        self.assertEqual({proxy.key(r) for r in rows},{('43.155.153.147',443),('43.155.153.148',8443)})
        self.assertEqual(settings.pool_failures,['mileet'])
        self.assertFalse(hasattr(settings,'allowed_nodes'))

    def test_all_source_failures_do_not_return_a_fake_successful_empty_pool(self):
        settings=types.SimpleNamespace(proxy='http://127.0.0.1:7890',max_candidates=10000)
        with patch.object(proxy.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=OSError('unavailable')
            with self.assertRaises(proxy.Problem):proxy.fetch_pool(settings)

    def test_larger_public_reserve_does_not_expand_formal_private_permission(self):
        text='ip,port,colo,provider\n'+''.join('43.155.{}.{} ,443,NRT,Tencent\n'.format(i//250,i%250+1).replace(' ','') for i in range(300))
        with self.assertRaises(proxy.Problem):proxy.seed_rows(text)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'proxy-public-candidates.csv').write_text(text)
            cfg=self.settings(directory)
            self.assertEqual(len(cfg.public_seeds),300)
            self.assertEqual(len(cfg.allowed_nodes),3)

    def test_large_subnet_reserve_rotates_bounded_work_and_all_host_offsets(self):
        text='cidr,port,colo,provider\n'+''.join('43.{}.{}.0/24,443,NRT,Tencent\n'.format(150+i//250,i%250) for i in range(300))
        ranges=proxy.cidr_rows(text)
        first=proxy.sample_cidrs(ranges,16,cycle=0)
        self.assertEqual(len(first),128*16)
        all_hosts={};visits={}
        for cycle in range(40):
            sample=proxy.sample_cidrs(ranges,16,cycle=cycle)
            self.assertEqual(len(sample),128*16)
            grouped={}
            for row in sample:grouped.setdefault('.'.join(row['ip'].split('.')[:3]),set()).add(row['ip'])
            for network,hosts in grouped.items():
                all_hosts.setdefault(network,set()).update(hosts);visits[network]=visits.get(network,0)+1
        self.assertEqual(len(all_hosts),300)
        self.assertTrue(all(len(all_hosts[k])==254 for k,n in visits.items() if n>=16))

    def settings(self, directory, **overrides):
        seed = Path(directory) / 'candidates.csv'
        seed.write_text('ip,port,colo,provider\n39.105.24.90,443,HKG,Alibaba\n'
                        '39.106.84.178,443,NRT,Alibaba\n111.119.223.194,10008,SIN,Huawei\n')
        env = dict(CF_PROXY_CANDIDATES_FILE=str(seed), CFST_OUTPUT_DIR=directory,
                   CF_PROXY_VERIFY_NODE='1', CFST_IPV6_NODE_HOST='private.example.com',
                   CFST_IPV6_NODE_PATH='/private-path', CF_PROXY_NODE_ROUNDS='2',
                   CF_PROXY_DOWNLOAD_ROUNDS='2', CF_PROXY_DOWNLOAD_PER_COLO='1')
        env.update(overrides)
        return proxy.Settings(env, base=Path(directory))

    def test_seeds_keep_endpoint_ports_and_reject_non_public_addresses(self):
        self.assertEqual(proxy.seed_rows('ip,port,colo,provider\n111.119.223.194,10008,SIN,Huawei\n')[0]['port'], 10008)
        for ip, port, colo in [('127.0.0.1', '443', 'HKG'), ('192.168.1.1', '443', 'HKG'),
                               ('100.64.0.1', '443', 'HKG'), ('224.0.0.1', '443', 'HKG'),
                               ('example.com', '443', 'HKG'), ('39.105.24.90', '65536', 'HKG'),
                               ('39.105.24.90', '443', 'LAX')]:
            with self.subTest(ip=ip, port=port, colo=colo), self.assertRaises(proxy.Problem):
                proxy.seed_rows('ip,port,colo,provider\n{},{},{},Alibaba\n'.format(ip, port, colo))

    def test_plain_http_ports_are_rejected_from_local_and_subnet_pools(self):
        for port in proxy.PLAINTEXT_PORTS:
            with self.subTest(port=port):
                with self.assertRaises(proxy.Problem):
                    proxy.seed_rows('ip,port,colo,provider\n43.155.153.147,{},NRT,Tencent\n'.format(port))
                with self.assertRaises(proxy.Problem):
                    proxy.cidr_rows('cidr,port,colo,provider\n43.155.153.0/24,{},NRT,Tencent\n'.format(port))

    def test_remote_tls_label_cannot_reintroduce_http_ports(self):
        text = 'ip,port,tls,colo,asn,isp,fail_count\n'
        for port in sorted(proxy.PLAINTEXT_PORTS | {443, 8443, 39738}):
            text += '43.155.153.147,{},true,NRT,AS132203,Tencent,0\n'.format(port)
        self.assertEqual({r['port'] for r in proxy.remote_rows(text)}, {443,8443,39738})

    def test_http_probe_is_rejected_before_any_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            prober = proxy.Prober(self.settings(directory))
            with patch.object(prober,'connect') as connect:
                for port in (80,8080):
                    with self.subTest(port=port),self.assertRaises(proxy.Problem):
                        prober.probe(dict(ip='43.155.153.147',port=port))
                connect.assert_not_called()

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

    def test_kix_seed_participates_in_node_download_and_report(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory, CF_PROXY_REFRESH_POOL='0')
            row = proxy.seed_rows('ip,port,colo,provider\n166.88.14.71,8443,KIX,User supplied\n')[0]
            settings.seeds.append(row)
            settings.allowed_nodes = frozenset(proxy.key(r) for r in settings.seeds)
            calls = []
            def probe(item, node=False, download=False):
                if proxy.key(item) == proxy.key(row): calls.append((node, download))
                return dict(ok=True, colo=item['colo'], tcp_ms=80, ready_ms=500, mbps=22)
            fake = types.SimpleNamespace(tcp=lambda r: dict(r, tcp_ms=80), probe=probe)
            with patch('builtins.print'):
                payload = proxy.scan(settings, fake)
            self.assertEqual(payload['regions']['KIX'][0]['ip'], row['ip'])
            self.assertEqual(payload['regions']['KIX'][0]['port'], 8443)
            self.assertEqual(calls.count((True, False)), settings.node_rounds)
            self.assertEqual(calls.count((False, True)), settings.download_rounds)
            report = (Path(directory) / 'report.txt').read_text(encoding='utf-8')
            self.assertIn('大阪 KIX', report)
            self.assertIn('166.88.14.71:8443', report)

    def test_apac_includes_korea_and_australia_but_excludes_us_and_europe(self):
        text = 'ip,port,tls,colo,asn,isp,fail_count\n'
        for i, colo in enumerate(['ICN', 'SYD', 'HKG', 'SJC', 'LAX', 'AMS'], 1):
            text += '43.155.1.{},8443,true,{},AS132203,Tencent,0\n'.format(i, colo)
        self.assertEqual({r['colo'] for r in proxy.remote_rows(text)}, {'ICN', 'SYD', 'HKG'})

    def test_public_local_candidates_do_not_authorize_private_node_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'proxy-public-candidates.csv').write_text(
                'ip,port,colo,provider\n43.155.153.147,2096,ICN,User supplied\n')
            settings = self.settings(directory, CF_PROXY_REFRESH_POOL='0')
            row = settings.public_seeds[0]
            self.assertNotIn(proxy.key(row), settings.allowed_nodes)
            with patch.object(proxy.Prober, 'connect') as connect, self.assertRaises(proxy.Problem):
                proxy.Prober(settings).probe(row, node=True)
            connect.assert_not_called()
            fake = types.SimpleNamespace(tcp=lambda r:dict(r,tcp_ms=30),
                probe=lambda r,**_:dict(ok=True,colo=r['colo'],tcp_ms=30,ready_ms=100,mbps=50))
            with patch('builtins.print'):
                payload = proxy.scan(settings, fake)
            self.assertEqual(payload['regions']['ICN'], [])
            self.assertTrue(any(r['ip'] == row['ip'] for r in payload['unverified']))
            self.assertLess(len((Path(directory)/'report.txt').read_text(encoding='utf-8')), 2000)

    def test_expanded_apac_pool_has_a_global_public_request_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            settings.verify_node = False
            remote = [proxy.endpoint('43.155.{}.{}'.format(i//250, i%250+1), 8443, c, 'Tencent')
                      for i, c in enumerate(proxy.COLOS * 3)]
            public = []
            def probe(row, node=False, download=False):
                if not node and not download: public.append(row)
                return dict(ok=True,colo=row['colo'],tcp_ms=30,ready_ms=100,mbps=50)
            fake = types.SimpleNamespace(tcp=lambda r:dict(r,tcp_ms=30),probe=probe)
            with patch('builtins.print'):
                proxy.scan(settings, fake, lambda _:remote)
            self.assertEqual(len(public), 240)
            self.assertTrue(set(proxy.PRIORITY_COLOS).issubset({r['colo'] for r in public}))

    def test_cidr_rotation_covers_hosts_without_private_node_permission(self):
        text = 'cidr,port,colo,provider\n43.155.153.0/24,2096,ICN,Tencent\n'
        ranges = proxy.cidr_rows(text)
        first = proxy.sample_cidrs(ranges, 16, cycle=0)
        self.assertEqual(first, proxy.sample_cidrs(ranges,16,cycle=0))
        self.assertEqual(len(first),16)
        self.assertFalse({proxy.key(r) for r in first} &
                         {proxy.key(r) for r in proxy.sample_cidrs(ranges,16,cycle=1)})
        complete = {r['ip'] for c in range(16) for r in proxy.sample_cidrs(ranges,16,cycle=c)}
        self.assertEqual(len(complete),254)
        self.assertNotIn('43.155.153.0',complete)
        self.assertNotIn('43.155.153.255',complete)
        self.assertTrue(all(r['port']==2096 and r['colo']=='ICN' for r in first))
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory)/'proxy-cidrs.csv').write_text(text)
            settings = self.settings(directory)
            self.assertEqual(len(settings.cidr_seeds),16)
            self.assertTrue(settings.allowed_nodes.isdisjoint(proxy.key(r) for r in settings.cidr_seeds))
            with patch.object(proxy.Prober,'connect') as connect,self.assertRaises(proxy.Problem):
                proxy.Prober(settings).probe(settings.cidr_seeds[0],node=True)
            connect.assert_not_called()

    def test_cidr_pool_rejects_large_private_cf_and_unaligned_ranges(self):
        for network in ['43.155.152.0/23','43.155.153.0/25','43.155.153.1/24',
                        '192.168.1.0/24','104.16.0.0/24','224.0.0.0/24']:
            with self.subTest(network=network),self.assertRaises(proxy.Problem):
                proxy.cidr_rows('cidr,port,colo,provider\n{},8443,ICN,Tencent\n'.format(network))

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
