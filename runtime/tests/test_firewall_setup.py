import base64
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from firewall_setup import configure_firewall, build_script, FirewallSetupError


class FirewallTests(unittest.TestCase):
    def test_approval_required_before_any_effect(self):
        for value in (False, None, 1, 'yes'):
            with self.assertRaises(FirewallSetupError):
                configure_firewall('missing', approved=value, runner=lambda *a: self.fail('executed'))

    def test_platform_gate(self):
        with self.assertRaises(FirewallSetupError):
            configure_firewall('missing', approved=True, platform='posix', runner=lambda *a: self.fail('executed'))

    def test_encoded_scoped_script(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "O'Brien" / 'caddy.exe'
            path.parent.mkdir(); path.write_bytes(b'test only')
            calls=[]
            name = configure_firewall(path, approved=True, platform='nt', runner=lambda *a: calls.append(a) or 0)
            self.assertTrue(name.startswith('MT5Widget-Caddy-HTTPS-'))
            script = base64.b64decode(calls[0][1].split()[-1]).decode('utf-16-le')
            self.assertIn("O''Brien", script)
            self.assertIn('-Direction Inbound -Action Allow', script)
            self.assertIn('-Protocol TCP -LocalPort 443 -Program $program', script)
            self.assertIn('-EdgeTraversalPolicy Block', script)
            for bad in ('LocalPort 80', '8767', '2019', 'Set-NetFirewall', 'Remove-NetFirewall', 'Invoke-Expression'):
                self.assertNotIn(bad, script)

    def test_existing_and_failure_never_success(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'caddy.exe';path.write_bytes(b'test')
            for code in (10,11,12,13,1):
                with self.assertRaises(FirewallSetupError):
                    configure_firewall(path, approved=True, platform='nt', runner=lambda *a: code)

    def test_exact_existing_rule_success_is_explicit_separate_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'caddy.exe';path.write_bytes(b'test')
            logs=[]
            result=configure_firewall(path, approved=True, platform='nt', log=logs.append,
                                      runner=lambda *a: 20)
            self.assertTrue(result.startswith('MT5Widget-Caddy-HTTPS-'))
            self.assertIn('Изменений нет', logs[-1])

    def test_idempotency_guard_is_conservative_and_read_only(self):
        script=build_script('C:/test/caddy.exe')[1]
        before_create=script.split('New-NetFirewallRule')[0]
        self.assertIn('$existing.Count -eq 1 -and (Match-OwnRule', before_create)
        self.assertIn('exit 20', before_create)
        self.assertIn('exit 10', before_create)
        self.assertIn('$null -eq $prop', script)
        self.assertIn('@($prop.Value).Count -ne 1', script)
        for field in ('Direction', 'Action', 'Enabled', 'Profile', 'EdgeTraversalPolicy',
                      'LooseSourceMapping', 'LocalOnlyMapping', 'Protocol', 'LocalPort',
                      'RemotePort', 'IcmpType', 'DynamicTarget', 'Program', 'Package',
                      'LocalAddress', 'RemoteAddress', 'Service', 'InterfaceAlias',
                      'InterfaceType', 'Authentication', 'Encryption', 'OverrideBlockRules',
                      'LocalUser', 'RemoteUser', 'RemoteMachine'):
            self.assertIn(field+'=', before_create)
        self.assertNotIn('Set-NetFirewall', script)
        self.assertNotIn('Remove-NetFirewall', script)

    def test_wrong_missing_relative_path(self):
        for path in ('caddy.exe', '/missing/caddy.exe', __file__):
            with self.assertRaises(FirewallSetupError):
                configure_firewall(path, approved=True, platform='nt', runner=lambda *a: self.fail('executed'))

    def test_rule_identity_deterministic_and_path_specific(self):
        self.assertEqual(build_script('C:/one/caddy.exe')[0], build_script('c:/ONE/caddy.exe')[0])
        self.assertNotEqual(build_script('C:/one/caddy.exe')[0], build_script('C:/two/caddy.exe')[0])

if __name__ == '__main__': unittest.main()
