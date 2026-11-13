"""Atomic panel migration using local native API doubles only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tradfi.control import ActionRejected
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.market_time_migration import migrate_market_time, migration_availability
from tradfi.market_time_state import bind_market_time_state, resolve_market_time_config, MarketTimeStateError
from tradfi.storage import Store, DurabilityError
from test_raw_market_mode import config
from test_tradfi_native_utc import UTCAPI


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name)/'state.db'); self.addCleanup(self.store.close)
        self.cfg = config(market_time_offset_seconds=7200); self.cfg.pop('market_time_mode')
        self.api = UTCAPI(); self.api.shift = 7200
        self.api.order_send = self.api.order_check = lambda *a, **k: self.fail('Broker mutation forbidden')
        self.broker = MT5EvidenceBroker(self.cfg, self.store, api=self.api,
                                       clock=lambda: self.api.now, monotonic=lambda: self.api.now)
        self.broker.connect(); self.addCleanup(self.broker.close)
        self.state = dict(schema=1, phase='WAIT_START', cycle=None, stop=True,
                          account=self.broker.account, backend='mt5_direct_v1', currency='UST',
                          commands={}, completed=[dict(id='old', realized='-12.50', closed_at=1234)],
                          basket_carry='12.50', last_candle=1200, seen_deals=[],
                          control_receipts={}, time_block='OLD_UTC_BLOCK')
        self.controller = SimpleNamespace(store=self.store, broker=self.broker,
                                          state=self.state, _durable_state=None)
        self.persist()

    def persist(self):
        self.store.save(self.state, 'fixture')
        self.controller._durable_state = deepcopy(self.state)

    def dump(self):
        return list(self.store.db.iterdump())

    def denied(self):
        before = self.dump(); state = deepcopy(self.controller.state)
        with self.assertRaises(ActionRejected): migrate_market_time(self.controller)
        self.assertEqual(self.dump(), before); self.assertEqual(self.controller.state, state)

    def test_migration_preserves_history_debt_and_binding_atomically(self):
        before = deepcopy(self.state)
        result = migrate_market_time(self.controller)
        state = self.store.load()
        for key in ('completed','basket_carry','commands','seen_deals','account','currency','control_receipts'):
            self.assertEqual(state[key], before[key])
        self.assertTrue(state['stop']); self.assertEqual(state['phase'], 'WAIT_START')
        self.assertEqual(state['last_candle'], int((self.api.now+7200)//60)*60)
        self.assertEqual(result['binding']['migration_authority'], 'panel_explicit_v1')
        transition = json.loads(self.store.db.execute('SELECT payload FROM market_time_transitions').fetchone()[0])
        self.assertEqual(transition['previous_last_candle'], 1200)
        self.assertEqual(transition['previous_binding']['market_time_offset_seconds'], 7200)
        self.assertEqual(transition['previous_time_block'], 'OLD_UTC_BLOCK')

    def test_cycle_unknown_commands_and_all_pending_receipts_denied(self):
        for change in ({'cycle': {}}, {'phase':'SAFE_PAUSE'}, {'phase':'MANUAL'}, {'phase':'MIGRATION_BLOCK'},
                       {'commands':{'c':{'status':'UNKNOWN'}}}, {'commands':{'c':{'status':'READY'}}},
                       {'control_receipts':{'r':{'status':'WAITING_DATA'}}},
                       {'control_receipts':{'r':{'status':'UNKNOWN'}}},
                       {'control_receipts':{'r':{'status':'QUEUED'}}}):
            with self.subTest(change=change):
                original = deepcopy(self.state); self.state.update(change); self.persist(); self.denied()
                self.state.clear(); self.state.update(original); self.persist()

    def test_only_current_unknown_migration_receipt_exempted(self):
        self.state['control_receipts']={'migration':{'status':'UNKNOWN'}}
        self.controller._active_control_id='migration'; self.persist()
        migrate_market_time(self.controller)
        self.assertEqual(self.store.load()['control_receipts']['migration']['status'],'APPLIED')
        # This is the crash-recovery state before gateway completion or broker activation.
        reopened=Store(self.store.path)
        try:
            self.assertEqual(reopened.load()['control_receipts']['migration']['status'],'APPLIED')
            self.assertEqual(resolve_market_time_config(reopened,self.cfg)['market_time_mode'],'mt5_raw_v1')
        finally: reopened.close()

    def test_account_wide_objects_and_unavailable_reads_denied(self):
        for name, value in (('positions_get',[object()]), ('orders_get',[object()]), ('positions_get',None)):
            old=getattr(self.api,name)
            setattr(self.api,name,lambda *a,_old=old,_value=value,**kw:_old(*a,**kw) if kw else _value)
            self.denied();setattr(self.api,name,old)

    def test_identity_mismatch_and_disconnection_denied(self):
        original=self.api.account_info
        self.api.account_info=lambda:original()._replace(login=222)
        self.denied();self.api.account_info=original
        original_terminal=self.api.terminal_info
        self.api.terminal_info=lambda:original_terminal()._replace(connected=False)
        self.denied()

    def test_incoherent_bars_denied_but_cached_valid_data_allowed(self):
        old=self.api.copy_rates_from_pos
        self.api.copy_rates_from_pos=lambda *a:[dict(r,time=r['time']-60) for r in old(*a)]
        self.denied();self.api.copy_rates_from_pos=old
        self.broker.clock=lambda:self.api.now+86400*90
        migrate_market_time(self.controller)

    def test_atomic_failure_rolls_back_every_table(self):
        for stage in ('market_time_binding_written','market_time_before_commit'):
            before=self.dump();state=deepcopy(self.controller.state)
            def fault(event):
                if event==stage: raise OSError('injected storage failure')
            self.store.fault=fault
            with self.assertRaises(DurabilityError): migrate_market_time(self.controller)
            self.assertEqual(self.dump(),before);self.assertEqual(self.controller.state,state)
        self.store.fault=None

    def test_restart_resolves_authorized_mode_without_config_write(self):
        migrate_market_time(self.controller)
        cfg=deepcopy(self.cfg);resolved=resolve_market_time_config(self.store,cfg)
        self.assertEqual(cfg,self.cfg);self.assertEqual(resolved['market_time_mode'],'mt5_raw_v1')
        self.assertEqual(resolved['market_time_offset_seconds'],0)
        bind_market_time_state(self.store,resolved)
        for delta in ({'expected_login':222},{'market_time_mode':'fixed_utc_v1'}):
            with self.assertRaises(MarketTimeStateError):resolve_market_time_config(self.store,dict(cfg,**delta))

    def test_unauthorized_manual_flip_remains_blocked(self):
        cfg=dict(self.cfg,market_time_mode='mt5_raw_v1',market_time_offset_seconds=0)
        with self.assertRaises(MarketTimeStateError):bind_market_time_state(self.store,resolve_market_time_config(self.store,cfg))

    def test_retry_is_idempotent(self):
        migrate_market_time(self.controller);before=self.dump()
        result=migrate_market_time(self.controller)
        self.assertTrue(result['already_migrated']);self.assertEqual(self.dump(),before)

    def test_financial_ledger_and_old_command_timestamps_are_unchanged(self):
        self.store.enable_deal_ledger()
        record=dict(backend='mt5_direct_v1',account=self.state['account'],ticket='71',
                    currency='UST',position_id='70',cycle_id='old',net='-12.50',
                    raw=dict(ticket=71,position_id=70,time_msc=1234000))
        payload=json.dumps(record)
        self.store.db.execute('INSERT INTO consumed_deals VALUES(?,?,?,?)',
                             (record['backend'],record['account'],'71',payload))
        self.state.update(ledger_contract='deal_ledger_v1',seen_deals=['71'])
        cid='a'*24
        self.state['commands'][cid]=dict(id=cid,kind='STOP',status='OBSOLETE',never_sent=True,
                                         cycle_id='old',created_at_utc=1234,bar_id=1200)
        self.persist();old=deepcopy(self.state)
        migrate_market_time(self.controller)
        for key in ('completed','commands','basket_carry','seen_deals'):
            self.assertEqual(self.store.load()[key],old[key])
        self.assertEqual(self.store.db.execute('SELECT payload FROM consumed_deals').fetchone()[0],payload)

    def test_missing_terminal_proof_and_unknown_ownership_denied(self):
        cid='b'*24
        self.state['commands'][cid]=dict(id=cid,kind='STOP',status='FILLED',cycle_id='old')
        self.persist();self.denied()

    def test_ledger_mismatch_rolls_back(self):
        self.store.enable_deal_ledger()
        self.state.update(ledger_contract='deal_ledger_v1',seen_deals=['missing'])
        # Deliberately corrupt the snapshot without Store.save validation.
        self.store.db.execute('UPDATE snapshot SET payload=?',(json.dumps(self.state),))
        self.controller._durable_state=deepcopy(self.state)
        before=self.dump()
        with self.assertRaises(DurabilityError):migrate_market_time(self.controller)
        self.assertEqual(self.dump(),before)

    def test_availability_is_read_only_and_respects_pending_actions(self):
        before=self.dump()
        self.broker.api=None
        self.addCleanup(setattr,self.broker,'api',self.api)
        self.assertTrue(migration_availability(self.controller)[0])
        self.state['control_receipts']['queued']=dict(status='WAITING_DATA')
        self.assertFalse(migration_availability(self.controller)[0])
        self.assertEqual(self.dump(),before)

    def test_unidentified_history_is_not_adopted(self):
        self.state['account']=None;self.persist();self.denied()
        self.state['completed']=[];self.persist();self.denied()  # nonzero debt

    def test_real_read_only_startup_gateway_migration_binds_pristine_account(self):
        from tradfi.recovery_v3 import ControlledRecoveryController
        from tradfi.control import ActionGateway
        store=Store(Path(self.tmp.name)/'pristine.db');self.addCleanup(store.close)
        broker=MT5EvidenceBroker(self.cfg,store,api=self.api,
                                 clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        broker.connect();self.addCleanup(broker.close)
        controller=ControlledRecoveryController(store,broker,clock=lambda:self.api.now,
                                                monotonic=lambda:self.api.now)
        self.assertIsNone(controller.state['account'])
        snapshot=dict(connected=True,observations_usable=True,account=broker.account,orders=[],positions=[])
        self.assertTrue(migration_availability(controller,snapshot)[0])
        gateway=ActionGateway(controller)
        receipt=gateway.apply(dict(id='migrate_pristine',revision=controller.state.get('control_revision',0),
                                   action=dict(type='command',text='/use_mt5_raw_time')))
        self.assertEqual(receipt['status'],'APPLIED',receipt)
        self.assertEqual(store.load()['account'],broker.account)
        self.assertTrue(store.load()['stop'])
        self.assertTrue(broker.read_only)
        self.assertEqual(broker.market_time_mode,'mt5_raw_v1')
        self.assertEqual(store.load()['commands'],{})
        self.assertEqual(store.load()['completed'],[])

    def seed_archived_filled_cycle(self):
        from test_tradfi_evidence import EvidenceTest, CID
        fixture=EvidenceTest();fixture.setUp();fixture.close()
        self.api.history=fixture.api.history;self.api.deals=fixture.api.deals
        self.api.positions=[];self.api.orders=[]
        cmd=dict(fixture.cmd,cycle_id='old')
        self.state.update(scope_contract='cycle_scope_v1',ledger_contract='deal_ledger_v1',
                          commands={CID:dict(cmd,status='FILLED')},seen_deals=['302'])
        row=dict(fixture.rows[CID],command=cmd,result=dict(status='ACCEPTED'))
        self.broker.rows={CID:row}
        self.store.db.execute('INSERT INTO mt5_commands VALUES(?,?)',(CID,json.dumps(row)))
        self.store.enable_deal_ledger()
        record=dict(backend=self.state['backend'],account=self.state['account'],currency='UST',
                    ticket='302',position_id='501',cycle_id='old',raw=dict(fixture.exit._asdict()))
        self.store.db.execute('INSERT INTO consumed_deals VALUES(?,?,?,?)',
                             (record['backend'],record['account'],'302',json.dumps(record)))
        self.persist()
        # Archive verification must use targeted order/position calls, not ranges.
        for name in ('history_orders_get','history_deals_get'):
            original=getattr(self.api,name)
            def targeted(*args,_original=original,**kwargs):
                if args:self.fail('Unscoped historical discovery forbidden during migration')
                return _original(**kwargs)
            setattr(self.api,name,targeted)

    def test_archived_closed_cycle_preserves_exact_ledger_and_commands(self):
        self.seed_archived_filled_cycle()
        state=deepcopy(self.state)
        ledger=list(self.store.db.execute('SELECT * FROM consumed_deals'))
        journal=list(self.store.db.execute('SELECT * FROM mt5_commands'))
        migrate_market_time(self.controller)
        for key in ('completed','commands','seen_deals','basket_carry'):
            self.assertEqual(self.store.load()[key],state[key])
        self.assertEqual(list(self.store.db.execute('SELECT * FROM consumed_deals')),ledger)
        self.assertEqual(list(self.store.db.execute('SELECT * FROM mt5_commands')),journal)

    def test_archived_unavailable_proof_denied_without_changes(self):
        self.seed_archived_filled_cycle();self.api.position_none=True
        self.denied()

    def test_archived_changed_financial_fact_denied_without_changes(self):
        self.seed_archived_filled_cycle()
        self.api.deals[-1]=self.api.deals[-1]._replace(profit=999.)
        self.denied()

    def seed_manual_archive(self, *, provenance=True):
        self.seed_archived_filled_cycle()
        self.api.deals[-1]=self.api.deals[-1]._replace(reason=self.api.DEAL_REASON_EXPERT)
        row=json.loads(self.store.db.execute('SELECT payload FROM consumed_deals').fetchone()[0])
        row['raw']=dict(self.api.deals[-1]._asdict())
        self.store.db.execute('UPDATE consumed_deals SET payload=?',(json.dumps(row),))
        if provenance:self.state['completed'][0]['completion']='MANUAL_RESET_RETAINED'
        self.persist()

    def test_archived_manual_reset_with_external_exit_preserves_financial_state(self):
        self.seed_manual_archive()
        before=deepcopy(self.state)
        ledger=list(self.store.db.execute('SELECT * FROM consumed_deals'))
        migrate_market_time(self.controller)
        for key in ('completed','commands','seen_deals','basket_carry'):
            self.assertEqual(self.store.load()[key],before[key])
        self.assertEqual(list(self.store.db.execute('SELECT * FROM consumed_deals')),ledger)

    def test_archived_external_exit_without_manual_provenance_denied(self):
        self.seed_manual_archive(provenance=False);self.denied()

    def test_archived_manual_reset_changed_ledger_denied(self):
        self.seed_manual_archive()
        row=json.loads(self.store.db.execute('SELECT payload FROM consumed_deals').fetchone()[0])
        row['raw']['commission']=12
        self.store.db.execute('UPDATE consumed_deals SET payload=?',(json.dumps(row),))
        self.denied()

    def test_archived_manual_reset_unavailable_history_denied(self):
        self.seed_manual_archive();self.api.position_none=True;self.denied()

    def test_archived_manual_reset_active_position_denied(self):
        from test_tradfi_evidence import EvidenceTest
        self.seed_manual_archive()
        fixture=EvidenceTest();fixture.setUp()
        self.api.positions=[fixture.position]
        self.denied()

    def test_archived_manual_reset_unknown_send_not_relabelled(self):
        self.seed_manual_archive()
        cid=next(iter(self.broker.rows))
        self.broker.rows[cid]['result']['status']='UNKNOWN'
        self.store.db.execute('UPDATE mt5_commands SET payload=? WHERE id=?',
                             (json.dumps(self.broker.rows[cid]),cid))
        self.denied()

    def test_stale_snapshot_and_invalid_ledger_denied_unchanged(self):
        self.controller.state=deepcopy(self.state);self.controller.state['basket_carry']='0'
        self.denied()


if __name__ == '__main__': unittest.main()
