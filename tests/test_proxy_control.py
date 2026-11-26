import contextlib
import copy
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'linux'))
import proxy_control as pc
import telegram_control as control


class Client:
    name = 'tj.example.com'
    def __init__(self):
        self.rows = [dict(id='a' * 32, type='A', name=self.name, content='39.105.24.90',
                          ttl=120, proxied=False, comment='keep')]
        self.writes = []
    def records(self):
        return copy.deepcopy(self.rows)
    def request(self, method, path, body):
        self.writes.append(copy.deepcopy(body))
        for item in body.get('patches', []):
            next(r for r in self.rows if r['id'] == item['id']).update(item)


class ProxyControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.row = dict(ip='39.105.108.60', port=443, colo='HKG', brand='test',
                        mbps=100, tcp_ms=20, node_verified=True)
        self.config = SimpleNamespace(output=Path(self.temp.name),
            seeds=[self.row], allowed_nodes=frozenset([('39.105.108.60', 443)]), check_runtime=Mock())
        self.settings = SimpleNamespace(colo='NRT')
        p = patch.object(pc, 'configuration', return_value=(('token', 'zone', Client.name), 443, self.config))
        p.start();self.addCleanup(p.stop)
        self.write_snapshot()

    def write_snapshot(self, **changes):
        data = dict(generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    node_verified=True, regions={'HKG': [self.row]})
        data.update(changes)
        (self.config.output / 'best-proxy-ips.json').write_text(json.dumps(data))

    def call(self, text, client=None, probe=None):
        return pc.handle(text, self.settings, client=client or Client(), probe=probe or Mock(return_value=20),
                         sleep=lambda _: None, lock=contextlib.nullcontext)

    def test_query_never_probes_or_writes(self):
        c, probe = Client(), Mock()
        answer = self.call('/tj', c, probe)
        self.assertIn(Client.name, answer)
        self.assertIn('39.105.108.60:443', answer)
        self.assertFalse(c.writes);probe.assert_not_called()

    def test_faster_other_ports_cannot_replace_a_443_dns_candidate(self):
        other=dict(self.row,ip='8.216.95.3',port=39738,mbps=1000)
        self.config.allowed_nodes=frozenset([('39.105.108.60',443),('8.216.95.3',39738)])
        self.write_snapshot(regions={'HKG':[other,self.row]})
        rows,_=pc.snapshot(self.config,443)
        self.assertEqual([r['ip'] for r in rows],['39.105.108.60'])

    def test_cached_http_candidate_cannot_be_used_even_with_matching_port(self):
        for port in (80,8080):
            with self.subTest(port=port):
                row=dict(self.row,port=port,mbps=1000)
                self.config.allowed_nodes=frozenset([(row['ip'],port)])
                self.write_snapshot(regions={'HKG':[row]})
                rows,_=pc.snapshot(self.config,port)
                self.assertEqual(rows,[])

    def test_8443_command_uses_8443_winner_and_separate_domain(self):
        row=dict(self.row,ip='166.88.14.71',port=8443,colo='KIX',mbps=30)
        self.config.allowed_nodes=frozenset([('39.105.108.60',443),('166.88.14.71',8443)])
        self.write_snapshot(regions={'HKG':[dict(self.row,mbps=1000)],'KIX':[row]})
        client=Client();client.name='nj.example.com'
        for record in client.rows:record['name']=client.name
        probe=Mock(return_value=20)
        with patch.object(pc,'configuration',return_value=(('token','zone',client.name),8443,self.config)) as cfg:
            result=self.call('/fd8443',client,probe)
        cfg.assert_called_once_with(self.settings,8443)
        self.assertEqual(client.rows[0]['content'],'166.88.14.71')
        self.assertEqual(probe.call_count,2)
        self.assertTrue(all(c.args[0]['port']==8443 for c in probe.call_args_list))
        self.assertIn('nj.example.com',result)

    def test_443_command_does_not_choose_a_faster_8443_winner(self):
        row=dict(self.row,ip='166.88.14.71',port=8443,colo='KIX',mbps=1000)
        self.config.allowed_nodes=frozenset([('39.105.108.60',443),('166.88.14.71',8443)])
        self.write_snapshot(regions={'HKG':[self.row],'KIX':[row]})
        client=Client();probe=Mock(return_value=20)
        self.call('/fd443',client,probe)
        self.assertEqual(client.rows[0]['content'],self.row['ip'])
        self.assertTrue(all(c.args[0]['port']==443 for c in probe.call_args_list))

    def test_switch_targets_separate_hostname_and_rechecks_twice(self):
        c, probe = Client(), Mock(return_value=20)
        answer = self.call('/usetj HKG 1', c, probe)
        self.assertIn('已切换', answer);self.assertIn(Client.name, answer)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(c.rows[0]['content'], self.row['ip'])
        self.assertEqual(c.rows[0]['ttl'], 120);self.assertEqual(c.rows[0]['comment'], 'keep')
        self.assertEqual(len(c.writes), 1)
        self.assertEqual(self.settings.colo, 'NRT')

    def test_second_probe_failure_never_writes(self):
        c = Client()
        with self.assertRaises(control.Problem):
            self.call('/usetj', c, Mock(side_effect=[20, control.Problem('failed')]))
        self.assertFalse(c.writes)

    def test_each_round_reports_actual_old_and_new_tcp_values(self):
        self.config.allowed_nodes=frozenset([('39.105.108.60',443),('39.105.24.90',443)])
        tcp=Mock(side_effect=[{'tcp_ms':25.5},{'tcp_ms':30.75}])
        with patch.object(pc.proxy_scan,'Prober',return_value=SimpleNamespace(tcp=tcp)):
            answer=self.call('/usetj',probe=Mock(side_effect=[20.1,21.2]))
        self.assertIn('第 1 轮：旧最快 39.105.24.90 25.50 ms；新 39.105.108.60 20.10 ms',answer)
        self.assertIn('第 2 轮：旧最快 39.105.24.90 30.75 ms；新 39.105.108.60 21.20 ms',answer)
        self.assertEqual(tcp.call_count,2)

    def test_manual_address_outside_local_allowlist_never_probes(self):
        probe, c = Mock(), Client()
        with self.assertRaises(control.Problem):self.call('/settj 8.8.8.8', c, probe)
        probe.assert_not_called();self.assertFalse(c.writes)
        self.assertIn('已切换', self.call('/settj 39.105.108.60'))

    def test_custom_8443_changes_only_its_domain_without_enlarging_allowlist(self):
        client=Client();client.name='nj.example.com'
        for record in client.rows:record['name']=client.name
        allowed=self.config.allowed_nodes
        public=Mock(return_value=dict(ok=True,colo='NRT',tcp_ms=30))
        private=Mock()
        with patch.object(pc,'configuration',return_value=(('token','zone',client.name),8443,self.config)):
            result=pc.handle('/fd8443 82.110.234.167:8443',self.settings,client=client,probe=private,
                             public_probe=public,sleep=lambda _:None,lock=contextlib.nullcontext)
        self.assertEqual(client.rows[0]['content'],'82.110.234.167')
        self.assertEqual(public.call_count,2);private.assert_not_called()
        self.assertEqual(self.config.allowed_nodes,allowed)
        self.assertTrue(all(c.args[0]['port']==8443 for c in public.call_args_list))
        self.assertIn('不验证实际节点',result);self.assertIn('公开 TLS 检查通过',result)

    def test_custom_invalid_address_or_wrong_port_never_reads_dns(self):
        for value in ['127.0.0.1','192.168.1.160','169.254.169.254','100.64.0.1','224.0.0.1',
                      '192.0.0.9','198.18.0.1','203.0.113.1','255.255.255.255','::1',
                      'example.com','82.110.234.167:443','82.110.234.167:80','82.110.234.167 extra']:
            client=Mock()
            with self.subTest(value=value),self.assertRaises(control.Problem):
                self.call('/fd8443 '+value,client)
            client.records.assert_not_called();client.request.assert_not_called()

    def test_custom_second_round_failure_or_colo_change_never_writes(self):
        for second in [control.Problem('failed'),dict(colo='LAX',tcp_ms=10),dict(colo='SIN',tcp_ms=10)]:
            client=Client()
            public=Mock(side_effect=[dict(colo='NRT',tcp_ms=20),second])
            with self.subTest(second=second),self.assertRaises(control.Problem):
                pc.handle('/fd443 82.110.234.167',self.settings,client=client,public_probe=public,
                          sleep=lambda _:None,lock=contextlib.nullcontext)
            self.assertFalse(client.writes)

    def test_custom_public_probe_never_uses_private_node_data(self):
        prober=Mock();prober.probe.return_value=dict(ok=True,colo='NRT',tcp_ms=20)
        row=dict(ip='82.110.234.167',port=8443,colo='')
        with patch.object(pc.proxy_scan,'Prober',return_value=prober):
            self.assertEqual(pc.verify_custom(row,self.config)['colo'],'NRT')
        prober.probe.assert_called_once_with(row)
        prober.probe.return_value=dict(ok=False,colo='NRT')
        with patch.object(pc.proxy_scan,'Prober',return_value=prober),self.assertRaises(control.Problem):
            pc.verify_custom(row,self.config)

    def test_fdcheck_detects_ports_without_dns_or_private_node_calls(self):
        def public(row,config):
            if row['port']==443:raise control.Problem('closed')
            return dict(colo='NRT',tcp_ms=20)
        with patch.object(control,'ControlDNS') as dns:
            reply=pc.check_fd('/fdcheck 82.110.234.167',self.settings,public_probe=public,
                              lock=contextlib.nullcontext,sleep=lambda _:None)
        dns.assert_not_called()
        self.assertIn('443：未通过',reply);self.assertIn('8443：两轮',reply)
        self.assertIn('/fd8443 82.110.234.167:8443',reply);self.assertIn('未修改 DNS',reply)

    def test_wrong_service_port_suggests_alternative_without_changing_dns(self):
        client=Client();ports=[]
        def public(row,config):
            ports.append(row['port'])
            if row['port']==443:raise control.Problem('closed')
            return dict(colo='NRT',tcp_ms=20)
        with self.assertRaises(control.Problem) as error:
            pc.handle('/fd443 82.110.234.167',self.settings,client=client,public_probe=public,
                      sleep=lambda _:None,lock=contextlib.nullcontext)
        self.assertFalse(client.writes);self.assertEqual(ports,[443,8443])
        self.assertIn('/fd8443 82.110.234.167:8443',str(error.exception))

    def test_rate_limit_never_probes_alternate_port_or_continues_checks(self):
        public=Mock(side_effect=pc.CheckLimited('limited'))
        for command in ['/fdcheck 82.110.234.167','/fd443 82.110.234.167']:
            public.reset_mock();client=Client()
            with self.subTest(command=command),self.assertRaises(control.Problem):
                if command.startswith('/fdcheck'):
                    pc.check_fd(command,self.settings,public_probe=public,lock=contextlib.nullcontext,sleep=lambda _:None)
                else:
                    pc.handle(command,self.settings,client=client,public_probe=public,lock=contextlib.nullcontext)
            self.assertEqual(public.call_count,1);self.assertFalse(client.writes)

    def test_stale_unverified_snapshot_and_absent_rank_refuse_writes(self):
        for change in [dict(generated_at='2020-01-01T00:00:00+00:00'),dict(node_verified=False),
                       dict(generated_at='2099-01-01T00:00:00+00:00')]:
            self.write_snapshot(**change)
            c=Client()
            with self.assertRaises(control.Problem):self.call('/usetj',c)
            self.assertFalse(c.writes)
        self.write_snapshot()
        for text in ['/usetj NRT', '/usetj HKG 2']:
            with self.assertRaises(control.Problem):self.call(text)

    def test_snapshot_excludes_wrong_port_unverified_and_unknown_endpoints(self):
        variants=[self.row,dict(self.row,port=8443),dict(self.row,node_verified=False),dict(self.row,ip='8.8.8.8')]
        self.write_snapshot(regions={'HKG':variants})
        rows,_=pc.snapshot(self.config,443)
        self.assertEqual(rows,[self.row])

    def test_dns_race_and_uncertain_write_are_not_replayed(self):
        c=Client()
        before=c.records();changed=[dict(before[0],content='39.106.2.1')]
        with patch.object(c,'records',side_effect=[before,changed]):
            self.assertIn('/tj',self.call('/usetj',c))
        self.assertFalse(c.writes)
        with patch.object(c,'request',side_effect=pc.dns_update.DNSProblem('secret')) as write:
            answer=self.call('/usetj',c)
        self.assertIn('未确认',answer);self.assertIn('/tj',answer)
        self.assertNotIn('secret',answer);write.assert_called_once()

    def test_failed_public_check_never_sends_private_node_path(self):
        prober=Mock();prober.probe.return_value={'ok':False}
        with patch.object(pc.proxy_scan,'Prober',return_value=prober),self.assertRaises(control.Problem):
            pc.verify(self.row,self.config)
        self.assertEqual(prober.probe.call_count,1)

    def test_existing_official_ip_validation_is_unchanged(self):
        with patch.object(control,'BASE',Path(__file__).resolve().parents[1]),self.assertRaises(control.Problem):
            control.candidate('39.105.108.60')

    def test_auto_uses_only_fresh_snapshot_and_same_two_round_update(self):
        self.settings.runtime=self.config.output
        payload=json.loads((self.config.output/'best-proxy-ips.json').read_text())
        scan=SimpleNamespace(control_env={'CF_PROXY_DNS_AUTO_ENABLED':'1'})
        c,probe=Client(),Mock(return_value=20)
        with patch.object(control,'Settings',return_value=self.settings):
            answer=pc.auto_after_scan(scan,payload,c,probe,lambda _:None,contextlib.nullcontext)
        self.assertIn('已切换',answer);self.assertEqual(probe.call_count,2)
        self.assertEqual(len(c.writes),1)
        c=Client()
        with patch.object(control,'Settings',return_value=self.settings):
            answer=pc.auto_after_scan(scan,dict(payload,generated_at='old'),c,probe,lock=contextlib.nullcontext)
        self.assertIn('快照已变化',answer);self.assertFalse(c.writes)

    def test_auto_failure_or_disabled_never_writes(self):
        self.settings.runtime=self.config.output
        payload=json.loads((self.config.output/'best-proxy-ips.json').read_text())
        c=Client();scan=SimpleNamespace(control_env={'CF_PROXY_DNS_AUTO_ENABLED':'1'})
        with patch.object(control,'Settings',return_value=self.settings):
            answer=pc.auto_after_scan(scan,payload,c,Mock(side_effect=control.Problem('probe failed')),
                                      lambda _:None,contextlib.nullcontext)
        self.assertIn('probe failed',answer);self.assertFalse(c.writes)
        scan.control_env={}
        with patch.object(control,'Settings') as create:
            self.assertIn('未启用',pc.auto_after_scan(scan,payload,c))
            create.assert_not_called()

    def test_inherited_lock_is_reused_without_unlocking_parent(self):
        import stat
        info=SimpleNamespace(st_mode=stat.S_IFREG,st_dev=1,st_ino=2)
        fake=SimpleNamespace(flock=Mock(),LOCK_EX=2,LOCK_NB=4)
        with patch.object(pc.os,'fstat',return_value=info),patch.object(pc.os,'stat',return_value=info), \
                patch.object(control,'fcntl',fake):
            with pc.scan_update_lock():pass
        fake.flock.assert_called_once_with(9,6)


