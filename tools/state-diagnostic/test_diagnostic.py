import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import state_readonly_diag as diag

SECRET='PRIVATE_TOKEN_ACCOUNT_TICKET_123456789'
class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        self.path=self.root/'data'/'tradfi_mt5_state.sqlite3'
        (self.root/'current.json').write_text(json.dumps({'current':'a'*40}))
        (self.root/'data'/'service.json').write_text(json.dumps({'password':SECRET,'expected_login':SECRET}))
        self.state={'schema':1,'backend':'mt5_direct_v1','ledger_contract':'deal_ledger_v1','scope_contract':'cycle_scope_v1','phase':'PAUSED','cycle':None,'commands':{},'seen_deals':[],'private':SECRET}
    def save(self,state=None,journal=(),tickets=()):
        db=sqlite3.connect(self.path)
        try:
            db.executescript('CREATE TABLE snapshot(id INTEGER,payload TEXT); CREATE TABLE mt5_commands(id TEXT); CREATE TABLE consumed_deals(ticket TEXT);')
            db.execute('INSERT INTO snapshot VALUES(1,?)',(json.dumps(state if state is not None else self.state),))
            db.executemany('INSERT INTO mt5_commands VALUES(?)',[(x,) for x in journal])
            db.executemany('INSERT INTO consumed_deals VALUES(?)',[(x,) for x in tickets]);db.commit()
        finally:db.close()
    def run_diag(self):
        before={p.relative_to(self.root):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        with patch('socket.socket',side_effect=AssertionError('no network')):
            result=diag.diagnose(self.root)
        after={p.relative_to(self.root):hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after);self.assertNotIn(SECRET,json.dumps(result));return result
    def test_idle_and_repeat_preserve_bytes(self):
        self.save();r=self.run_diag();self.assertEqual(r['nonterminal_commands'],0);self.assertFalse(r['cycle_present']);self.assertEqual(self.run_diag(),r)
    def test_active_phase_and_unknown_commands(self):
        self.state.update(cycle={'private':SECRET},phase=SECRET,commands={SECRET:{'status':'UNKNOWN'}})
        self.save();r=self.run_diag();self.assertTrue(r['cycle_present']);self.assertEqual(r['phase'],'UNKNOWN_STRING');self.assertEqual(r['nonterminal_commands'],1)
    def test_incompatible_contracts(self):
        self.state.update(schema=2,backend=SECRET,ledger_contract=SECRET,scope_contract=SECRET,commands={SECRET:{'status':'DONE'}})
        self.save();r=self.run_diag()
        for key in ('schema_ok','backend_ok','ledger_ok','scope_ok'):self.assertFalse(r[key])
    def test_orphan_journal_and_ledger_difference(self):
        self.save(journal=[SECRET],tickets=[SECRET]);r=self.run_diag();self.assertEqual(r['journal_missing_in_snapshot'],1);self.assertFalse(r['deal_sets_match'])
    def test_duplicate_deals(self):
        self.state['seen_deals']=[SECRET,SECRET];self.save(tickets=[SECRET,SECRET]);r=self.run_diag();self.assertTrue(r['seen_duplicates']);self.assertTrue(r['ledger_duplicates'])
    def test_missing_database_not_created(self):
        self.assertEqual(self.run_diag()['result'],'STATE_FILE_MISSING');self.assertFalse(self.path.exists())
    def test_invalid_database(self):
        self.path.write_text(SECRET);self.assertEqual(self.run_diag()['result'],'READ_FAILED')
    def test_invalid_snapshot(self):
        self.save(state=[SECRET]);self.assertEqual(self.run_diag()['error_stage'],'snapshot_structure')
    def test_missing_snapshot(self):
        self.save();db=sqlite3.connect(self.path);db.execute('DELETE FROM snapshot');db.commit();db.close()
        self.assertEqual(self.run_diag()['result'],'SNAPSHOT_MISSING')
    def test_binding_mismatch(self):
        self.save();(self.root/'data'/'state-binding.json').write_text(json.dumps({'path':SECRET}));self.assertFalse(self.run_diag()['binding_ok'])
    def test_bad_pointer_does_not_hide_database(self):
        self.save();(self.root/'current.json').write_text(SECRET);r=self.run_diag();self.assertEqual(r['installed_commit'],'UNREADABLE');self.assertTrue(r['snapshot_present'])
    def test_readonly_connection_and_no_mutation_statements(self):
        self.save();original=sqlite3.connect;statements=[]
        def connect(filename,**kw):
            self.assertTrue(filename.endswith('?mode=ro'));self.assertTrue(kw['uri']);db=original(filename,**kw);db.set_trace_callback(statements.append);return db
        with patch.object(diag.sqlite3,'connect',side_effect=connect):self.run_diag()
        self.assertTrue(statements)
        for sql in statements:self.assertTrue(sql.startswith(('SELECT ','BEGIN','PRAGMA query_only=ON')),sql)
    def test_all_known_phases_and_installer_allowlist(self):
        for phase in sorted(diag.KNOWN_PHASES):
            with self.subTest(phase=phase):
                self.state['phase'] = phase
                self.save()
                r = self.run_diag()
                self.assertEqual(r['phase'], phase)
                self.assertEqual(r['phase_allowed_by_installer'], phase in {'IDLE', 'PAUSED', 'WAIT_START'})
                self.path.unlink()

    def test_phase_missing_null_unknown_and_invalid_types(self):
        cases = [('MISSING', None), ('NULL', None), ('UNKNOWN_STRING', SECRET),
                 ('INVALID_TYPE', {SECRET: SECRET}), ('INVALID_TYPE', [SECRET]),
                 ('INVALID_TYPE', True), ('INVALID_TYPE', 1)]
        for expected, value in cases:
            with self.subTest(expected=expected, value_type=type(value).__name__):
                self.state['phase'] = value
                if expected == 'MISSING': self.state.pop('phase')
                self.save()
                r = self.run_diag()
                self.assertEqual(r['phase'], expected)
                self.assertFalse(r['phase_allowed_by_installer'])
                self.assertEqual(r['result'], 'READ_COMPLETE_NOT_TRADING_APPROVAL')
                self.path.unlink()

    def test_cycle_and_time_block_categories(self):
        for key in ('cycle', 'time_block'):
            for expected, value in [('MISSING', None), ('NULL', None), ('EMPTY_OBJECT', {}),
                                    ('OBJECT', {SECRET: SECRET}), ('INVALID_TYPE', [SECRET]),
                                    ('INVALID_TYPE', SECRET), ('INVALID_TYPE', False)]:
                with self.subTest(key=key, expected=expected):
                    self.state[key] = value
                    if expected == 'MISSING': self.state.pop(key)
                    self.save()
                    self.assertEqual(self.run_diag()[key + '_kind'], expected)
                    self.path.unlink()

    def test_stop_categories_do_not_coerce_values(self):
        for expected, value in [('MISSING', None), ('TRUE', True), ('FALSE', False),
                                ('INVALID_TYPE', None), ('INVALID_TYPE', 0),
                                ('INVALID_TYPE', 1), ('INVALID_TYPE', SECRET),
                                ('INVALID_TYPE', {SECRET: SECRET})]:
            with self.subTest(expected=expected):
                self.state['stop'] = value
                if expected == 'MISSING': self.state.pop('stop')
                self.save()
                self.assertEqual(self.run_diag()['stop'], expected)
                self.path.unlink()

    def test_latest_committed_wal_snapshot_is_read(self):
        self.save();db=sqlite3.connect(self.path)
        try:
            db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA wal_autocheckpoint=0')
            state=copy.deepcopy(self.state);state['cycle']={'private':SECRET};state.update(phase='SAFE_PAUSE',stop=True,time_block={'reason':SECRET})
            db.execute('UPDATE snapshot SET payload=?',(json.dumps(state),));db.commit()
            before=self.path.read_bytes();wal=Path(str(self.path)+'-wal').read_bytes()
            r=diag.diagnose(self.root);self.assertTrue(r['cycle_present']);self.assertNotIn(SECRET,json.dumps(r))
            self.assertEqual(r['phase'],'SAFE_PAUSE');self.assertEqual(r['cycle_kind'],'OBJECT');self.assertEqual(r['stop'],'TRUE');self.assertEqual(r['time_block_kind'],'OBJECT')
            self.assertEqual(before,self.path.read_bytes());self.assertEqual(wal,Path(str(self.path)+'-wal').read_bytes())
        finally:db.close()

if __name__=='__main__':unittest.main(verbosity=2)
