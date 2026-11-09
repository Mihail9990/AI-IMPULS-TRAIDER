import copy
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
import paper_https_setup as p

class Fake:

    def __init__(self):
        self.snapshot = dict(services=[], listeners=[], firewall=[])
        self.calls = []
        self.fail = None

    def inventory(self):
        return copy.deepcopy(self.snapshot)

    def admin(self):
        pass

    def protect(self, path):
        pass

    def fetch(self):
        return b'pinned'

    def run(self, args):
        self.calls.append(('adapt', args))

    def sc(self, action, name, *args):
        assert name == p.SERVICE
        self.calls.append((action, name))
        if action == 'create': self.create_args = args
        if action == 'config': self.config_args = args
        if self.fail == action:
            raise TimeoutError('secret must not appear')
        if action == 'create':
            self.snapshot['services'] = [dict(Name=name, PathName=args[-1], StartName='LocalSystem', StartMode='Manual', State='Stopped', ProcessId=0, Dependencies=[], Dependents=[])]
        if action == 'config':
            self.snapshot['services'][0]['StartMode'] = {'auto': 'Auto', 'disabled': 'Disabled'}[args[-1]]
        if action == 'start':
            s = self.snapshot['services'][0]
            s.update(State='Running', ProcessId=5, ProcessPath=p.shared.arguments(s['PathName'])[0], ProcessCommand=s['PathName'])
        if action == 'stop':
            self.snapshot['services'][0]['State'] = 'Stopped'
        if action == 'delete':
            self.snapshot['services'] = []

    def wait(self, *args):
        pass

    def firewall(self, ports, exe):
        self.calls.append(('firewall', ports))
        self.snapshot['firewall'] = [p.RULE]

    def validate_firewall(self, ports, exe):
        if self.fail == 'firewall_identity':
            raise p.shared.HttpsError('changed')

    def remove_firewall(self):
        self.calls.append(('remove_firewall',))
        self.snapshot['firewall'] = []

    def verify_certificate(self, origin, instance_id):
        self.calls.append(('certificate', origin))
        if self.fail == 'tls':
            raise p.ssl.SSLCertVerificationError()

def build_setup(tmp_path):
    (tmp_path / 'data').mkdir()
    p.shared.save(tmp_path / 'application.json', dict(format=1, role='paper'))
    p.shared.save(tmp_path / 'managed-install.json', dict(format=2, role='paper', layout='independent_roots_v1'))
    p.shared.save(tmp_path / 'data/service.json', dict(backend='paper_mt5_quotes_v1', enable_trading=False, web=dict(host='127.0.0.1', port=8766, enabled=True, password='x' * 32)))
    return p.Setup(tmp_path, Fake())

def plan(s):
    return s.prepare('8.8.8.8')

def test_install_and_rollback_are_paper_only(setup):
    seen = []
    result = setup.apply(plan(setup), lambda x: seen.append(x) or True)
    assert 'ещё не подтверждён' in result
    assert len(seen) == 5 and 'Let’s Encrypt' in seen[-1]
    assert ('firewall', [443]) in setup.adapter.calls
    tx = p.shared.read(setup.journal)
    assert tx['status'] == 'INSTALLED_UNVERIFIED' and 'password' not in tx
    setup.rollback(lambda x: True)
    assert p.shared.read(setup.journal)['status'] == 'ROLLED_BACK'
    setup.recover()
    assert not setup.home.exists()

def test_ports_fail_closed(setup, port):
    setup.adapter.snapshot['listeners'] = [dict(port=port, pid=3, address='0.0.0.0')]
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        plan(setup)
    assert not setup.adapter.calls

def test_services_fail_closed(setup, name):
    setup.adapter.snapshot['services'] = [dict(Name=name)]
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        plan(setup)

def test_bad_origins(address):
    with unittest.TestCase().assertRaises((p.shared.HttpsError, UnicodeError)):
        p.public_origin(address)

def test_domain_caddy():
    assert p.public_origin('EXAMPLE.com') == 'https://example.com'
    text = p.caddyfile('https://example.com', Path('certs'))
    assert 'protocols h1 h2' in text and 'h3' not in text
    assert 'disable_http_challenge' in text and '127.0.0.1:8766' in text and ('admin off' in text)
    assert 'disable_http_challenge' not in p.caddyfile('https://example.com', Path('certs'), True)

def test_wrong_role(setup):
    p.shared.save(setup.root / 'application.json', dict(format=1, role='live'))
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        plan(setup)

def test_refusal_and_safe_recovery(setup):
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        setup.apply(plan(setup), lambda action: False)
    assert not any((x[0] in ('create', 'firewall', 'start') for x in setup.adapter.calls))
    setup.recover()
    assert not setup.home.exists()

