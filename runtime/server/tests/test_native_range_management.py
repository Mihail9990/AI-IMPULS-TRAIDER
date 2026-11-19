import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch
from recovery.native_range_management import NativeRangeManagement
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_entry_observer import EntryFake
from test_native_recovery_control import RANGES
import test_native_recovery_control as control_tests


class RangeManagementTests(unittest.TestCase):
    session=control_tests.ControlTests.session
    def setup_control(self,a,path,submit=True):
        s=self.session(a,path);s.__enter__();self.addCleanup(s.close)
        c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
        if submit:c.submit_initial('1')
        return s,c,NativeRangeManagement(c)
    def test_cancel_explicit_then_archive_and_reset_unique_generation(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db')
            old=c.state()['data'];self.assertEqual(m.cancel_ranges()['phase'],'waiting')
            self.assertEqual(len(a.sent),2);self.assertFalse(s.ready)
            with self.assertRaises(RuntimeError):c.submit_initial('1')
            state=m.replace_ranges([{'id':'1','side':'SELL','price':'3950','volume':'.01'}],
                initial_sl_distance='.60',reentry_distance='.35')['data']
            self.assertNotEqual(state['generation'],old['generation'])
            self.assertNotEqual(state['orders'][0]['key'],old['orders'][0]['key'])
            self.assertEqual(state['orders'][0]['intent']['volume'],'0.01')
            self.assertNotIn('range_management',state)
            self.assertEqual(len(m.history()),1)
            self.assertEqual(m.history()[0]['generation'],old['generation'])
            self.assertEqual(len(a.sent),2)
    def test_open_position_or_unmapped_pending_blocks_without_send(self):
        for mode in ('position','unmapped'):
            with tempfile.TemporaryDirectory() as d:
                a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db')
                if mode=='position':a.fill(.004)
                else:a.orders[0].ticket=999
                with self.assertRaises(RuntimeError):m.cancel_ranges()
                self.assertEqual(len(a.sent),1);self.assertNotIn('range_management',c._read())
    def test_empty_staged_generation_can_wait_and_replace(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db',submit=False)
            self.assertEqual(m.cancel_ranges()['phase'],'waiting')
            self.assertEqual(a.sent,[])
            m.replace_ranges(RANGES);self.assertEqual(a.sent,[])
    def test_restart_dispatch_gap_is_not_resent(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();path=d+'/s.db';s,c,m=self.setup_control(a,path)
            with patch.object(s,'execute',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):m.cancel_ranges()
            s.close()
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);m=NativeRangeManagement(c)
                self.assertEqual(m.reconcile_cancel()['commands']['51']['phase'],'unresolved')
                m.cancel_ranges()
                with self.assertRaises(RuntimeError):m.replace_ranges(RANGES)
            self.assertEqual(len(a.sent),1)
    def test_mapping_commit_failure_reconciles_no_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db')
            original=c._save
            def fail_after_cancel(data):
                if len(a.sent)==2:raise OSError('disk')
                original(data)
            with patch.object(c,'_save',side_effect=fail_after_cancel):
                with self.assertRaises(OSError):m.cancel_ranges()
            self.assertEqual(m.reconcile_cancel()['phase'],'waiting')
            m.cancel_ranges();self.assertEqual(len(a.sent),2)
    def test_fill_during_cancellation_blocks_reset(self):
        class RaceFake(EntryFake):
            def order_send(self,r):
                if r['action']==self.TRADE_ACTION_REMOVE:self.fill(.01)
                return super().order_send(r)
        with tempfile.TemporaryDirectory() as d:
            a=RaceFake();s,c,m=self.setup_control(a,d+'/s.db')
            with self.assertRaises(RuntimeError):m.cancel_ranges()
            with self.assertRaises(RuntimeError):m.replace_ranges(RANGES)
            self.assertEqual(len(a.sent),2);self.assertEqual(len(a.positions),1)
    def test_invalid_new_ranges_do_not_archive_or_reset(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db')
            m.cancel_ranges();before=deepcopy(c._read())
            with self.assertRaises(ValueError):m.replace_ranges(RANGES,initial_sl_distance='.59')
            self.assertEqual(c._read(),before);self.assertEqual(m.history(),[])

    def test_staged_external_terminal_is_readonly_resolved_and_reset(self):
        for terminal in ('ORDER_STATE_CANCELED','ORDER_STATE_EXPIRED'):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as d:
                a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db')
                original=m.reconcile_cancel
                # Interrupt after durable batch planning, before any command dispatch.
                with patch.object(m,'reconcile_cancel',side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):m.cancel_ranges()
                key=c._read()['range_management']['commands']['51']['key']
                order=a.orders.pop();order.state=getattr(a,terminal);a.history=[order]
                result=original()
                self.assertEqual(result['phase'],'waiting')
                self.assertEqual(result['commands']['51']['phase'],'observed_terminal')
                self.assertIsNone(c.db.execute('SELECT id FROM recovery_outbox WHERE id=?',(key,)).fetchone())
                m.replace_ranges(RANGES)
                self.assertEqual(len(a.sent),1)

    def test_staged_disappearance_without_history_never_invents_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db')
            with patch.object(m,'reconcile_cancel',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):m.cancel_ranges()
            a.orders=[]
            with self.assertRaises(ValueError):m.reconcile_cancel()
            self.assertEqual(c._read()['range_management']['commands']['51']['phase'],'staged')
            self.assertEqual(len(a.sent),1)

    def test_fill_after_management_snapshot_before_executor_blocks_remove(self):
        with tempfile.TemporaryDirectory() as d:
            a=EntryFake();s,c,m=self.setup_control(a,d+'/s.db')
            original=s.execute
            def race(key,request,expected):
                a.fill(.004)
                return original(key,request,expected)
            with patch.object(s,'execute',side_effect=race):
                with self.assertRaises(RuntimeError):m.cancel_ranges()
            self.assertEqual(len(a.sent),1)
            self.assertEqual(len(a.positions),1)
            command=c._read()['range_management']['commands']['51']
            journal=s.executor.read(command['key'])
            self.assertTrue(journal['payload']['expected']['require_no_owned_positions'])
            with self.assertRaises(RuntimeError):m.replace_ranges(RANGES)
