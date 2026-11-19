import tempfile
import unittest
from types import SimpleNamespace as N
from unittest.mock import patch
from recovery.native_close_range import NativeCloseRange
from recovery.native_entry_observer import NativeEntryObserver
from recovery.native_entry_protection import NativeEntryProtection
from recovery.native_initial_reverse import NativeInitialReverse
from recovery.native_recovery_control import NativeRecoveryControl
import test_native_recovery_control as controls
from test_native_recovery_control import RANGES
from test_native_support_cycle import CycleFake


class CloseFake(CycleFake):
    def order_send(self,r):
        if r['action'] != self.TRADE_ACTION_DEAL:return super().order_send(r)
        self.sent.append(r)
        p=next(p for p in self.positions if p.ticket==r['position'])
        self.close(p.identifier,reason=self.DEAL_REASON_CLIENT)
        return N(retcode=self.TRADE_RETCODE_DONE,order=self.deals[-1].order,deal=self.deals[-1].ticket)


class CloseRangeTests(unittest.TestCase):
    session=controls.ControlTests.session
    def setup(self,a,path):
        s=self.session(a,path);s.__enter__();self.addCleanup(s.close)
        c=NativeRecoveryControl(s,magic=123)
        c.stage_initial_ranges(RANGES+[{'id':'2','side':'SELL','price':'3950','volume':'.01'}])
        c.submit_initial('1');c.submit_initial('2');a.fill_order(51,88);a.ask=4000.21
        NativeEntryProtection(c).initial('1');NativeInitialReverse(c).arm('1')
        return s,c
    def test_explicit_close_cancels_only_selected_range(self):
        with tempfile.TemporaryDirectory() as d:
            a=CloseFake(d);s,c=self.setup(a,d+'/s.db')
            before=len(a.sent);result=NativeCloseRange(c).close_and_cancel('1')
            self.assertEqual(result['phase'],'verified')
            self.assertEqual([r['action'] for r in a.sent[before:]],[a.TRADE_ACTION_REMOVE,a.TRADE_ACTION_DEAL])
            self.assertEqual([o.ticket for o in a.orders],[52]);self.assertEqual(a.positions,[])
            self.assertEqual(c.state()['data']['range_modes']['1'],'cancelled')
            with self.assertRaises(RuntimeError):NativeInitialReverse(c).arm('1')
            count=len(a.sent);NativeCloseRange(c).close_and_cancel('1');self.assertEqual(len(a.sent),count)
    def test_ordinary_manual_close_preserves_pending_trigger(self):
        with tempfile.TemporaryDirectory() as d:
            a=CloseFake(d);s,c=self.setup(a,d+'/s.db');a.close(88,reason=0)
            count=len(a.sent);NativeEntryObserver(c).observe('1')
            self.assertEqual({o.ticket for o in a.orders},{52,53});self.assertEqual(len(a.sent),count)
            self.assertNotIn('range_modes',c.state()['data'])
    def test_crash_before_cancel_blocks_restart_without_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            a=CloseFake(d);s,c=self.setup(a,d+'/s.db')
            with patch.object(s,'execute',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):NativeCloseRange(c).close_and_cancel('1')
            count=len(a.sent);s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile();self.assertFalse(s.ready)
                self.assertEqual(NativeCloseRange(c).reconcile('1')['phase'],'unresolved')
                self.assertFalse(s.ready)
            self.assertEqual(len(a.sent),count)
    def test_partial_market_close_is_unresolved_never_retried(self):
        class Partial(CloseFake):
            def order_send(self,r):
                if r['action']!=self.TRADE_ACTION_DEAL:return super().order_send(r)
                self.sent.append(r);p=next(p for p in self.positions if p.ticket==r['position'])
                p.volume=.006
                self.deals.append(N(ticket=602,order=1602,position_id=p.identifier,magic=123,symbol=p.symbol,
                    entry=1,type=1-p.type,volume=.004,price=p.sl,time_msc=602,reason=3,
                    profit=-.1,commission=0,swap=0,fee=0))
                return N(retcode=self.TRADE_RETCODE_DONE_PARTIAL,order=1602,deal=602)
        with tempfile.TemporaryDirectory() as d:
            a=Partial(d);s,c=self.setup(a,d+'/s.db')
            self.assertEqual(NativeCloseRange(c).close_and_cancel('1')['phase'],'unresolved')
            self.assertFalse(s.ready);self.assertEqual(a.positions[0].volume,.006)
            count=len(a.sent);s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123)
                self.assertEqual(NativeCloseRange(c).close_and_cancel('1')['phase'],'unresolved')
                self.assertFalse(s.ready)
            self.assertEqual(len(a.sent),count);self.assertEqual([o.ticket for o in a.orders],[52])
    def test_cancel_raced_by_reverse_fill_never_closes_another_position(self):
        class Racing(CloseFake):
            def order_send(self,r):
                if r['action']!=self.TRADE_ACTION_REMOVE:return super().order_send(r)
                self.sent.append(r);self.fill_order(r['order'],89)
                return N(retcode=self.TRADE_RETCODE_DONE,order=r['order'],deal=0)
        with tempfile.TemporaryDirectory() as d:
            a=Racing(d);s,c=self.setup(a,d+'/s.db');count=len(a.sent)
            self.assertEqual(NativeCloseRange(c).close_and_cancel('1')['phase'],'unresolved')
            self.assertEqual({p.identifier for p in a.positions},{88,89})
            self.assertFalse(any(r['action']==a.TRADE_ACTION_DEAL for r in a.sent[count:]))
            self.assertEqual(c.state()['data']['range_modes']['1'],'closing');self.assertFalse(s.ready)
