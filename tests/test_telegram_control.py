import contextlib
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'linux'))
import telegram_control as bot


def settings():
    return bot.Settings({'CF_TG_CONTROL_ENABLED': '1', 'TELEGRAM_BOT_TOKEN': 'private-test-token',
        'TELEGRAM_CHAT_ID': '12345', 'CFST_IPV6_NODE_HOST': 'node.example.com',
        'CFST_IPV6_NODE_PATH': '/ws', 'CLOUDFLARE_API_TOKEN': 'private-cf-token',
        'CLOUDFLARE_ZONE_ID': 'a' * 32, 'CF_DNS_NAME': 'edge.example.com'})


def record(kind='AAAA', address='2606:4700:ff00::1', ident='1'):
    return {'id': ident * 32, 'type': kind, 'name': 'edge.example.com',
            'content': address, 'ttl': 60, 'proxied': False, 'comment': 'keep me'}


class Client:
    name = 'edge.example.com'

    def __init__(self, rows):
        self.rows = copy.deepcopy(rows)
        self.writes = []

    def records(self):
        return copy.deepcopy(self.rows)

    def request(self, method, suffix, body):
        self.writes.append(copy.deepcopy(body))
        deleted = {row['id'] for row in body.get('deletes', [])}
        self.rows = [row for row in self.rows if row['id'] not in deleted]
        for patchrow in body.get('patches', []):
            next(row for row in self.rows if row['id'] == patchrow['id']).update(patchrow)
        for row in body.get('posts', []):
            self.rows.append(dict(row, id='f' * 32))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.settings = settings()
        source = patch.object(bot, 'BASE', ROOT)
        source.start()
        self.addCleanup(source.stop)

    def switch(self, value, client, **kwargs):
        return bot.switch(value, self.settings, client, probe=kwargs.pop('probe', lambda *args: 80),
                          sleep=lambda seconds: None, lock=contextlib.nullcontext, **kwargs)

    def test_v4_and_v6_switch_families_in_one_batch_preserving_ttl(self):
        for old, new, kind in [(record(), '172.64.229.194', 'A'),
                (record('A', '172.64.229.91'), '2606:4700:ff00::2', 'AAAA')]:
            with self.subTest(kind=kind):
                old['ttl'] = 120
                client = Client([old])
                answer = self.switch(new, client)
                self.assertIn('已切换', answer)
                self.assertEqual(len(client.writes), 1)
                self.assertEqual(client.writes[0]['deletes'], [{'id': old['id']}])
                self.assertEqual([(row['type'], row['content'], row['ttl']) for row in client.rows], [(kind, new, 120)])
                self.assertEqual(client.rows[0]['comment'], 'keep me')

    def test_existing_target_record_is_patched_without_replacing_metadata(self):
        old = record('A', '172.64.229.91')
        client = Client([old, record(ident='2')])
        self.switch('172.64.229.194', client)
        self.assertEqual(client.writes[0]['patches'], [{'id': old['id'], 'content': '172.64.229.194'}])
        self.assertEqual(client.rows[0]['id'], old['id'])
        self.assertEqual(client.rows[0]['comment'], old['comment'])

    def test_two_failed_or_incomplete_probes_never_write(self):
        for failure_on in (1, 2):
            client = Client([record()])
            calls = []
            def probe(*args):
                calls.append(1)
                if len(calls) == failure_on:
                    raise bot.Problem('probe failed')
                return 80
            with self.subTest(failure_on=failure_on), self.assertRaises(bot.Problem):
                self.switch('172.64.229.194', client, probe=probe)
            self.assertFalse(client.writes)
            self.assertEqual(client.rows, [record()])

    def test_same_address_is_rechecked_but_not_written(self):
        client = Client([record()])
        probe = MagicMock(return_value=80)
        self.switch('2606:4700:ff00::1', client, probe=probe)
        self.assertEqual(probe.call_count, 2)
        self.assertFalse(client.writes)

    def test_concurrent_edit_is_not_overwritten(self):
        client = Client([record()])
        with patch.object(client, 'records', side_effect=[[record()], [record(address='2606:4700:ff00::9')]]):
            answer = self.switch('172.64.229.194', client)
        self.assertIn('DNS 已变化', answer)
        self.assertFalse(client.writes)

    def test_uncertain_write_is_not_retried_or_exposed(self):
        client = Client([record()])
        with patch.object(client, 'request', side_effect=bot.dns_update.DNSProblem('private-token-secret')) as request:
            answer = self.switch('172.64.229.194', client)
        request.assert_called_once()
        self.assertIn('结果未确认', answer)
        self.assertNotIn('secret', answer)

    def test_readback_mismatch_never_claims_success(self):
        client = Client([record()])
        with patch.object(client, 'records', side_effect=[[record()], [record()], [record()]]):
            answer = self.switch('172.64.229.194', client)
        self.assertIn('结果未确认', answer)
        self.assertEqual(len(client.writes), 1)

    def test_ip_validation_rejects_private_noncf_and_shell_payloads(self):
        for value in ('127.0.0.1', '192.168.1.1', '::1', '1.1.1.1', '2606:4700::1%eth0',
                      '172.64.229.1;reboot', '2606:4700::1:443/path', 'example.com'):
            with self.subTest(value=value), self.assertRaises(bot.Problem):
                bot.candidate(value)

    def test_auth_rejects_others_groups_forwarding_and_old_messages(self):
        now = int(time.time())
        update = {'message': {'chat': {'type': 'private', 'id': 12345},
                  'from': {'id': 12345, 'is_bot': False}, 'date': now, 'text': '/dns'}}
        self.assertTrue(bot.authorized(update, self.settings, now))
        variants = []
        for path, value in [('chat', {'type': 'group', 'id': 12345}), ('from', {'id': 55, 'is_bot': False}),
                ('date', now - 300), ('forward_origin', {}), ('via_bot', {}), ('text', 'x' * 513)]:
            item = copy.deepcopy(update); item['message'][path] = value; variants.append(item)
        for variant in variants:
            self.assertFalse(bot.authorized(variant, self.settings, now))

    def test_bare_ips_and_set_command_use_same_switch(self):
        client = Client([record()])
        with patch.object(bot, 'switch', return_value='ok') as switch:
            self.assertEqual(bot.handle('172.64.229.194', self.settings, client), 'ok')
            self.assertEqual(bot.handle('/set 2606:4700:ff00::1', self.settings, client), 'ok')
        self.assertEqual(switch.call_count, 2)

    def test_use_command_rejects_wrong_family_and_missing_ram_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.settings.runtime = Path(tmp)
            self.assertIn('没有', bot.handle('/use6', self.settings, Client([])))
            (Path(tmp) / 'best-ips-v6.txt').write_text('172.64.229.1\n')
            with self.assertRaises(bot.Problem): bot.handle('/use6', self.settings, Client([]))

    def test_dns_reader_rejects_cname_proxy_wrong_name_and_pagination(self):
        for rows, info in [([dict(record(), type='CNAME')], {}), ([dict(record(), proxied=True)], {}),
                ([dict(record(), name='other.example.com')], {}), ([record()], {'total_pages': 2})]:
            client = bot.ControlDNS(*self.settings.dns)
            with patch.object(client, 'request', return_value={'result': rows, 'result_info': info}), self.assertRaises(bot.Problem):
                client.records()

    def test_readonly_probe_binds_physical_device_and_uses_ip_literal(self):
        raw, connection = MagicMock(), MagicMock()
        socketmock = MagicMock();socketmock.return_value.__enter__.return_value = raw
        tls = MagicMock();tls.wrap_socket.return_value.__enter__.return_value = connection
        connection.recv.side_effect = [b'HTTP/1.1 200 OK\r\n\r\ncolo=NRT\n', b'']
        # Windows lacks this Linux socket constant; emulate it for this test.
        with patch.object(bot.socket, 'socket', socketmock), patch.object(bot.socket, 'SO_BINDTODEVICE', 25, create=True), patch.object(bot.ssl, 'create_default_context', return_value=tls):
            ok, colo, delay = bot.https_probe(bot.candidate('172.64.229.1'), 'www.cloudflare.com', 443, '/cdn-cgi/trace', 'eth0')
        self.assertTrue(ok);self.assertEqual(colo, 'NRT')
        raw.setsockopt.assert_called_once_with(bot.socket.SOL_SOCKET, 25, b'eth0\0')
        raw.connect.assert_called_once_with(('172.64.229.1', 443))

    def test_restart_discards_old_updates_and_processes_only_new_commands(self):
        telegram = MagicMock()
        current = int(time.time())
        fresh = {'update_id': 101, 'message': {'chat': {'type': 'private', 'id': 12345},
            'from': {'id': 12345, 'is_bot': False}, 'date': current, 'text': '/dns'}}
        telegram.call.side_effect = [{'username': 'test_bot'}, {'url': ''},
                                    [{'update_id': 100, 'message': {'text': 'old'}}], [fresh], KeyboardInterrupt()]
        with patch.object(bot, 'handle', return_value='reply') as handle, patch.object(bot, 'ControlDNS'), self.assertRaises(KeyboardInterrupt):
            bot.serve(self.settings, telegram)
        handle.assert_called_once()
        telegram.send.assert_called_once_with('reply')

    def test_check_mode_only_reads_dns_and_probes_without_listening_or_sending(self):
        client, telegram = Client([record()]), MagicMock()
        telegram.call.side_effect = [{'username': 'test_bot'}, {'url': ''}]
        with patch.object(bot, 'Settings', return_value=self.settings), patch.object(bot, 'ControlDNS', return_value=client), patch.object(bot, 'Telegram', return_value=telegram), patch.object(bot, 'verify', return_value=80), patch.object(bot, 'serve') as serve, patch.object(sys, 'argv', ['bot', '--check']), patch('builtins.print'):
            bot.main()
        serve.assert_not_called()
        telegram.send.assert_not_called()
        self.assertFalse(client.writes)

    def test_announce_preserves_all_existing_command_menus(self):
        telegram = MagicMock()
        with patch.object(bot, 'Settings', return_value=self.settings), patch.object(bot, 'Telegram', return_value=telegram), patch.object(sys, 'argv', ['bot', '--announce']):
            bot.main()
        telegram.call.assert_not_called()
        telegram.send.assert_called_once()


if __name__ == '__main__':
    unittest.main()
