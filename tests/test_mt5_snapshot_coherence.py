"""Read-only snapshot regressions: never connect to MT5 or send/check orders."""
from copy import deepcopy
import unittest
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.reconciliation_diagnostics import reason_code
from test_raw_market_mode import config
from test_tradfi_native_utc import UTCAPI
from test_tradfi_mt5 import record


class SnapshotCoherenceTests(unittest.TestCase):
    def setUp(self):
        self.api=UTCAPI()
        self.api.order_send=self.api.order_check=lambda *a,**k:self.fail('Trading forbidden')
        self.broker=MT5EvidenceBroker(config(),api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.broker.connect();self.addCleanup(self.broker.close)

    def seed(self, kind, changes):
        self.before=dict(ticket=101,identifier=501,symbol='XAUUSD.s',magic=25101102,
                         type=0,state=1,volume=.01,volume_initial=.01,volume_current=.01,
                         price_open=4000.,price_current=4000.1,profit=.1,sl=3999.,tp=4001.,
                         swap=0.,time_update_msc=1,price_stoplimit=0.,future_field='strict')
        attr='orders' if kind=='order' else 'positions'
        setattr(self.api,attr,[record(**self.before)])
        original=self.broker.reconciliation_evidence
        def evidence():
            audit=original()
            after=dict(self.before,**changes)
            setattr(self.api,attr,[record(**after)])
            return audit
        self.broker.reconciliation_evidence=evidence

    def test_pending_mark_price_drift_and_unknown_ownership_not_promoted(self):
        self.seed('order',dict(price_current=4000.2))
        snap=self.broker.snapshot()
        self.assertFalse(snap['orders'][0]['owned'])
        self.assertEqual(snap['obligations'],{})
        self.assertFalse(snap['history_complete'])

    def test_open_position_mark_to_market_drift(self):
        self.seed('position',dict(price_current=4000.2,profit=.2))
        self.assertEqual(len(self.broker.snapshot()['positions']),1)

    def test_execution_changes_fail_closed(self):
        # Test actual full snapshot validation, not just the projection helper.
        for kind in ('order','position'):
            for key,value in dict(sl=3998.,tp=4002.,price_open=4001.,volume=.02,
                                  volume_current=0.,state=4,type=1,magic=3,symbol='OTHER',
                                  ticket=102,identifier=502,time_update_msc=2,swap=.1,
                                  price_stoplimit=4001.,future_field='changed').items():
                with self.subTest(kind=kind,key=key):
                    self.api.orders=[];self.api.positions=[]
                    # Restore real method before creating each closure.
                    self.broker.reconciliation_evidence=MT5EvidenceBroker.reconciliation_evidence.__get__(self.broker)
                    self.seed(kind,{key:value})
                    with self.assertRaises(OSError):self.broker.snapshot()

    def test_cancellation_fill_and_new_objects_not_merged(self):
        for kind in ('order','position'):
            for replacement in ([],[record(ticket=222,identifier=333)]):
                with self.subTest(kind=kind,replacement=replacement):
                    self.api.orders=[];self.api.positions=[]
                    self.broker.reconciliation_evidence=MT5EvidenceBroker.reconciliation_evidence.__get__(self.broker)
                    self.seed(kind,{})
                    original=self.broker.reconciliation_evidence
                    def change():
                        audit=original()
                        setattr(self.api,'orders' if kind=='order' else 'positions',replacement)
                        return audit
                    self.broker.reconciliation_evidence=change
                    with self.assertRaises(OSError):self.broker.snapshot()

    def test_rollover_retries_tick_and_pair_together(self):
        self.api.now=int(self.api.now//60)*60+59.9
        original=self.api.copy_rates_from_pos;calls=[]
        def rates(*args):
            calls.append(1)
            if len(calls)==1:self.api.now+=.2
            return original(*args)
        self.api.copy_rates_from_pos=rates
        snap=self.broker.snapshot()
        self.assertEqual(len(calls),2)
        self.assertEqual(snap['quote']['raw_time_msc'],int(self.api.now*1000))
        self.assertEqual(snap['candle']['current_id'],int(self.api.now//60)*60)
        self.assertEqual(snap['candle']['id'],snap['candle']['current_id']-60)
        self.assertEqual(snap['market_observation']['broker_time'],snap['quote']['broker_time'])

    def test_market_sample_after_slow_evidence_is_not_old_start_tick(self):
        original=self.broker.reconciliation_evidence
        def slow():
            audit=original();self.api.now+=61;return audit
        self.broker.reconciliation_evidence=slow
        snap=self.broker.snapshot()
        self.assertEqual(snap['quote']['broker_time'],self.api.now)
        self.assertEqual(snap['candle']['current_id'],int(self.api.now//60)*60)

    def test_persistent_rollover_mismatch_bounded_and_sanitized(self):
        original=self.api.copy_rates_from_pos;calls=[]
        def rates(*args):
            calls.append(1);self.api.now+=60;return original(*args)
        self.api.copy_rates_from_pos=rates
        with self.assertRaises(OSError) as ctx:self.broker.snapshot()
        self.assertEqual(len(calls),3)
        self.assertEqual(reason_code(ctx.exception,'OTHER_VALIDATION_FAILURE'),'RAW_TICK_M1_ROLLOVER_UNRESOLVED')
        self.assertEqual(reason_code('secret account 123','OTHER_VALIDATION_FAILURE'),'OTHER_VALIDATION_FAILURE')

    def test_malformed_bar_is_not_retried_or_hidden(self):
        original=self.api.copy_rates_from_pos;calls=[]
        def rates(*args):
            calls.append(1);rows=original(*args);rows[0]['high']=0;return rows
        self.api.copy_rates_from_pos=rates
        with self.assertRaisesRegex(ValueError,'Invalid raw M1 OHLC'):self.broker.snapshot()
        self.assertEqual(len(calls),1)

    def test_raw_observation_also_retries_boundary_without_trade(self):
        self.api.now=int(self.api.now//60)*60+59.9
        original=self.api.copy_rates_from_pos;calls=[]
        def rates(*args):
            calls.append(1)
            if len(calls)==1:self.api.now+=.2
            return original(*args)
        self.api.copy_rates_from_pos=rates
        self.assertTrue(self.broker.observe_raw_market());self.assertEqual(len(calls),2)

    def test_fill_during_market_retry_rejects_before_persistence(self):
        from types import SimpleNamespace
        self.seed('order',{})
        original=self.broker.reconciliation_evidence
        audit=original()
        self.broker.reconciliation_evidence=lambda:deepcopy(audit)
        self.broker.store=SimpleNamespace(load=lambda:None)
        self.broker._transaction=lambda *a:self.fail('Invalid evidence must not be persisted')
        rates=self.api.copy_rates_from_pos
        def fill(*args):
            self.api.orders=[]
            return rates(*args)
        self.api.copy_rates_from_pos=fill
        with self.assertRaisesRegex(OSError,'Orders changed'):self.broker.snapshot()
        self.broker.store=None

    def test_unknown_command_is_never_promoted_on_quote_retry(self):
        audit=self.broker.reconciliation_evidence()
        cid='a'*24
        audit['obligations'][cid]=dict(status='UNKNOWN')
        self.broker.rows[cid]=dict(command=dict(kind='STOP'),result=dict(status='UNKNOWN'))
        self.broker.reconciliation_evidence=lambda:deepcopy(audit)
        self.api.now=int(self.api.now//60)*60+59.9
        original=self.api.copy_rates_from_pos;calls=[]
        def rates(*args):
            calls.append(1)
            if len(calls)==1:self.api.now+=.2
            return original(*args)
        self.api.copy_rates_from_pos=rates
        snap=self.broker.snapshot()
        self.assertEqual(snap['obligations'][cid]['status'],'UNKNOWN')
        self.assertEqual(self.broker.rows[cid]['result']['status'],'UNKNOWN')
        self.assertEqual(len(calls),2)

    def test_late_context_ticks_do_not_relabel_market_sample(self):
        original=self.broker._context
        def context(*args,**kwargs):
            if kwargs.get('stage') in {'evidence_snapshot_complete','evidence_snapshot_return'}:
                self.api.now+=1
            return original(*args,**kwargs)
        self.broker._context=context
        snap=self.broker.snapshot()
        self.assertEqual(snap['quote']['broker_time'],snap['market_observation']['broker_time'])
        self.assertLess(snap['quote']['broker_time'],self.api.now)
        self.assertEqual(snap['quote']['raw_time_msc'],snap['mt5_display_time']['tick_time_msc'])

    def test_new_unknown_field_and_unknown_journal_stay_fail_closed(self):
        self.seed('order',dict(new_future_field='unexpected'))
        cid='a'*24
        self.broker.rows[cid]=dict(command=dict(kind='STOP'),result=dict(status='UNKNOWN'))
        # Keep the real collected evidence independent of this synthetic journal;
        # the assertion exercises snapshot rejection, not obligation attribution.
        self.broker.rows.pop(cid)
        original=self.broker.reconciliation_evidence
        def collect():
            audit=original()
            self.broker.rows[cid]=dict(command=dict(kind='STOP'),result=dict(status='UNKNOWN'))
            return audit
        self.broker.reconciliation_evidence=collect
        with self.assertRaisesRegex(OSError,'Order evidence changed'):self.broker.snapshot()
        self.assertEqual(self.broker.rows[cid]['result']['status'],'UNKNOWN')

    def test_durable_unknown_journal_unchanged_on_rejected_snapshot(self):
        import tempfile
        from pathlib import Path
        from tradfi.storage import Store
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db')
            broker=MT5EvidenceBroker(config(),store,api=self.api,clock=lambda:self.api.now,monotonic=lambda:self.api.now)
            try:
                broker.connect()
                cid='b'*24
                row=dict(command=dict(id=cid,kind='STOP',side='BUY',size='.01',price='4003',sl='4001',tp='4004'),
                         account=broker.account,send_started=True,result=dict(status='UNKNOWN'))
                store.save(dict(schema=1,backend=broker.backend_id,account=broker.account,currency='UST',phase='WAIT_FILL',
                                cycle=None,completed=[],seen_deals=[],commands={cid:dict(row['command'],status='UNKNOWN')}),'seed')
                broker._save_row(cid,row,'durable_intent')
                audit=broker.reconciliation_evidence()
                self.assertEqual(audit['obligations'][cid]['status'],'UNKNOWN')
                # A new order arrives after evidence. Snapshot must not commit
                # facts/readback, nor turn the unknown submission into a retry.
                broker.reconciliation_evidence=lambda:deepcopy(audit)
                self.api.orders=[record(ticket=999)]
                before=list(store.db.iterdump())
                with self.assertRaisesRegex(OSError,'Orders changed'):broker.snapshot()
                self.assertEqual(list(store.db.iterdump()),before)
                self.assertEqual(broker.rows[cid],row)
            finally:
                broker.close();store.close()
