"""Adversarial native broker-history doubles; no live terminal connection."""
import tempfile
import unittest
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
from recovery.native_entry_reader import NativeEntryReader
from recovery.native_entry_observer import NativeEntryObserver
from recovery.native_recovery_control import NativeRecoveryControl
from recovery.native_executor_session import NativeExecutorSession
from recovery.native_preflight import TerminalBinding
from test_native_entry_reader import EntryFake
from test_native_recovery_control import RANGES


class HistoryResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.a=EntryFake();self.path=self.temp.name+'/state.db'
        self.binding=TerminalBinding(1,'demo','XAUUSD.s',frozenset({123}))
        self.s=NativeExecutorSession(self.a,self.binding,database=self.path)
        self.s.__enter__();self.addCleanup(self.s.close)
        self.c=NativeRecoveryControl(self.s,magic=123);self.c.stage_initial_ranges(RANGES)
        self.item=self.c.submit_initial('1')['data']['orders'][0]
        self.reader=NativeEntryReader(self.s);self.observer=NativeEntryObserver(self.c)
    def read(self):return self.reader.read(self.item,magic=123)
    def exit(self,ticket,volume,*,time_msc=None):
        d=deepcopy(self.a.entry_deals[0]);d.ticket=ticket;d.order=1000+ticket
        d.entry=1;d.type=1;d.magic=0;d.volume=volume
        d.time_msc=ticket if time_msc is None else time_msc
        self.a.exit_deals.append(d)
        remaining=Decimal('.01')-sum(Decimal(str(x.volume)) for x in self.a.exit_deals)
        if remaining:self.a.positions[0].volume=float(remaining)
        else:self.a.positions=[]
        return d
    def test_duplicate_entry_and_exit_records_are_deduped_without_volume_growth(self):
        self.a.fill();d=self.exit(201,.004)
        self.a.entry_deals.append(deepcopy(self.a.entry_deals[0]));self.a.exit_deals.append(deepcopy(d))
        one=self.observer.observe('1');two=self.observer.observe('1')
        self.assertEqual(one,two)
        self.assertEqual(len(one['evidence']['deals']),1)
        self.assertEqual(len(one['evidence']['exit_deals']),1)
        self.assertEqual(Decimal(one['evidence']['position']['volume']),Decimal('.006'))
        self.assertEqual(len(self.a.sent),1)
    def test_reordered_multi_fill_and_exit_history_has_identical_durable_result(self):
        self.a.fill();first=self.a.entry_deals[0];first.volume=.004;first.price=4000
        second=deepcopy(first);second.ticket=102;second.volume=.006;second.price=4001;second.time_msc=99
        self.a.entry_deals.append(second);self.a.positions[0].price_open=4000.6
        self.exit(201,.002,time_msc=201);self.exit(202,.003,time_msc=200)
        before=self.observer.observe('1')
        self.a.entry_deals.reverse();self.a.exit_deals.reverse()
        after=self.observer.observe('1')
        self.assertEqual(after,before)
        self.assertEqual(after['evidence']['average_price'],'4000.6')
        self.assertEqual([d['ticket'] for d in after['evidence']['deals']],[102,101])
        self.assertEqual([d['ticket'] for d in after['evidence']['exit_deals']],[202,201])
        self.assertEqual(len(self.a.sent),1)
    def test_delayed_order_history_fails_without_mutation_then_recovers(self):
        self.a.fill();saved=self.a.hist;self.a.hist=[];before=self.c.state()
        with self.assertRaises(ValueError):self.observer.observe('1')
        self.assertEqual(self.c.state(),before);self.assertFalse(self.s.ready)
        self.a.hist=saved;self.c.reconcile()
        self.assertEqual(self.observer.observe('1')['phase'],'awaiting_protection')
        self.assertEqual(len(self.a.sent),1)
    def test_missing_previously_observed_entry_or_exit_does_not_replace_ledger(self):
        self.a.fill();self.exit(201,.004);self.observer.observe('1');before=self.c.state()
        for attr in ('entry_deals','exit_deals'):
            with self.subTest(attr=attr):
                saved=getattr(self.a,attr);setattr(self.a,attr,[])
                with self.assertRaises(ValueError):self.observer.observe('1')
                self.assertEqual(self.c.state(),before)
                setattr(self.a,attr,saved);self.c.reconcile()
                self.observer.observe('1')
        self.assertEqual(len(self.a.sent),1)
    def test_delayed_exit_evidence_does_not_invent_close(self):
        self.a.fill();self.observer.observe('1');before=self.c.state()
        position=self.a.positions.pop()
        with self.assertRaises(ValueError):self.observer.observe('1')
        self.assertEqual(self.c.state(),before)
        self.a.positions=[position];self.exit(201,.01);self.c.reconcile()
        self.assertEqual(self.observer.observe('1')['phase'],'closed_before_protection')
        self.assertEqual(len(self.a.sent),1)
    def test_partial_exit_restart_then_final_exit_no_duplicate_volume(self):
        self.a.fill();self.exit(201,.004)
        first=self.observer.observe('1');self.s.close()
        with NativeExecutorSession(self.a,self.binding,database=self.path) as s:
            c=NativeRecoveryControl(s,magic=123);observer=NativeEntryObserver(c)
            self.assertEqual(observer.observe('1'),first)
            self.exit(202,.006)
            final=observer.observe('1')
            self.assertEqual(final['phase'],'closed_before_protection')
            self.assertEqual(sum(Decimal(x['volume']) for x in final['evidence']['exit_deals']),Decimal('.010'))
            self.assertEqual(observer.observe('1'),final)
        self.assertEqual(len(self.a.sent),1)
    def test_none_is_unavailable_not_an_empty_successful_read(self):
        self.a.fill()
        for name in ('orders_get','history_orders_get','positions_get'):
            with self.subTest(api=name),patch.object(self.a,name,return_value=None):
                with self.assertRaises(ValueError):self.read()
        original=self.a.history_deals_get
        for missing_filter in ('ticket','position'):
            def history(*args,**kwargs):
                return None if missing_filter in kwargs else original(*args,**kwargs)
            with self.subTest(filter=missing_filter),patch.object(self.a,'history_deals_get',side_effect=history):
                with self.assertRaises(ValueError):self.read()
        self.assertEqual(len(self.a.sent),1)
    def test_empty_history_valid_only_when_consistent_with_pending_or_closed_state(self):
        pending=self.read() # no fills, empty historical orders/deals, pending exists
        self.assertEqual(Decimal(pending['filled_volume']),0)
        self.assertIsNotNone(pending['pending_order'])
        self.a.fill();self.assertIsNone(self.read()['pending_order']) # empty live orders is valid
        with patch.object(self.a,'positions_get',return_value=[]):
            with self.assertRaises(ValueError):self.read() # open history cannot be empty positions
        original=self.a.history_deals_get
        with patch.object(self.a,'history_deals_get',side_effect=lambda *args,**kw: [] if 'position' in kw else original(*args,**kw)):
            with self.assertRaises(ValueError):self.read()
        self.exit(201,.01)
        closed=self.read();self.assertIsNone(closed['position'])
        self.assertTrue(closed['terminal_order']);self.assertEqual(len(self.a.sent),1)
    def test_late_fill_during_second_pending_read_is_not_committed(self):
        self.a.fill(.004);original=self.a.orders_get;calls=[];before=self.c.state()
        def orders(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:
                self.a.orders[0].volume_current=.005
                self.a.positions[0].volume=.005
                deal=deepcopy(self.a.entry_deals[0]);deal.ticket=102;deal.volume=.001
                self.a.entry_deals.append(deal)
            return deepcopy(original(*args,**kwargs))
        with patch.object(self.a,'orders_get',side_effect=orders):
            with self.assertRaises(ValueError):self.observer.observe('1')
        self.assertEqual(self.c.state(),before)
        self.c.reconcile();later=self.observer.observe('1')
        self.assertEqual(Decimal(later['evidence']['filled_volume']),Decimal('.005'))
        self.assertEqual(len(self.a.sent),1)
    def test_conflicting_duplicate_exit_is_rejected_without_mutating_observation(self):
        self.a.fill();d=self.exit(201,.004);self.observer.observe('1');before=self.c.state()
        changed=deepcopy(d);changed.profit=-999;self.a.exit_deals.append(changed)
        with self.assertRaises(ValueError):self.observer.observe('1')
        self.assertEqual(self.c.state(),before);self.assertEqual(len(self.a.sent),1)
    def test_coordinator_records_partial_exit_pnl_before_full_close_and_after_restart(self):
        from types import SimpleNamespace as N
        from recovery.native_coordinator import NativeCoordinator
        from test_native_support_cycle import CycleFake
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d);path=d+'/state.db'
            with NativeExecutorSession(a,self.binding,database=path) as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                n.command('start',dict(kind='ranges',ranges=RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                a.fill_order(51,88);a.ask=4000.21;n.tick()
                a.positions[0].volume=.006
                a.deals.append(N(ticket=602,order=1602,position_id=88,magic=0,symbol='XAUUSD.s',
                    entry=1,type=1,volume=.004,price=3999.8,time_msc=602,reason=0,
                    profit=-.20,commission=-.01,swap=0,fee=0))
                result=n.tick();self.assertEqual(result['tick']['status'],'ok')
                expected=result['totals']['1']
                self.assertEqual(Decimal(expected['realized_net']),Decimal('-.21'))
                self.assertEqual(expected['closed_positions'],0);self.assertEqual(expected['full_sl_count'],0)
                count=len(a.sent)
            with NativeExecutorSession(a,self.binding,database=path) as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                self.assertEqual(n.tick()['totals']['1'],expected)
                self.assertEqual(len(a.sent),count)
