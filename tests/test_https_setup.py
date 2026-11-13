import copy
import io
import json
import tempfile
import struct
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import https_setup as h


def old(state='Running'):
    exe=r'C:\Users\Admin\XAUUSDs_Strategy_Analyzer\https_ip\caddy.exe'
    config=r'C:\Users\Admin\XAUUSDs_Strategy_Analyzer\https_ip\Caddyfile'
    cmd=f'"{exe}" run --config "{config}" --adapter caddyfile'
    return dict(Name='OldAnalyzerCaddy',PathName=cmd,StartName='LocalSystem',StartMode='Auto',State=state,
                ProcessId=42 if state=='Running' else 0,ProcessPath=exe if state=='Running' else '',
                ProcessCommand=cmd if state=='Running' else '',Dependencies=[],Dependents=[])

class Fake:
    def __init__(self,legacy=None):
        self.services=[legacy] if legacy else []; self.listeners=[]; self.calls=[]; self.fail=None
        if legacy and legacy['State']=='Running': self.listeners=[dict(port=443,pid=42,address='::')]
        self.archive=io.BytesIO()
        binary=bytearray(256);binary[:2]=b'MZ';struct.pack_into('<I',binary,0x3c,64);binary[64:68]=b'PE\0\0'
        struct.pack_into('<H',binary,68,0x8664);struct.pack_into('<H',binary,84,2);struct.pack_into('<H',binary,88,0x20b)
        with zipfile.ZipFile(self.archive,'w') as z:z.writestr('caddy.exe',bytes(binary))
    def inventory(self):return copy.deepcopy(dict(services=self.services,listeners=self.listeners))
    def admin(self): self.calls.append(('admin',))
    def protect(self,p): self.calls.append(('protect',))
    def fetch(self):return self.archive.getvalue()
    def run(self,args):
        self.calls.append(tuple(args))
        if self.fail=='adapt' and 'adapt' in args:raise h.HttpsError('adapt failed')
        return b''
    def sc(self,*args):
        self.calls.append(args)
        if self.fail==args[0]:raise h.HttpsError('injected failure')
        action,name=args[:2]; svc=next((s for s in self.services if s['Name']==name),None)
        if action=='create':
            self.services.append(dict(Name=name,PathName=args[-1],State='Stopped',StartName='LocalSystem',
                                     StartMode='Auto',ProcessId=0,Dependencies=[],Dependents=[],ProcessCommand='',ProcessPath=''))
        if action=='stop':
            self.listeners=[x for x in self.listeners if x['pid']!=svc['ProcessId']]; svc.update(State='Stopped',ProcessId=0,ProcessPath='',ProcessCommand='')
        if action=='start':
            svc.update(State='Running',ProcessId=90,ProcessCommand=svc['PathName'],ProcessPath=h.arguments(svc['PathName'])[0]); self.listeners.append(dict(port=443,pid=90,address='::'))
        if action=='config':svc['StartMode']={'disabled':'Disabled','auto':'Auto','demand':'Manual'}[args[-1]]
        if action=='delete':self.services.remove(svc)
    def wait(self,name,state):
        svc=next((s for s in self.services if s['Name']==name),None)
        assert state=='Absent' and svc is None or svc and svc['State']==state
    def backup_service(self,svc,folder):pass