class ProxyPortConfigurationTests(unittest.TestCase):
    def test_explicit_ports_select_separate_names_without_rewriting_environment(self):
        env=dict(CF_PROXY_DNS_NAME='tj.example.com',CF_PROXY_DNS_PORT='443',CF_PROXY_DNS_8443_NAME='nj.example.com')
        settings=SimpleNamespace(proxy_control_env=env,dns=('token','zone','bj.example.com'))
        original=dict(env)
        def dns(values):return 'token','zone',values['CF_DNS_NAME']
        with patch.object(pc.dns_update,'configuration',side_effect=dns),patch.object(pc.proxy_scan,'Settings',return_value='config'):
            self.assertEqual(pc.configuration(settings,443)[:2],(('token','zone','tj.example.com'),443))
            self.assertEqual(pc.configuration(settings,8443)[:2],(('token','zone','nj.example.com'),8443))
            del env['CF_PROXY_DNS_8443_NAME']
            with self.assertRaises(control.Problem):pc.configuration(settings,8443)
            env['CF_PROXY_DNS_8443_NAME']='tj.example.com'
            with self.assertRaises(control.Problem):pc.configuration(settings,8443)
        self.assertEqual(original['CF_PROXY_DNS_NAME'],env['CF_PROXY_DNS_NAME'])


if __name__ == '__main__':
    unittest.main()
