import tempfile
import unittest
from unittest.mock import patch
from recovery.native_initial_reverse import NativeInitialReverse
from recovery.native_entry_protection import NativeEntryProtection
from recovery.native_recovery_control import NativeRecoveryControl
import test_native_entry_protection as protection_tests
from test_native_entry_protection import ProtectFake


class ReverseFake(ProtectFake):
    def order_send(self,r):
        result=super().order_send(r)
        if r['action']==self.TRADE_ACTION_PENDING and self.history:
            self.orders[-1].ticket=52;result.order=52
        return result


class ReverseTests(unittest.TestCase):
    session=protection_tests.ProtectionTests.session
    build=protection_tests.ProtectionTests.build
    def test_arms_while_open_and_support_does_not_move_reverse(self):
        with tempfile.TemporaryDirectory() as d:
            a=ReverseFake();s,c,p=self.build(a,d+'/s.db');p.initial('1')
            reverse=NativeInitialReverse(c);record=reverse.arm('1')
            self.assertEqual(record['phase'],'verified');self.assertEqual(record['broker_order'],52)
            self.assertEqual(record['intent']['side'],'SELL')
            self.assertEqual(float(record['intent']['price']),3999.3)
            self.assertEqual(float(record['intent']['sl']),4000)
            self.assertEqual(float(record['intent']['volume']),.01)
            self.assertEqual(len(a.positions),1) # no SL closure was awaited
            a.ask=4000.46;p.support('1')
            self.assertEqual(a.orders[0].price_open,3999.3)
            self.assertEqual(reverse.arm('1'),record);self.assertEqual(len(a.sent),3)
            s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123)
                self.assertEqual(NativeInitialReverse(c).reconcile(record['key']),record)
            self.assertEqual(len(a.sent),3)
    def test_requires_confirmed_initial_protection(self):
        with tempfile.TemporaryDirectory() as d:
            a=ReverseFake();s,c,p=self.build(a,d+'/s.db')
            with self.assertRaises(RuntimeError):NativeInitialReverse(c).arm('1')
            self.assertEqual(len(a.sent),1)
    def test_crash_checkpoint_no_outbox_never_replays(self):
        with tempfile.TemporaryDirectory() as d:
            a=ReverseFake();s,c,p=self.build(a,d+'/s.db');p.initial('1')
            r=NativeInitialReverse(c)
            with patch.object(s,'execute',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):r.arm('1')
            key=next(iter(c.state()['data']['reverse_commands']));s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile();self.assertFalse(s.ready)
                result=NativeInitialReverse(c).reconcile(key)
                self.assertEqual(result['phase'],'unresolved');self.assertFalse(s.ready)
            self.assertEqual(len(a.sent),1)
    def test_reverse_fill_is_observed_and_protected_by_exact_mapped_key(self):
        from types import SimpleNamespace as N
        from recovery.native_entry_observer import NativeEntryObserver
        with tempfile.TemporaryDirectory() as d:
            a=ReverseFake();s,c,p=self.build(a,d+'/s.db');p.initial('1')
            record=NativeInitialReverse(c).arm('1');order=a.orders.pop()
            order.state=a.ORDER_STATE_FILLED;order.position_id=89;order.volume_current=0
            a.history.append(order)
            a.deals.extend([
                N(ticket=602,order=70,position_id=88,magic=0,symbol='XAUUSD.s',entry=1,type=1,
                  volume=.01,price=3999.3,time_msc=602,reason=4,profit=-.007,commission=0,swap=0,fee=0),
                N(ticket=603,order=52,position_id=89,magic=123,symbol='XAUUSD.s',entry=0,type=1,
                  volume=.01,price=3999.3,time_msc=603,reason=3,profit=0,commission=0,swap=0,fee=0)])
            a.positions=[N(ticket=78,identifier=89,magic=123,symbol='XAUUSD.s',type=1,
                volume=.01,price_open=3999.3,sl=4000,tp=0)]
            a.ask=3999.1
            observer=NativeEntryObserver(c)
            observed=observer.observe('1',order_key=record['key'])
            self.assertEqual(observed['phase'],'awaiting_protection')
            self.assertEqual(observed['evidence']['position_identifier'],89)
            self.assertEqual(p.initial('1',order_key=record['key'])['stage'],0)
            a.ask=3999.0;p.support('1',order_key=record['key'])
            self.assertEqual(a.positions[0].sl,3999.55)
