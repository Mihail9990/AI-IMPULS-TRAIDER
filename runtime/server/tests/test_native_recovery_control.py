import tempfile
import unittest
from types import SimpleNamespace as N
from unittest.mock import patch
from recovery.native_executor_session import NativeExecutorSession
from recovery.native_preflight import TerminalBinding
from recovery.native_recovery_control import NativeRecoveryControl
from test_recovery_native_transport import NativeSendFake

RANGES=[{'id':'1','side':'BUY','price':'4000','volume':'.01'}]
class FillingFake(NativeSendFake):
    def order_send(self,r):
        result=super().order_send(r)
        self.orders.append(N(ticket=51,magic=r['magic'],symbol=r['symbol'],type=r['type'],
            price_open=r['price'],sl=r['sl'],tp=r['tp'],volume_current=r['volume'],volume_initial=r['volume'],state=1))
        return result

class ControlTests(unittest.TestCase):
    def session(self,a,path):
        return NativeExecutorSession(a,TerminalBinding(1,'demo','XAUUSD.s',frozenset({123})),database=path)
    def test_plan_send_map_restart_without_resend(self):
        with tempfile.TemporaryDirectory() as d:
            a=FillingFake();path=d+'/state.db'
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123)
                c.stage_initial_ranges(RANGES,initial_sl_distance='.60',reentry_distance='.01')
                self.assertEqual(a.sent,[])
                state=c.submit_initial('1')['data']['orders'][0]
                self.assertEqual(state['phase'],'verified');self.assertEqual(state['broker_order'],51)
                self.assertEqual(a.sent[0]['sl'],3999.4)
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile()
                with self.assertRaises(RuntimeError):c.submit_initial('1')
                with self.assertRaises(RuntimeError):c.stage_initial_ranges(RANGES)
            self.assertEqual(len(a.sent),1)
    def test_loss_never_replays(self):
        with tempfile.TemporaryDirectory() as d:
            a=FillingFake();a.lose=True;path=d+'/state.db'
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                self.assertEqual(c.submit_initial('1')['data']['orders'][0]['phase'],'unresolved')
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile()
                self.assertFalse(s.ready)
                with self.assertRaises(RuntimeError):c.submit_initial('1')
            self.assertEqual(len(a.sent),1)
    def test_pre_submit_crash_checkpoint_requires_manual_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            a=FillingFake();path=d+'/state.db'
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                with patch.object(s,'execute',side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):c.submit_initial('1')
                self.assertFalse(s.ready)
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile();self.assertFalse(s.ready)
                with self.assertRaises(RuntimeError):c.submit_initial('1')
            self.assertEqual(a.sent,[])
    def test_post_send_mapping_failure_can_recover_without_resend(self):
        with tempfile.TemporaryDirectory() as d:
            a=FillingFake();path=d+'/state.db'
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                original=c._save;calls=[]
                def fail(data):
                    calls.append(1)
                    if len(calls)==2:raise OSError('disk failure')
                    original(data)
                with patch.object(c,'_save',side_effect=fail):
                    with self.assertRaises(OSError):c.submit_initial('1')
                self.assertFalse(s.ready)
                self.assertEqual(c.reconcile()['data']['orders'][0]['phase'],'verified')
            self.assertEqual(len(a.sent),1)
    def test_inactive_wrong_magic_and_minimum_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a=FillingFake();s=self.session(a,d+'/state.db')
            with self.assertRaises(RuntimeError):NativeRecoveryControl(s,magic=123)
            with s:
                with self.assertRaises(ValueError):NativeRecoveryControl(s,magic=456)
                c=NativeRecoveryControl(s,magic=123)
                with self.assertRaises(ValueError):c.stage_initial_ranges(RANGES,initial_sl_distance='.59')
                self.assertIsNone(c.state()['data'])
            with self.assertRaises(RuntimeError):c.state()
            self.assertEqual(a.sent,[])

    def test_custom_config_survives_restart_and_caller_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            a=FillingFake();path=d+'/state.db'
            ranges=[dict(RANGES[0]), {'id':'2','side':'SELL','price':'3950','volume':'.01'}]
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123)
                c.stage_initial_ranges(ranges,initial_sl_distance='1.20',reentry_distance='.35')
                expected=c.strategy_config()
                ranges[0]['price']='1'
                returned=c.strategy_config();returned['settings']['reentry_distance']='99'
            with self.session(a,path) as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile()
                self.assertEqual(c.strategy_config(),expected)
                self.assertEqual(expected['settings'],{'initial_sl_distance':'1.20','reentry_distance':'0.35'})
                self.assertEqual(expected['ranges'][0]['price'],'4000')
                self.assertEqual(len(expected['ranges']),2)
            self.assertEqual(a.sent,[])

    def test_legacy_config_not_inferred_from_orders(self):
        with tempfile.TemporaryDirectory() as d:
            a=FillingFake()
            with self.session(a,d+'/state.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES)
                data=c._read();del data['strategy_config'];del data['schema_version'];c._save(data)
                with self.assertRaises(RuntimeError):c.strategy_config()
                with self.assertRaises(RuntimeError):c.submit_initial('1')
                self.assertEqual(c.state()['data']['orders'][0]['phase'],'staged')
                # Existing dispatch receipts remain readable for reconciliation.
                c.reconcile()
            self.assertEqual(a.sent,[])
