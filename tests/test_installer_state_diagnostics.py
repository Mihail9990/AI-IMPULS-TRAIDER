"""Offline real-store regressions: no terminal, credentials or order sends."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import install_tradfi as installer
from tradfi.storage import Store
from tradfi.market_time_state import bind_market_time_state
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.recovery_v3 import ControlledRecoveryController
from test_raw_market_mode import config
from test_tradfi_native_utc import UTCAPI


class InstallerStateDiagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        self.path=self.root/'data'/'state.sqlite3'
        self.cfg=config(state_file=str(self.path))
        self.store=Store(self.path);self.addCleanup(self.store.close)
        bind_market_time_state(self.store,self.cfg)
        installer.atomic_json(installer.binding_path(self.root),{'path':str(self.path)})
    def inspect(self):
        before=list(self.store.db.iterdump())
        try:installer.inspect_state(self.root,self.cfg)
        finally:self.assertEqual(before,list(self.store.db.iterdump()))
    def broker(self):
        api=UTCAPI();api.order_send=api.order_check=lambda *a,**k:self.fail('No orders permitted')
        b=MT5EvidenceBroker(self.cfg,self.store,api=api,clock=lambda:api.now,monotonic=lambda:10)
        self.addCleanup(b.close)
        return b
    def test_failed_first_start_without_controller_can_update_without_reset(self):
        self.inspect()
        self.broker()  # Constructor initializes empty journal before MT5 connect.
        self.inspect()
    def test_connected_before_controller_preserves_populated_binding(self):
        b=self.broker();b.connect()
        self.inspect()

    def test_real_fresh_v3_controller_idle_passes(self):
        b=self.broker();b.connect()
        c=ControlledRecoveryController(self.store,b,clock=lambda:1700000000,monotonic=lambda:10)
        c.save('service_started')
        self.assertEqual(c.state['phase'],'IDLE')
        self.inspect()
    def test_empty_snapshot_with_operational_evidence_stays_blocked(self):
        self.store.db.execute("INSERT INTO audit(event,payload) VALUES('prior','{}')")
        with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
    def test_missing_snapshot_with_order_journal_stays_blocked(self):
        self.broker()
        self.store.db.execute("INSERT INTO mt5_commands VALUES('x','{}')")
        with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
    def test_unknown_table_even_empty_stays_blocked(self):
        self.store.db.execute('CREATE TABLE future_state(x)')
        with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
    def test_reason_specific_active_state_and_unknown_commands(self):
        b=self.broker();b.connect();c=ControlledRecoveryController(self.store,b)
        c.save('service_started')
        for change,reason in [({'cycle':{'id':'x'}},'CYCLE_NOT_FINISHED'),({'phase':'SAFE_PAUSE'},'PHASE_REQUIRES_RECONCILIATION'),({'commands':{'x':{'status':'UNKNOWN'}}},'COMMAND_NOT_TERMINAL')]:
            with self.subTest(reason=reason):
                state=dict(c.state,**change)
                self.store.db.execute('UPDATE snapshot SET payload=?',(json.dumps(state),))
                with self.assertRaisesRegex(installer.SetupError,reason):self.inspect()
    def test_lookalike_internal_table_is_not_ignored(self):
        self.store.db.execute('CREATE TABLE sqliteXstate(obligation TEXT)')
        self.store.db.execute("INSERT INTO sqliteXstate VALUES('pending')")
        with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
    def test_malformed_binding_blocked(self):
        self.store.db.execute("UPDATE market_time_binding SET payload='{}'")
        with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
    def test_changed_identity_binding_blocked(self):
        self.cfg['expected_login']=42
        with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
    def test_unknown_schema_and_views_blocked(self):
        for statement in ('CREATE VIEW invisible AS SELECT 1',
                          'CREATE TRIGGER hidden AFTER INSERT ON audit BEGIN SELECT 1; END'):
            with self.subTest(statement=statement):
                self.store.db.execute(statement)
                with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
                self.store.db.execute('DROP VIEW invisible' if 'VIEW' in statement else 'DROP TRIGGER hidden')
        self.store.db.execute('DROP TABLE audit')
        self.store.db.execute('CREATE TABLE audit(seq TEXT, event TEXT, payload TEXT)')
        with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
    def test_every_operational_table_blocks_missing_snapshot(self):
        self.broker();self.store.enable_deal_ledger()
        for table,values in [('audit',"1,'event','{}'"),('mt5_commands',"'id','{}'"),
                             ('mt5_facts',"'deal','id','{}'"),('mt5_events',"1,'{}'"),
                             ('consumed_deals',"'backend','account','id','{}'")]:
            with self.subTest(table=table):
                self.store.db.execute('INSERT INTO '+table+' VALUES('+values+')')
                with self.assertRaisesRegex(installer.SetupError,'SNAPSHOT_MISSING_OR_INVALID'):self.inspect()
                self.store.db.execute('DELETE FROM '+table)

    def test_defaults_do_not_authorize_execution(self):
        cfg=json.loads(Path('tradfi_config.example.json').read_text())
        self.assertEqual(cfg['execution_contract'],'cycle_scoped_identity_v1')
        self.assertEqual(cfg['market_time_mode'],'mt5_raw_v1')
        self.assertNotIn('market_time_offset_seconds',cfg)
        for name in ('enable_trading','legacy_ea_removed','legacy_state_reconciled'):
            self.assertIs(cfg[name],False)

if __name__=='__main__':unittest.main()
