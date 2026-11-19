import tempfile
import unittest
from copy import deepcopy
from types import SimpleNamespace as N
from unittest.mock import patch
from recovery.native_entry_observer import NativeEntryObserver
from test_native_recovery_control import FillingFake, RANGES
import test_native_recovery_control as control_tests
from recovery.native_recovery_control import NativeRecoveryControl


class EntryFake(FillingFake):
    DEAL_ENTRY_IN=0;DEAL_ENTRY_OUT=1;DEAL_ENTRY_INOUT=2;DEAL_ENTRY_OUT_BY=3
    DEAL_TYPE_BUY=0;DEAL_TYPE_SELL=1
    ORDER_STATE_PARTIAL=3;ORDER_STATE_PLACED=1;ORDER_STATE_EXPIRED=6;ORDER_STATE_REJECTED=5
    def __init__(self):
        super().__init__();self.deals=[];self.late=False
    def history_orders_get(self,*args,**kwargs):
        return tuple(x for x in self.history if not kwargs or x.ticket==kwargs.get('ticket'))
    def history_deals_get(self,*args,**kwargs):
        return tuple(x for x in self.deals if
            ('ticket' not in kwargs or x.order==kwargs['ticket']) and
            ('position' not in kwargs or x.position_id==kwargs['position']))
    def fill(self,volume,ticket=601,price=4000):
        self.deals.append(N(ticket=ticket,order=51,position_id=88,magic=123,
            symbol='XAUUSD.s',entry=0,type=0,volume=volume,price=price,
            time_msc=ticket,reason=0,profit=0,commission=0,swap=0,fee=0))
        total=sum(x.volume for x in self.deals)
        average=sum(x.volume*x.price for x in self.deals)/total
        self.positions=[N(ticket=77,identifier=88,magic=123,symbol='XAUUSD.s',type=0,
            volume=total,price_open=average,sl=3999.3,tp=0)]
        self.orders[0].volume_current=round(.01-total,8)
        self.orders[0].position_id=88
        if total==.01:
            self.orders[0].state=self.ORDER_STATE_FILLED
            self.history=[self.orders.pop()]
    def order_send(self,r):
        if r['action']!=self.TRADE_ACTION_REMOVE:return super().order_send(r)
        self.sent.append(r)
        if self.late:self.fill(.002,ticket=602)
        if self.orders:
            order=self.orders.pop();order.state=self.ORDER_STATE_CANCELED
            self.history=[order]
        return N(retcode=self.TRADE_RETCODE_DONE,order=51,deal=0)


class ObserverTests(unittest.TestCase):
    session=control_tests.ControlTests.session
    def setup_control(self,a,path, ranges=RANGES):
        s=self.session(a,path);s.__enter__();self.addCleanup(s.close)
        c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(ranges);c.submit_initial('1')
        return s,c,NativeEntryObserver(c)
    def test_full_fill_is_durable_no_implicit_protection_or_reverse(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,o=self.setup_control(a,d+'/s.db');a.fill(.01)
            result=o.observe('1');self.assertEqual(result['phase'],'awaiting_protection')
            self.assertEqual(o.observe('1'),result);s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123)
                self.assertEqual(NativeEntryObserver(c).observe('1'),result)
            self.assertEqual(len(a.sent),1)
    def test_partial_cancel_then_late_fill_and_restart_no_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,o=self.setup_control(a,d+'/s.db');a.fill(.004);a.late=True
            self.assertEqual(o.observe('1')['phase'],'partial_remainder_pending')
            result=o.cancel_partial_remainder('1')
            self.assertEqual(result['phase'],'awaiting_protection')
            self.assertEqual(float(result['evidence']['filled_volume']),.006)
            self.assertEqual(result['cancellation']['phase'],'verified');s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);o=NativeEntryObserver(c)
                self.assertEqual(o.cancel_partial_remainder('1'),result)
            self.assertEqual(len(a.sent),2)
    def test_changed_deal_rejected_without_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,o=self.setup_control(a,d+'/s.db');a.fill(.01)
            o.observe('1');before=deepcopy(c.state())
            a.deals[0].time_msc+=1
            with self.assertRaises(ValueError):o.observe('1')
            self.assertEqual(c.state(),before);self.assertFalse(s.ready)
    def test_cancel_checkpoint_before_send_never_replayed(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,o=self.setup_control(a,d+'/s.db',ranges=RANGES+[
                {'id':'2','side':'SELL','price':'3950','volume':'.01'}]);a.fill(.004)
            with patch.object(s,'execute',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):o.cancel_partial_remainder('1')
            s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);o=NativeEntryObserver(c)
                # Even skipping control reconciliation cannot bypass durable guard.
                with self.assertRaises(RuntimeError):c.submit_initial('2')
                c.reconcile();self.assertFalse(s.ready)
                with self.assertRaises(RuntimeError):c.submit_initial('2')
                result=o.cancel_partial_remainder('1')
                self.assertEqual(result['cancellation']['phase'],'unresolved');self.assertFalse(s.ready)
            self.assertEqual(len(a.sent),1)

    def test_post_cancel_commit_failure_recovers_without_resend(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,o=self.setup_control(a,d+'/s.db');a.fill(.004)
            original=c._save;calls=[]
            def fail(data):
                calls.append(1)
                if len(calls)==3:raise OSError('disk failure after cancel verified')
                original(data)
            with patch.object(c,'_save',side_effect=fail):
                with self.assertRaises(OSError):o.cancel_partial_remainder('1')
            self.assertFalse(s.ready);s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);o=NativeEntryObserver(c)
                result=o.reconcile_partial('1')
                self.assertEqual(result['cancellation']['phase'],'verified')
                self.assertEqual(result['phase'],'awaiting_protection')
            self.assertEqual(len(a.sent),2)

