import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as N
from recovery.native_coordinator import NativeCoordinator
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_support_cycle import CycleFake, ea_response
from test_native_recovery_control import RANGES
import test_native_recovery_control as controls


class PartialAtSubmit(CycleFake):
    lose_receipt=False
    def order_send(self,r):
        result=super().order_send(r)
        if r['action']==self.TRADE_ACTION_PENDING and result.order==51:
            o=self.orders[0];o.volume_current=.006;o.position_id=88
            self.deals.append(N(ticket=601,order=51,position_id=88,magic=123,symbol=o.symbol,
                entry=0,type=0,volume=.004,price=4000,time_msc=601,reason=3,
                profit=0,commission=0,swap=0,fee=0));self.next_deal=602
            self.positions.append(N(ticket=188,identifier=88,magic=123,symbol=o.symbol,type=0,
                volume=.004,price_open=4000,sl=o.sl,tp=0))
            if self.lose_receipt:raise TimeoutError('receipt lost after partial execution')
            result.retcode=self.TRADE_RETCODE_DONE_PARTIAL
        return result


class PartialCoordinatorRecoveryTests(unittest.TestCase):
    session=controls.ControlTests.session
    def start(self,s,ranges=RANGES):
        n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
        n.command('start',dict(kind='ranges',ranges=ranges,initial_sl_distance='.70',reentry_distance='.70'))
        return n
    def test_immediate_partial_resolved_and_remainder_cancelled_via_tick(self):
        with tempfile.TemporaryDirectory() as d:
            a=PartialAtSubmit(d)
            with self.session(a,d+'/s.db') as s:
                n=self.start(s);a.ask=4000.21
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertFalse(any(o.ticket==51 for o in a.orders))
                self.assertEqual(a.positions[0].volume,.004)
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_REMOVE for r in a.sent),1)
                count=len(a.sent)
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123));n.tick()
                self.assertEqual(len(a.sent),count)
    def test_two_initial_ranges_continue_after_exact_partial_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            a=PartialAtSubmit(d)
            with self.session(a,d+'/s.db') as s:
                n=self.start(s,RANGES+[{'id':'2','side':'SELL','price':'3950','volume':'.01'}])
                self.assertFalse(n.state()['api_reconciliation_required'])
                a.ask=4000.21;self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertTrue(any(o.price_open==3950 for o in a.orders))
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_PENDING and r['price']==4000 for r in a.sent),1)
    def test_tick_resolves_persisted_preverification_partial_after_restart(self):
        with tempfile.TemporaryDirectory() as d:
            a=PartialAtSubmit(d)
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                self.assertEqual(c.submit_initial('1')['data']['orders'][0]['phase'],'unresolved')
            a.ask=4000.21
            with self.session(a,d+'/s.db') as s:
                n=NativeCoordinator(NativeRecoveryControl(s,magic=123))
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertFalse(any(o.ticket==51 for o in a.orders))
                self.assertEqual(sum(r['action']==a.TRADE_ACTION_PENDING and r['price']==4000 for r in a.sent),1)
    def test_lost_receipt_is_not_guessed_or_retried(self):
        with tempfile.TemporaryDirectory() as d:
            a=PartialAtSubmit(d);a.lose_receipt=True
            with self.session(a,d+'/s.db') as s:
                n=self.start(s);a.ask=4000.21
                for _ in range(3):self.assertEqual(n.tick()['tick']['status'],'reconciliation_required')
                self.assertEqual(len(a.sent),1)
                self.assertEqual(a.orders[0].volume_current,.006)
    def test_missing_mailbox_waits_and_recovers_without_readiness_reset(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=self.start(s);a.fill_order(51,88);a.ask=4000.21;n.tick();a.close(88)
                result=n.tick()
                self.assertEqual(result['tick']['status'],'waiting_evidence')
                self.assertTrue(s.ready)
                self.assertIn('mailbox',result['tick']['issues'][0])
                ea_response(n.ledger,51,602,d,'3999.3')
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertEqual(n.ledger.totals()['1']['full_sl_count'],1)
    def test_runtime_scheduler_handles_partial_without_manual_tick(self):
        import threading
        from fastapi.testclient import TestClient
        from recovery.native_runtime import NativeRuntime
        from recovery.native_preflight import TerminalBinding
        class ScheduledPartial(PartialAtSubmit):
            def __init__(self,root):super().__init__(root);self.cancelled=threading.Event()
            def order_send(self,r):
                result=super().order_send(r)
                if r['action']==self.TRADE_ACTION_PENDING and result.order==51:self.ask=4000.21
                if r['action']==self.TRADE_ACTION_REMOVE:self.cancelled.set()
                return result
        with tempfile.TemporaryDirectory() as d:
            a=ScheduledPartial(d)
            runtime=NativeRuntime(a,TerminalBinding(1,'demo','XAUUSD.s',frozenset({123})),
                database=d+'/s.db',magic=123,token='x'*32,interval=.05)
            app=runtime.open()
            with TestClient(app):
                n=app.state.coordinator
                n.command('start',dict(kind='ranges',ranges=RANGES,initial_sl_distance='.70',reentry_distance='.70'))
                self.assertTrue(a.cancelled.wait(2))
                with n.session._lock:
                    self.assertFalse(any(o.ticket==51 for o in a.orders))
                    self.assertEqual(a.positions[0].volume,.004)
                    self.assertEqual(n.last_tick['status'],'ok')
    def test_missing_mailbox_does_not_stop_support_on_open_reverse(self):
        with tempfile.TemporaryDirectory() as d:
            a=CycleFake(d)
            with self.session(a,d+'/s.db') as s:
                n=self.start(s);a.fill_order(51,88);a.ask=4000.21;n.tick()
                a.close(88);a.fill_order(52,89);a.ask=3999.0
                result=n.tick()
                self.assertEqual(result['tick']['status'],'waiting_evidence')
                self.assertTrue(s.ready)
                self.assertEqual(a.positions[0].sl,3999.55)
                self.assertEqual(a.orders,[]) # no successor lot guessed before close evidence
                ea_response(n.ledger,51,602,d,'3999.3')
                self.assertEqual(n.tick()['tick']['status'],'ok')
                self.assertEqual(len(a.orders),1)