def test_unknown_command_outcome_blocks_retry_and_rollback(setup):
    setup.adapter.fail = 'create'
    with unittest.TestCase().assertRaises(p.shared.HttpsError) as e:
        setup.apply(plan(setup), lambda action: True)
    assert 'secret' not in str(e.exception)
    assert p.shared.read(setup.journal)['pending']
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        plan(setup)
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        setup.rollback(lambda action: True)
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        setup.recover()

def test_inventory_changes_while_confirming(setup):

    def confirm(action):
        if action.startswith('Создать'):
            setup.adapter.snapshot['services'] = [dict(Name='AIImpulsHTTPS')]
        return True
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        setup.apply(plan(setup), confirm)
    assert not any((x[0] == 'create' for x in setup.adapter.calls))

def test_second_process_lock(setup):
    from independent_install import IndependentManager
    with IndependentManager(setup.root, 'paper').maintenance_guard():
        with unittest.TestCase().assertRaises(Exception):
            setup.apply(plan(setup), lambda action: True)
    assert not setup.adapter.calls

def test_double_click(setup):
    setup.guard.acquire()
    try:
        with unittest.TestCase().assertRaises(p.shared.HttpsError):
            setup.apply(plan(setup), lambda action: True)
    finally:
        setup.guard.release()
    assert not setup.adapter.calls

def test_modified_firewall_not_deleted(setup):
    setup.apply(plan(setup), lambda action: True)
    setup.adapter.fail = 'firewall_identity'
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        setup.rollback(lambda action: True)
    assert ('remove_firewall',) not in setup.adapter.calls

def test_verify_needs_backend_and_own_public_listener(setup):
    setup.apply(plan(setup), lambda action: True)
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        setup.verify()
    setup.adapter.snapshot['listeners'] = [dict(port=443, pid=5, address='0.0.0.0'), dict(port=8766, pid=9, address='127.0.0.1')]
    assert 'страница PAPER' in setup.verify()
    setup.adapter.fail = 'tls'
    with unittest.TestCase().assertRaises(p.ssl.SSLCertVerificationError):
        setup.verify()