class HTTPS(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        h.save(self.root/'managed-install.json',{'format':2,'role':'live','layout':'independent_roots_v1'})
        h.save(self.root/'application.json',{'format':1,'role':'live'})
        (self.root/'data').mkdir(); self.config=self.root/'data'/'service.json'
        self.cfg={'enable_trading':False,'web':{'host':'127.0.0.1','port':8765,'password':'x'*33},'untouched':{'custom_extension':123}}
        self.config.write_text(json.dumps(self.cfg)); self.adapter=Fake(); self.setup=h.Setup(self.root,self.adapter)
        self.env=patch.dict('os.environ',{},clear=True); self.env.start(); self.addCleanup(self.env.stop)
    def plan(self): return self.setup.prepare('130.94.67.32')
    def apply(self,plan):
        with patch.object(h,'ARCHIVE_SHA256',h.digest(self.adapter.fetch())):return self.setup.apply(plan,confirmed=True)
    def test_origin(self):
        self.assertEqual(h.origin('130.94.67.32'),'https://130.94.67.32')
        self.assertEqual(h.origin('2606:4700:4700::1111'),'https://[2606:4700:4700::1111]')
        for s in ('127.0.0.1','::1','http://1.1.1.1','1.1.1.1:443','1.1.1.1\n{','192.168.1.1'):
            with self.subTest(s=s),self.assertRaises(h.HttpsError):h.origin(s)
    def test_prepare_no_mutation_and_tls(self):
        before=self.config.read_bytes(); plan=self.plan()
        self.assertEqual(self.adapter.calls,[]);self.assertEqual(self.config.read_bytes(),before)
        self.assertIn('profile shortlived',plan['caddyfile']);self.assertNotIn('internal',plan['caddyfile'])
        self.assertIn('header_up Host 127.0.0.1:8765',plan['caddyfile']);self.assertNotIn('Origin',plan['caddyfile'])
    def test_short_password_reject(self):
        self.cfg['web']['password']='short';self.config.write_text(json.dumps(self.cfg))
        with self.assertRaises(h.HttpsError):self.plan()
    def test_environment_empty_rejects(self):
        with patch.dict('os.environ',{'TRADFI_WEB_PASSWORD':''}),self.assertRaises(h.HttpsError):self.plan()
    def test_live_enabled_rejects(self):
        self.cfg['enable_trading']=True;self.config.write_text(json.dumps(self.cfg))
        with self.assertRaises(h.HttpsError):self.plan()
    def test_public_backend_rejects(self):
        self.cfg['web']['host']='0.0.0.0';self.config.write_text(json.dumps(self.cfg))
        with self.assertRaises(h.HttpsError):self.plan()
    def test_wrong_port_rejects(self):
        self.cfg['web']['port']=9000;self.config.write_text(json.dumps(self.cfg))
        with self.assertRaises(h.HttpsError):self.plan()
    def test_unknown_listener_rejects(self):
        for port in (80,443,2020):
            self.adapter.listeners=[dict(port=port,pid=777,address='::')]
            with self.assertRaises(h.HttpsError):self.plan()
        self.assertFalse(self.adapter.calls)
    def test_backend_must_be_stopped(self):
        self.adapter.listeners=[dict(port=8765,pid=777,address='127.0.0.1')];p=self.plan()
        with self.assertRaises(h.HttpsError):self.apply(p)
        self.assertFalse(self.setup.home.exists())
    def test_confirmation_required(self):
        with self.assertRaises(h.HttpsError):self.setup.apply(self.plan())
        self.assertFalse(self.adapter.calls)
    def test_plan_stale_port(self):
        p=self.plan();self.adapter.listeners=[dict(port=443,pid=3,address='::')]
        with self.assertRaises(h.HttpsError):self.apply(p)
        self.assertFalse(self.setup.home.exists())
    def test_plan_stale_config(self):
        p=self.plan();self.config.write_text('{}')
        with self.assertRaises(h.HttpsError):self.apply(p)
    def test_plan_rechecked_after_download(self):
        p=self.plan(); data=self.adapter.fetch()
        def fetch():self.adapter.listeners=[dict(port=443,pid=9,address='::')];return data
        with patch.object(self.adapter,'fetch',fetch),self.assertRaises(h.HttpsError):self.apply(p)
        self.assertFalse(self.setup.home.exists())
    def test_checksum_failure(self):
        with self.assertRaises(h.HttpsError):self.setup.apply(self.plan(),confirmed=True)
        self.assertFalse(self.setup.home.exists())
    def test_apply_preserves_settings_no_trading(self):
        before=self.config.read_bytes();self.apply(self.plan());after=h.read(self.config)
        self.assertEqual(after['untouched'],self.cfg['untouched']);self.assertFalse(after['enable_trading'])
        self.assertEqual(after['web']['public_origin'],'https://130.94.67.32')
        self.assertEqual((self.setup.home/'backup'/'service.original.json').read_bytes(),before)
        self.assertEqual(h.read(self.setup.journal)['stage'],'installed')
        self.assertFalse(any('tradfi_main' in ' '.join(c) or '--execute' in c for c in self.adapter.calls))
        self.assertNotIn('x'*33,json.dumps(h.read(self.setup.journal)))
    def test_double_apply_restart_refuses(self):
        p=self.plan();self.apply(p)
        with self.assertRaises(h.HttpsError):self.apply(p)
        with self.assertRaises(h.HttpsError):h.Setup(self.root,self.adapter).prepare('130.94.67.32')
    def test_rollback_new_only(self):
        before=self.config.read_bytes();self.apply(self.plan());self.setup.rollback(True)
        self.assertEqual(self.config.read_bytes(),before);self.assertEqual(self.adapter.services[0]['StartMode'],'Disabled')
        self.assertTrue((self.setup.home/'caddy.exe').exists())
    def test_rollback_rejects_changed_settings(self):
        self.apply(self.plan());self.config.write_text('{}')
        with self.assertRaises(h.HttpsError):self.setup.rollback(True)
        self.assertEqual(self.adapter.services[0]['State'],'Running')
    def test_adapt_failure_has_recoverable_journal(self):
        self.adapter.fail='adapt'
        with self.assertRaises(h.HttpsError):self.apply(self.plan())
        self.assertEqual(h.read(self.setup.journal)['stage'],'prepared')
        self.setup.rollback(True)
    def test_create_failure_recoverable(self):
        self.adapter.fail='create'
        with self.assertRaises(h.HttpsError):self.apply(self.plan())
        self.setup.rollback(True);self.assertEqual(h.read(self.config),self.cfg)
    def test_old_identity_reject_foreign(self):
        for replacement in ('AnotherApp','XAUUSDs_Strategy_Analyzer_backup'):
            s=old();s['PathName']=s['PathName'].replace('XAUUSDs_Strategy_Analyzer',replacement)
            with self.assertRaises(h.HttpsError):h.old_identity(s)
        for key,value in [('StartName','Admin'),('Dependencies',['other']),('Name',h.SERVICE),('PathName','nssm.exe'),('State','Start Pending')]:
            s=old();s[key]=value
            with self.assertRaises(h.HttpsError):h.old_identity(s)
    def test_service_process_mismatch(self):
        s=old();s['ProcessPath']='C:\\other.exe'
        with self.assertRaises(h.HttpsError):h.verify_service_process(s)
    def test_multiple_old_services_reject(self):
        a=old();b=old();b['Name']='AnotherOld'
        with self.assertRaises(h.HttpsError):h.select_old({'services':[a,b]})
    def test_existing_new_service_reject(self):
        s=old('Stopped');s['Name']=h.SERVICE;self.adapter.services=[s]
        with self.assertRaises(h.HttpsError):self.plan()
    def test_no_old_removal_does_nothing(self):
        with self.assertRaises(h.HttpsError):h.OldRemoval(self.root,self.adapter).prepare()
        self.assertFalse(self.adapter.calls)
    def test_removal_confirmation(self):
        self.adapter=Fake(old());r=h.OldRemoval(self.root,self.adapter)
        with self.assertRaises(h.HttpsError):r.remove(r.prepare())
        self.assertFalse(self.adapter.calls)
    def test_removal_changed_process_blocks(self):
        self.adapter=Fake(old());r=h.OldRemoval(self.root,self.adapter);p=r.prepare()
        self.adapter.services[0]['ProcessId']=81
        with self.assertRaises(h.HttpsError):r.remove(p,True)
        self.assertFalse(any(x[0]=='stop' for x in self.adapter.calls))
    def test_removal_backup_failure_no_stop(self):
        self.adapter=Fake(old());r=h.OldRemoval(self.root,self.adapter)
        with patch.object(h,'backup_old',side_effect=h.HttpsError('backup failed')),self.assertRaises(h.HttpsError):r.remove(r.prepare(),True)
        self.assertFalse(any(x[0]=='stop' for x in self.adapter.calls))
    def test_removal_success_and_repeated(self):
        self.adapter=Fake(old());r=h.OldRemoval(self.root,self.adapter)
        def backup(a,s,p):p.mkdir();(p/'Caddyfile.original').write_bytes(b'original')
        with patch.object(h,'backup_old',backup),patch.object(h.Path,'read_bytes',return_value=b'original'):folder=r.remove(r.prepare(),True)
        self.assertEqual(h.read(folder/'operation.json')['state'],'removed')
        self.assertFalse(self.adapter.services);self.assertFalse(self.adapter.listeners)
        with self.assertRaises(h.HttpsError):r.prepare()
        self.assertFalse(any('taskkill' in ' '.join(x) for x in self.adapter.calls))
    def legacy_fixture(self,state='Running'):
        self.adapter=Fake(old(state));self.setup=h.Setup(self.root,self.adapter)
        original_read=Path.read_bytes; config=h.old_identity(old())[1];exe=h.old_identity(old())[0]
        def read_bytes(p):return b'original' if str(p)==config else original_read(p)
        def backup(a,svc,p):
            p.mkdir();(p/'Caddyfile.original').write_bytes(b'original');h.save(p/'service.json',svc)
            (p/'service-sddl.txt').write_text('D:(A;;FA;;;SY)')
            (p/'service.reg').write_text('Windows Registry Editor Version 5.00\n[HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Services\\'+svc['Name']+']\n',encoding='utf-16')
        p=patch.object(Path,'read_bytes',read_bytes);p.start();self.addCleanup(p.stop)
        p=patch.object(h,'backup_old',backup);p.start();self.addCleanup(p.stop)
        real_isfile=Path.is_file
        p=patch.object(Path,'is_file',lambda p:str(p) in (config,exe) or real_isfile(p));p.start();self.addCleanup(p.stop)
    def test_migration_stop_disable_then_new_service(self):
        self.legacy_fixture();before=self.config.read_bytes();self.apply(self.plan())
        self.assertEqual(self.adapter.services[0]['State'],'Stopped')
        self.assertEqual(self.adapter.services[0]['StartMode'],'Disabled')
        calls=self.adapter.calls
        self.assertLess(calls.index(('stop','OldAnalyzerCaddy')),next(i for i,c in enumerate(calls) if c[0]=='create'))
        self.setup.rollback(True)
        self.assertEqual(self.adapter.services[0]['State'],'Running');self.assertEqual(self.config.read_bytes(),before)
    def test_old_stopped_not_started_by_rollback(self):
        self.legacy_fixture('Stopped');self.apply(self.plan());self.setup.rollback(True)
        self.assertEqual(self.adapter.services[0]['State'],'Stopped')
    def test_migration_stop_rejected_no_new_service(self):
        self.legacy_fixture();self.adapter.fail='stop'
        with self.assertRaises(h.HttpsError):self.apply(self.plan())
        self.assertFalse(any(c[0]=='create' for c in self.adapter.calls))
        self.assertEqual(h.read(self.config),self.cfg)
    def test_migration_new_start_failure_rollback(self):
        self.legacy_fixture();self.adapter.fail='start'
        with self.assertRaises(h.HttpsError):self.apply(self.plan())
        self.adapter.fail=None;self.setup.rollback(True)
        self.assertEqual(self.adapter.services[0]['State'],'Running')
    def test_old_restarted_before_apply_refused(self):
        self.legacy_fixture();p=self.plan();self.adapter.services[0]['ProcessId']=43
        with self.assertRaises(h.HttpsError):self.apply(p)
        self.assertFalse(any(c[0]=='stop' for c in self.adapter.calls))
    def test_removal_delete_failure_then_restore(self):
        self.legacy_fixture();r=h.OldRemoval(self.root/'removal',self.adapter);self.adapter.fail='delete'
        with self.assertRaises(h.HttpsError):r.remove(r.prepare(),True)
        folder=next((self.root/'removal').glob('backup-*'));self.adapter.fail=None
        r.restore(folder,True);self.assertEqual(self.adapter.services[0]['State'],'Running')
        self.assertFalse(any(c[0]=='create' for c in self.adapter.calls))
    def test_removed_service_restored(self):
        self.legacy_fixture();r=h.OldRemoval(self.root/'removal',self.adapter)
        folder=r.remove(r.prepare(),True);r.restore(folder,True)
        self.assertEqual(self.adapter.services[0]['Name'],'OldAnalyzerCaddy')
        self.assertEqual(self.adapter.services[0]['State'],'Running')
        with self.assertRaises(h.HttpsError):r.restore(folder,True)
    def test_restore_rejects_foreign_registry(self):
        self.legacy_fixture();r=h.OldRemoval(self.root/'removal',self.adapter);folder=r.remove(r.prepare(),True)
        (folder/'service.reg').write_text('[HKEY_LOCAL_MACHINE\\SOFTWARE\\Other]\n',encoding='utf-16')
        with self.assertRaises(h.HttpsError):r.restore(folder,True)
        self.assertFalse(any(c[0]=='create' for c in self.adapter.calls))
    def test_other_process_takes_port_after_removal(self):
        self.legacy_fixture();r=h.OldRemoval(self.root/'removal',self.adapter);original=self.adapter.sc
        def sc(*args):
            original(*args)
            if args[0]=='delete':self.adapter.listeners.append(dict(port=443,pid=99,address='::'))
        self.adapter.sc=sc
        with self.assertRaisesRegex(h.HttpsError,'удалена, но порты заняты'):r.remove(r.prepare(),True)
        self.assertEqual(self.adapter.listeners[0]['pid'],99)
    def test_backup_paths_not_logged_or_password_exported(self):
        self.apply(self.plan());tx=h.read(self.setup.journal)
        self.assertNotIn('password',str(tx));self.assertNotIn(self.cfg['web']['password'],str(self.adapter.calls))
    def test_external_password_type_invalid_even_with_env(self):
        self.cfg['web']['password']=123;self.config.write_text(json.dumps(self.cfg))
        with patch.dict('os.environ',{'TRADFI_WEB_PASSWORD':'y'*40}),self.assertRaises(h.HttpsError):self.plan()
    def test_standalone_matches_module(self):
        self.assertEqual(Path(h.__file__).read_bytes(),(Path(h.__file__).parent/'tools'/'old-https-removal'/'https_setup.py').read_bytes())

if __name__=='__main__': unittest.main()