class ImmediatePartialFake(EntryFake):
    def order_send(self,r):
        result=super().order_send(r)
        if r['action']==self.TRADE_ACTION_PENDING:
            self.fill(.002,ticket=601,price=4000)
            self.fill(.002,ticket=602,price=4000.02)
        return result


class InitialPartialTests(unittest.TestCase):
    session=control_tests.ControlTests.session
    def test_receipt_bound_partial_before_verification_then_cancel(self):
        with tempfile.TemporaryDirectory() as d:
            a=ImmediatePartialFake();a.retcode=a.TRADE_RETCODE_DONE_PARTIAL
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                self.assertEqual(c.submit_initial('1')['data']['orders'][0]['phase'],'unresolved')
                self.assertFalse(s.ready)
                state=c.resolve_initial_partial('1')
                self.assertEqual(state['data']['orders'][0]['broker_order'],51)
                self.assertEqual(c.resolve_initial_partial('1'),state)
                o=NativeEntryObserver(c);result=o.cancel_partial_remainder('1')
                self.assertEqual(result['cancellation']['phase'],'verified')
                self.assertEqual(float(result['evidence']['filled_volume']),.004)
                self.assertEqual(float(result['evidence']['average_price']),4000.01)
            self.assertEqual(len(a.sent),2)
    def test_lost_receipt_never_infers_ticket(self):
        with tempfile.TemporaryDirectory() as d:
            a=ImmediatePartialFake();a.lose=True
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                c.submit_initial('1')
                with self.assertRaises(ValueError):c.resolve_initial_partial('1')
                self.assertFalse(s.ready)
            self.assertEqual(len(a.sent),1)
    def test_partial_verified_journal_survives_control_commit_failure(self):
        with tempfile.TemporaryDirectory() as d:
            a=ImmediatePartialFake()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
                with patch.object(c,'_save',side_effect=OSError('commit failed')):
                    with self.assertRaises(OSError):c.resolve_initial_partial('1')
                self.assertFalse(s.ready)
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123)
                self.assertEqual(c.reconcile()['data']['orders'][0]['phase'],'verified')
                self.assertEqual(NativeEntryObserver(c).observe('1')['phase'],'partial_remainder_pending')
            self.assertEqual(len(a.sent),1)

    def test_first_observation_checks_durable_partial_receipt_baseline(self):
        with tempfile.TemporaryDirectory() as d:
            a=ImmediatePartialFake()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
                c.resolve_initial_partial('1')
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile()
                a.deals[0].time_msc+=10
                with self.assertRaises(ValueError):NativeEntryObserver(c).observe('1')
                self.assertNotIn('entry_observations',c.state()['data'])
                self.assertFalse(s.ready)
            self.assertEqual(len(a.sent),1)
