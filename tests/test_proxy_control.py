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


if __name__ == '__main__':
    unittest.main()
