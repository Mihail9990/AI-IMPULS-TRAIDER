import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import https_setup as h
import https_simple as s
from test_https_setup import Fake

class Adapter(Fake):
    def validate_firewall(self):self.calls.append(('firewall_check',))
    def firewall(self):
        self.calls.append(('firewall',))
        if self.fail=='firewall':raise h.CommandError('powershell.exe',1)

class Simple(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        h.save(self.root/'managed-install.json',{'format':2,'role':'live','layout':'independent_roots_v1'})
        h.save(self.root/'application.json',{'format':1,'role':'live'})
        self.config=self.root/'data/service.json'
        self.config.write_text(json.dumps({'enable_trading':False,'web':{'host':'127.0.0.1','port':8765,'password':'x'*32},'custom_extension':{'preserve':True}}))
        self.a=Adapter();self.i=s.Installer(self.root,self.a)
        self.sha=patch.object(h,'ARCHIVE_SHA256',h.digest(self.a.fetch()));self.sha.start();self.addCleanup(self.sha.stop)
        self.env=patch.dict('os.environ',{},clear=True);self.env.start();self.addCleanup(self.env.stop)
        self.i.s.local_archive=self.a.fetch()
    def install(self):return self.i.install('38.60.203.9',True)
    def test_fresh(self):
        self.install()
        self.assertEqual(sum(c[0]=='create' for c in self.a.calls),1)
        self.assertEqual(self.a.calls[-1],('firewall',))
        self.assertTrue(h.read(self.config)['custom_extension']['preserve'])
        self.assertFalse(h.read(self.config)['enable_trading'])
    def test_repeat_refused_no_duplicate(self):
        self.install()
        with self.assertRaises(h.HttpsError):self.install()
        self.assertEqual(sum(c[0]=='create' for c in self.a.calls),1)
    def test_partial_start_refused(self):
        self.a.fail='start'
        with self.assertRaises(h.AttemptError):self.install()
        self.a.fail=None
        with self.assertRaises(h.HttpsError):self.install()
        self.assertEqual(len(self.a.services),1)
    def test_foreign_service(self):
        self.a.services=[{'Name':'foreign'}]
        with self.assertRaises(h.HttpsError):self.install()
        self.assertFalse(self.i.s.home.exists())
    def test_existing_home(self):
        self.i.s.home.mkdir()
        with self.assertRaises(h.HttpsError):self.install()
    def test_existing_attempt_even_recovered(self):
        h.save(self.i.s.attempt_path,{'status':'RECOVERED'})
        with self.assertRaises(h.HttpsError):self.install()
    def test_firewall_failure_preserves_state(self):
        self.a.fail='firewall'
        with self.assertRaises(h.CommandError):self.install()
        self.assertTrue(self.i.s.journal.exists());self.assertEqual(len(self.a.services),1)
    def test_cancel_no_effects(self):
        before=self.config.read_bytes()
        with self.assertRaises(h.HttpsError):self.i.install('38.60.203.9',False)
        self.assertEqual(self.a.calls,[]);self.assertEqual(self.config.read_bytes(),before)
    def test_no_zip(self):
        self.i.s.local_archive=None
        with self.assertRaises(h.HttpsError):self.install()
    def test_short_password(self):
        cfg=h.read(self.config);cfg['web']['password']='short';self.config.write_text(json.dumps(cfg))
        with self.assertRaises(h.HttpsError):self.install()
    def test_all_conflict_ports(self):
        for port in (80,443,2020,8765):
            self.a.listeners=[dict(port=port,pid=77,address='127.0.0.1')]
            with self.assertRaises(h.HttpsError):self.install()
        self.assertFalse(self.a.services)
    def test_local_zip_no_download(self):
        self.a.fetch=lambda:(_ for _ in ()).throw(AssertionError('no network'))
        self.install()
    def test_bad_ip_no_writes(self):
        with self.assertRaises(h.HttpsError):self.i.install('127.0.0.1',True)
        self.assertFalse(self.i.s.home.exists())
    def test_firewall_source(self):
        self.assertNotIn('Remove-NetFirewallRule',s.FIREWALL)
        self.assertIn("'8765','Block'",s.FIREWALL)
        self.assertIn('Get-NetFirewallApplicationFilter',s.FIREWALL)

    def test_rejects_legacy_or_foreign_installation_without_changes(self):
        before=self.config.read_bytes()
        for marker,application in (({'format':1},{'format':1,'role':'live'}),({'format':2,'role':'other','layout':'independent_roots_v1'},{'format':1,'role':'other'}),({'format':2,'role':'live','layout':'independent_roots_v1'},{'format':1,'role':'other'})):
            h.save(self.root/'managed-install.json',marker);h.save(self.root/'application.json',application)
            with self.assertRaises(h.HttpsError):self.install()
            self.assertFalse(self.i.s.home.exists());self.assertFalse(self.i.s.attempt_path.exists())
            self.assertEqual(self.config.read_bytes(),before);self.assertFalse(self.a.services)

    def test_missing_ownership_marker_rejected(self):
        (self.root/'application.json').unlink()
        with self.assertRaises(h.HttpsError):self.install()
        self.assertFalse(self.a.services);self.assertFalse(self.i.s.home.exists())

    def test_changed_ownership_between_plan_and_apply_rejected_before_writes(self):
        plan=self.i.s.prepare('38.60.203.9');before=self.config.read_bytes()
        h.save(self.root/'application.json',{'format':1,'role':'other'})
        with self.assertRaises(h.HttpsError):self.i.s.apply(plan,True)
        self.assertEqual(self.config.read_bytes(),before);self.assertFalse(self.i.s.attempt_path.exists());self.assertFalse(self.a.services)

    def test_pending_migration_rejected(self):
        h.save(self.root/'migration-pending.json',{'format':1})
        with self.assertRaises(h.HttpsError):self.install()
        self.assertFalse(self.i.s.home.exists())