def test_backend_never_public(setup):
    setup.apply(plan(setup), lambda action: True)
    setup.adapter.snapshot['listeners'] = [dict(port=443, pid=5, address='0.0.0.0'), dict(port=8766, pid=9, address='0.0.0.0')]
    with unittest.TestCase().assertRaises(p.shared.HttpsError):
        setup.verify()

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.setup = build_setup(Path(self.temp.name))
        patcher = patch.object(p.shared, "validate_archive", return_value=b"pinned-exe")
        patcher.start()
        self.addCleanup(patcher.stop)
    def test_install_and_rollback_are_paper_only(self):
        test_install_and_rollback_are_paper_only(self.setup)
    def test_ports_fail_closed_0(self):
        test_ports_fail_closed(self.setup, 80)
    def test_ports_fail_closed_1(self):
        test_ports_fail_closed(self.setup, 443)
    def test_ports_fail_closed_2(self):
        test_ports_fail_closed(self.setup, 8766)
    def test_services_fail_closed_0(self):
        test_services_fail_closed(self.setup, 'AIImpulsHTTPS')
    def test_services_fail_closed_1(self):
        test_services_fail_closed(self.setup, 'unknown')
    def test_services_fail_closed_2(self):
        test_services_fail_closed(self.setup, 'AIImpulsPaperHTTPS')
    def test_bad_origins_0(self):
        test_bad_origins('localhost')
    def test_bad_origins_1(self):
        test_bad_origins('127.0.0.1')
    def test_bad_origins_2(self):
        test_bad_origins('10.0.0.1')
    def test_bad_origins_3(self):
        test_bad_origins('example.com:443')
    def test_bad_origins_4(self):
        test_bad_origins('https://example.com')
    def test_bad_origins_5(self):
        test_bad_origins('example.com/path')
    def test_bad_origins_6(self):
        test_bad_origins('a\nfoo.com')
    def test_domain_caddy(self):
        test_domain_caddy()
    def test_wrong_role(self):
        test_wrong_role(self.setup)
    def test_refusal_and_safe_recovery(self):
        test_refusal_and_safe_recovery(self.setup)
    def test_unknown_command_outcome_blocks_retry_and_rollback(self):
        test_unknown_command_outcome_blocks_retry_and_rollback(self.setup)
    def test_inventory_changes_while_confirming(self):
        test_inventory_changes_while_confirming(self.setup)
    def test_second_process_lock(self):
        test_second_process_lock(self.setup)
    def test_double_click(self):
        test_double_click(self.setup)
    def test_modified_firewall_not_deleted(self):
        test_modified_firewall_not_deleted(self.setup)
    def test_verify_needs_backend_and_own_public_listener(self):
        test_verify_needs_backend_and_own_public_listener(self.setup)
    def test_backend_never_public(self):
        test_backend_never_public(self.setup)

    def test_acl_failure_retains_recoverable_identity(self):
        def denied(path):
            raise PermissionError('private password')
        self.setup.adapter.protect = denied
        with self.assertRaises(p.shared.HttpsError) as error:
            self.setup.apply(plan(self.setup), lambda action: True)
        self.assertNotIn('private password', str(error.exception))
        self.assertEqual(p.shared.read(self.setup.journal)['status'], 'REVIEW_REQUIRED')
        self.setup.recover()
        self.assertFalse(self.setup.home.exists())

    def test_verify_rejects_changed_config(self):
        self.setup.apply(plan(self.setup), lambda action: True)
        cfg = p.shared.read(self.setup.config)
        cfg['web']['password'] = 'changed' * 10
        p.shared.save(self.setup.config, cfg)
        with self.assertRaises(p.shared.HttpsError):
            self.setup.verify()

    def test_rollback_rechecks_service_after_modal(self):
        self.setup.apply(plan(self.setup), lambda action: True)
        def confirm(action):
            if action.startswith('Остановить'):
                self.setup.adapter.snapshot['services'][0]['PathName'] = 'foreign.exe'
            return True
        with self.assertRaises(p.shared.HttpsError):
            self.setup.rollback(confirm)
        self.assertNotIn(('stop', p.SERVICE), self.setup.adapter.calls)

    def test_cancel_before_autostart_never_configures_auto(self):
        def confirm(action):
            return not action.startswith('Включить автозапуск')
        with self.assertRaises(p.shared.HttpsError):
            self.setup.apply(plan(self.setup), confirm)
        self.assertEqual(self.setup.adapter.create_args[1], 'demand')
        self.assertNotIn(('config', p.SERVICE), self.setup.adapter.calls)
        self.assertNotIn(('start', p.SERVICE), self.setup.adapter.calls)
        self.assertIsNone(p.shared.read(self.setup.journal)['pending'])
        self.setup.rollback(lambda action: True)

    def test_delete_timeout_leaves_service_disabled_and_unknown(self):
        self.setup.apply(plan(self.setup), lambda action: True)
        self.setup.adapter.fail = 'delete'
        with self.assertRaises(TimeoutError):
            self.setup.rollback(lambda action: True)
        own = self.setup.adapter.snapshot['services'][0]
        self.assertEqual(own['StartMode'], 'Disabled')
        self.assertEqual(own['State'], 'Stopped')
        self.assertTrue(p.shared.read(self.setup.journal)['pending'].startswith('Удалить'))
        with self.assertRaises(p.shared.HttpsError):
            self.setup.rollback(lambda action: True)
        with self.assertRaises(p.shared.HttpsError):
            self.setup.recover()

    def test_disable_modal_rechecks_owner(self):
        self.setup.apply(plan(self.setup), lambda action: True)
        before = len(self.setup.adapter.calls)
        def confirm(action):
            if action.startswith('Отключить автозапуск'):
                self.setup.adapter.snapshot['services'][0]['PathName'] = 'foreign.exe'
            return True
        with self.assertRaises(p.shared.HttpsError):
            self.setup.rollback(confirm)
        self.assertEqual(len(self.setup.adapter.calls), before)

class LauncherIdentityTests(unittest.TestCase):
    def test_expected_sid_is_validated_and_forwarded_across_uac(self):
        script = (Path(p.__file__).parent / 'setup_paper_phone_https.ps1').read_text()
        self.assertIn("[string]$ExpectedSid = ''", script)
        self.assertIn('$currentSid = $identity.User.Value', script)
        self.assertIn("$ExpectedSid -notmatch '^S-1-[0-9]+(-[0-9]+)+$'", script)
        self.assertIn("'-ExpectedSid', $ExpectedSid", script)
        self.assertIn("'-Root',", script)

    def test_mismatched_sid_exits_before_elevation_or_python(self):
        script = (Path(p.__file__).parent / 'setup_paper_phone_https.ps1').read_text()
        mismatch = script.index('if ($currentSid -ne $ExpectedSid)')
        exit_at = script.index('exit 1', mismatch)
        self.assertLess(exit_at, script.index('Start-Process'))
        self.assertLess(exit_at, script.index('& py -3.13'))
        self.assertIn('SAME Windows user', script[mismatch:exit_at])
        self.assertIn('Nothing was changed', script[mismatch:exit_at])

if __name__ == "__main__":
    unittest.main()
