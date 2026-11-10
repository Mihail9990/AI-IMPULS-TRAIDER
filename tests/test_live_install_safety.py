"""LIVE-only installation safety regressions. All processes/network are mocked."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import independent_install as independent
import install_tradfi as legacy
from tradfi import managed
try:
    from tests import test_independent_installations as fixtures
except ImportError:
    import test_independent_installations as fixtures

A=fixtures.A
B=fixtures.B

class LiveInstallSafetyTest(unittest.TestCase):
    def setUp(self):
        fixtures.IndependentInstallationsTest.setUp(self)
        for target in ('urllib.request.urlopen','subprocess.run','subprocess.Popen'):
            blocker=patch(target,side_effect=AssertionError('External work forbidden in offline safety tests'))
            blocker.start();self.addCleanup(blocker.stop)

    def install(self):
        self.manager.install()
        config=self.manager.config();config['web']['password']='local-test-password'
        legacy.atomic_json(self.manager.config_path,config)

    def state(self):
        path=legacy.state_path(self.root,self.manager.config())
        snapshot=dict(schema=1,backend='mt5_direct_v1',ledger_contract='deal_ledger_v1',scope_contract='cycle_scope_v1',phase='PAUSED',cycle=None,commands={},seen_deals=[],preserved_live_identity='LIVE-original')
        with closing(sqlite3.connect(path)) as db,db:
            db.execute('CREATE TABLE snapshot(id INTEGER PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE mt5_commands(id TEXT PRIMARY KEY,payload TEXT)')
            db.execute('CREATE TABLE consumed_deals(ticket TEXT PRIMARY KEY,payload TEXT)')
            db.execute('INSERT INTO snapshot VALUES(1,?)',(json.dumps(snapshot),))
        return path

    @contextmanager
    def runtime(self,state=None):
        code=self.manager.version(self.manager.pointer()['current'])/'code'
        state=state or legacy.state_path(self.root,self.manager.config())
        with patch.object(managed,'__file__',str(code/'tradfi'/'managed.py')):
            with managed.runtime_guard(state,instance='live'):yield

    def test_external_state_or_log_path_rejected_without_foreign_writes(self):
        self.install();foreign=self.root.parent/'foreign.sqlite3';foreign.write_bytes(b'preserve')
        original=self.manager.config_path.read_bytes()
        for key in ('state_file','log_file'):
            for value in (str(foreign),'../../foreign.sqlite3'):
                with self.subTest(key=key,value=value):
                    config=json.loads(original);config[key]=value;legacy.atomic_json(self.manager.config_path,config)
                    for operation in (self.manager.install,self.manager.launch):
                        with self.assertRaises(legacy.SetupError):operation()
                    self.assertEqual(foreign.read_bytes(),b'preserve')
        self.manager.config_path.write_bytes(original)
        with self.assertRaises(legacy.SetupError):
            with self.runtime(foreign):pass

    def test_fresh_data_symlink_rejected_without_external_writes(self):
        self.root.mkdir();external=self.root.parent/'external';external.mkdir()
        sentinel=external/'sentinel';sentinel.write_bytes(b'preserve')
        try:(self.root/'data').symlink_to(external,target_is_directory=True)
        except (OSError,NotImplementedError) as exc:self.skipTest(str(exc))
        with self.assertRaises(legacy.SetupError):self.manager.install()
        self.assertEqual(list(external.iterdir()),[sentinel]);self.assertEqual(sentinel.read_bytes(),b'preserve')
        self.assertFalse((self.root/'current.json').exists());self.assertFalse((self.root/'managed-install.json').exists())

    def test_replaced_data_symlink_blocks_all_access(self):
        self.install();self.state();data=self.root/'data';external=self.root.parent/'external';data.rename(external)
        try:data.symlink_to(external,target_is_directory=True)
        except (OSError,NotImplementedError) as exc:external.rename(data);self.skipTest(str(exc))
        before={p:p.read_bytes() for p in external.rglob('*') if p.is_file()}
        for operation in (self.manager.config,self.manager.launch,self.manager.install):
            with self.assertRaises(legacy.SetupError):operation()
        with self.assertRaises(legacy.SetupError):
            with self.runtime(external/'state.sqlite3'):pass
        self.assertEqual(before,{p:p.read_bytes() for p in before});self.assertEqual(self.manager.pointer()['current'],A)

    def test_missing_role_marker_blocks_manager_and_runtime(self):
        self.install();(self.root/'application.json').unlink()
        for operation in (self.manager.launch,self.manager.install):
            with self.assertRaises(legacy.SetupError):operation()
        with self.assertRaises(legacy.SetupError):
            with self.runtime():pass
        self.assertEqual(self.manager.pointer()['current'],A)

    def test_pending_migration_blocks_manager_and_runtime(self):
        self.install();legacy.atomic_json(self.root/'migration-pending.json',{'format':1})
        for operation in (self.manager.launch,self.manager.install):
            with self.assertRaises(legacy.SetupError):operation()
        with self.assertRaises(legacy.SetupError):
            with self.runtime():pass

    def test_missing_or_moved_bound_state_never_resets(self):
        self.install();state=self.state();saved=state.read_bytes()
        legacy.atomic_json(legacy.binding_path(self.root,'live'),{'path':str(state)})
        state.unlink();self.sha=B
        with self.assertRaises(legacy.SetupError):self.manager.install()
        with self.assertRaises(legacy.SetupError):
            with self.runtime(state):pass
        self.assertFalse(state.exists());self.assertEqual(self.manager.pointer()['current'],A)
        state.write_bytes(saved);config=self.manager.config();other=state.with_name('different.sqlite3');config['state_file']=str(other)
        legacy.atomic_json(self.manager.config_path,config)
        with self.assertRaises(legacy.SetupError):self.manager.install()
        with self.assertRaises(legacy.SetupError):
            with self.runtime(other):pass
        self.assertFalse(other.exists());self.assertEqual(state.read_bytes(),saved)

    def test_failed_prepare_preserves_pointer_config_and_state(self):
        self.install();state=self.state();before={p:p.read_bytes() for p in (state,self.manager.config_path,self.root/'current.json')};self.sha=B
        with patch.object(self.manager,'prepare_python',side_effect=legacy.SetupError('offline preparation failed')):
            with self.assertRaises(legacy.SetupError):self.manager.install()
        self.assertEqual(before,{p:p.read_bytes() for p in before});self.assertEqual(self.manager.pointer()['current'],A)

    def test_backup_includes_committed_wal_and_preserves_live_identity(self):
        self.install();state=self.state()
        with closing(sqlite3.connect(state)) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal');writer.execute('PRAGMA wal_autocheckpoint=0')
            row=json.loads(writer.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]);row['committed_in_wal']='preserved'
            writer.execute('UPDATE snapshot SET payload=? WHERE id=1',(json.dumps(row),));writer.commit()
            self.assertTrue(Path(str(state)+'-wal').is_file());backup=self.manager.backup()
            with closing(sqlite3.connect(backup/'state.sqlite3')) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(json.loads(db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]),row)
            self.assertFalse(Path(str(backup/'state.sqlite3')+'-wal').exists())
            self.assertEqual(row['preserved_live_identity'],'LIVE-original')

    def source(self):
        source=self.root.parent/'source.sqlite3'
        with closing(sqlite3.connect(source)) as db,db:
            db.execute('CREATE TABLE identity(value TEXT)');db.execute("INSERT INTO identity VALUES('source')")
        return source

    def test_copy_refuses_open_destination_wal_without_changing_bytes(self):
        source=self.source();destination=self.root.parent/'destination.sqlite3'
        with closing(sqlite3.connect(destination)) as writer:
            writer.execute('CREATE TABLE identity(value TEXT)');writer.execute("INSERT INTO identity VALUES('destination')");writer.commit()
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal');writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute("INSERT INTO identity VALUES('wal-progress')");writer.commit()
            files=[destination,Path(str(destination)+'-wal'),Path(str(destination)+'-shm')];self.assertTrue(all(p.is_file() for p in files));before={p:p.read_bytes() for p in files}
            with self.assertRaises(legacy.SetupError):independent.copy_database(source,destination)
            self.assertEqual(before,{p:p.read_bytes() for p in files})
            self.assertEqual(writer.execute('SELECT value FROM identity').fetchall(),[('destination',),('wal-progress',)])
        self.assertFalse(list(self.root.parent.glob('*.migration')))

    def test_copy_refuses_orphan_sidecars_without_deleting_them(self):
        source=self.source();destination=self.root.parent/'destination.sqlite3'
        for suffix in ('-wal','-shm','-journal'):
            with self.subTest(suffix=suffix):
                destination.write_bytes(b'preserve destination');sidecar=Path(str(destination)+suffix);sidecar.write_bytes(b'preserve sidecar')
                before={p:p.read_bytes() for p in (destination,sidecar)}
                with self.assertRaises(legacy.SetupError):independent.copy_database(source,destination)
                self.assertEqual(before,{p:p.read_bytes() for p in before});sidecar.unlink()
        self.assertFalse(list(self.root.parent.glob('*.migration')))

    def test_exact_historical_dual_contract_rejected_without_pointer_change(self):
        self.install();before=self.manager.config_path.read_bytes();self.sha=B;self.manager.install()
        old=self.manager.version(A)/'code'/'independent-install-contract.json'
        legacy.atomic_json(old,{'format':1,'roles':['live','paper'],'layout':'independent_roots_v1'})
        with self.assertRaises(legacy.SetupError):self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],B);self.assertEqual(self.manager.config_path.read_bytes(),before)

if __name__=='__main__':unittest.main()
