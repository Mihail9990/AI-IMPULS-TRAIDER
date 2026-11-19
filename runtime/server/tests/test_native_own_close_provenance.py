import tempfile
import unittest
from copy import deepcopy
from recovery.native_close_ledger import NativeCloseLedger
from recovery.native_close_range import NativeCloseRange
from recovery.native_entry_observer import NativeEntryObserver
from recovery.native_entry_protection import NativeEntryProtection
from recovery.native_initial_reverse import NativeInitialReverse
from recovery.native_recovery_control import NativeRecoveryControl
from test_native_close_range import CloseFake
from test_native_recovery_control import RANGES
import test_native_recovery_control as controls


class ExpertCloseFake(CloseFake):
    DEAL_REASON_EXPERT=3
    def order_send(self,r):
        result=super().order_send(r)
        if r['action']==self.TRADE_ACTION_DEAL:self.deals[-1].reason=self.DEAL_REASON_EXPERT
        return result


class OwnCloseProvenanceTests(unittest.TestCase):
    session=controls.ControlTests.session
    def setup_control(self,a,path):
        s=self.session(a,path);s.__enter__();self.addCleanup(s.close)
        c=NativeRecoveryControl(s,magic=123);c.stage_initial_ranges(RANGES);c.submit_initial('1')
        a.fill_order(51,88);a.ask=4000.21;NativeEntryProtection(c).initial('1')
        NativeInitialReverse(c).arm('1')
        return s,c
    def test_verified_explicit_expert_close_is_manual_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as d:
            a=ExpertCloseFake(d);s,c=self.setup_control(a,d+'/s.db')
            self.assertEqual(NativeCloseRange(c).close_and_cancel('1')['phase'],'verified')
            ledger=NativeCloseLedger(c);row=ledger.record(51)
            self.assertEqual(row['classification'],'manual')
            self.assertEqual(len(row['own_close_resolutions']),1)
            self.assertEqual(ledger.totals()['1']['full_sl_count'],0)
            count=len(a.sent);s.close()
            with self.session(a,d+'/s.db') as s:
                c=NativeRecoveryControl(s,magic=123);ledger=NativeCloseLedger(c)
                self.assertEqual(ledger.record(51),row)
                self.assertEqual(ledger.totals()['1']['classification_pending_count'],0)
            self.assertEqual(len(a.sent),count)
    def test_external_expert_close_without_our_action_is_unclassified(self):
        with tempfile.TemporaryDirectory() as d:
            a=ExpertCloseFake(d);s,c=self.setup_control(a,d+'/s.db')
            a.close(88,reason=a.DEAL_REASON_EXPERT);NativeEntryObserver(c).observe('1')
            row=NativeCloseLedger(c).record(51)
            self.assertIsNone(row['classification']);self.assertEqual(row['own_close_resolutions'],{})
            self.assertEqual([o.ticket for o in a.orders],[52])
    def test_changed_receipt_order_or_scope_cannot_prove_own_close(self):
        for change in ('receipt','scope'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as d:
                a=ExpertCloseFake(d);s,c=self.setup_control(a,d+'/s.db')
                NativeCloseRange(c).close_and_cancel('1')
                action=next(x for x in c._read()['range_close_commands']['1']['actions']
                            if x['request']['action']=='TRADE_ACTION_DEAL')
                if change=='receipt':
                    row=s.executor.read(action['key']);receipt=deepcopy(row['receipt']);receipt['order']+=1
                    s.executor._update(action['key'],'VERIFIED',receipt,row['evidence'])
                else:
                    data=c._read();data['range_close_commands']['1']['actions'][-1]['scope']['identifier']=999;c._save(data)
                with self.assertRaises(ValueError):NativeCloseLedger(c).record(51)
                self.assertNotIn('close_ledger',c._read())
