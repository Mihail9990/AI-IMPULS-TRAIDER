import tempfile
import unittest
from types import SimpleNamespace as N
from unittest.mock import patch
from recovery.native_recovery_control import NativeRecoveryControl
from recovery.native_entry_protection import NativeEntryProtection
import test_native_recovery_control as controls
from test_native_recovery_control import RANGES
from test_native_entry_observer import EntryFake


class ProtectFake(EntryFake):
    def order_send(self,r):
        if r['action'] != self.TRADE_ACTION_SLTP:return super().order_send(r)
        self.sent.append(r)
        self.positions[0].sl=r['sl'];self.positions[0].tp=r['tp']
        return N(retcode=self.TRADE_RETCODE_DONE,order=0,deal=0)


class ProtectionTests(unittest.TestCase):
    session=controls.ControlTests.session
    def build(self,a,path,price=4000):
        s=self.session(a,path);s.__enter__();self.addCleanup(s.close)
        c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
        a.fill(.01,price=price);a.ask=4000.21
        return s,c,NativeEntryProtection(c)
    def test_existing_initial_stop_then_bid_support_stages_tp_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,p=self.build(a,d+'/s.db');a.positions[0].tp=4005
            self.assertEqual(p.initial('1')['stage'],0);self.assertEqual(len(a.sent),1)
            a.ask=4000.44 # bid 4000.24: no support yet
            self.assertEqual(p.support('1')['stage'],0);self.assertEqual(len(a.sent),1)
            a.ask=4000.46
            self.assertEqual(p.support('1')['phase'],'verified')
            self.assertEqual(a.positions[0].sl,3999.75);self.assertEqual(a.positions[0].tp,4005)
            a.ask=4000.71
            self.assertEqual(p.support('1')['phase'],'verified')
            self.assertEqual(a.positions[0].sl,4000)
            self.assertEqual(c.state()['data']['confirmed_protection']['88']['stage'],2)
            p.support('1');self.assertEqual(len(a.sent),3)
    def test_actual_fill_tightens_initial_stop(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,p=self.build(a,d+'/s.db',price=4000.05)
            self.assertEqual(p.initial('1')['phase'],'verified')
            self.assertEqual(a.positions[0].sl,3999.35)
            p.initial('1');self.assertEqual(len(a.sent),2)
    def test_external_stop_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,p=self.build(a,d+'/s.db');a.positions[0].sl=3999.4
            with self.assertRaises(ValueError):p.initial('1')
            self.assertEqual(len(a.sent),1)
    def test_before_send_crash_blocks_restart_without_resend(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,p=self.build(a,d+'/s.db',price=4000.05)
            with patch.object(s,'execute',side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):p.initial('1')
            key=next(iter(c.state()['data']['protection_commands']));s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);c.reconcile();self.assertFalse(s.ready)
                p=NativeEntryProtection(c);self.assertEqual(p.reconcile(key)['phase'],'unresolved')
                self.assertFalse(s.ready)
            self.assertEqual(len(a.sent),1)
    def test_sell_uses_ask_and_mirrored_support_levels(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123)
                c.stage_initial_ranges([{'id':'1','side':'SELL','price':'3950','volume':'.01'}])
                c.submit_initial('1');a.fill(.01,price=3950)
                a.deals[0].type=1;a.positions[0].type=1;a.positions[0].sl=3950.7
                a.ask=3949.9;p=NativeEntryProtection(c);p.initial('1')
                a.ask=3949.76;p.support('1');self.assertEqual(len(a.sent),1)
                a.ask=3949.74;p.support('1');self.assertEqual(a.positions[0].sl,3950.25)
                a.ask=3949.49;p.support('1');self.assertEqual(a.positions[0].sl,3950)
    def test_verified_sl_survives_failed_strategy_commit(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,p=self.build(a,d+'/s.db',price=4000.05)
            original=c._save;calls=[]
            def fail(data):
                calls.append(1)
                if len(calls)==3:raise OSError('after verified SL')
                original(data)
            with patch.object(c,'_save',side_effect=fail):
                with self.assertRaises(OSError):p.initial('1')
            key=next(iter(c.state()['data']['protection_commands']));s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);p=NativeEntryProtection(c)
                self.assertEqual(p.reconcile(key)['phase'],'verified')
                self.assertEqual(p.initial('1')['stage'],0)
            self.assertEqual(len(a.sent),2)
    def test_receipt_proven_initial_slippage_correction_can_widen(self):
        with tempfile.TemporaryDirectory() as d:
            a=ProtectFake();s,c,p=self.build(a,d+'/s.db',price=3999.95)
            self.assertEqual(p.initial('1')['phase'],'verified')
            self.assertEqual(a.positions[0].sl,3999.25)
            self.assertEqual(c.state()['data']['confirmed_protection']['88']['stage'],0)
            self.assertEqual(len(a.sent),2)
