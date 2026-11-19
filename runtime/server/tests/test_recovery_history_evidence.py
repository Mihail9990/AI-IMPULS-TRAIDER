import unittest
from datetime import datetime, timezone
import test_recovery_pipeline as fixtures


class HistoryEvidenceTests(unittest.TestCase):
    def make_open(self):
        fixture=fixtures.PipelineTests()
        c,a,p=fixture.make()
        fixture.enter(c,a,p)
        return c,a,p

    def test_account_switch_during_history_does_not_mutate_ledger(self):
        fixture=fixtures.PipelineTests();c,a,p=fixture.make()
        p.sync_pending();a.fill();before=c.state()
        def history(start,end):
            a.login=2
            return [a.deal()]
        a.history_deals_get=history
        with self.assertRaisesRegex(ValueError,'Account/server changed'):
            p.poll_history(datetime.now(timezone.utc))
        self.assertEqual(c.state(),before)
        self.assertEqual(c.data['deals'],{})
        self.assertNotIn('observer_cursor_msc',c.data)

    def test_external_stop_change_blocks_classification_and_count(self):
        c,a,p=self.make_open();d=a.deal(101,1,a.DEAL_REASON_SL)
        d.sl=3999.8
        with self.assertRaisesRegex(ValueError,'SL evidence'):
            p.observe_mt5_deals([d])
        self.assertNotIn('101',c.data['deals'])
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
        self.assertEqual(len(c.engine().data['positions']),1)
        self.assertIn('1',c.data['blocked'])

    def test_execution_slippage_does_not_change_full_sl_classification(self):
        c,a,p=self.make_open();d=a.deal(101,1,a.DEAL_REASON_SL)
        d.sl=3999.3;d.price=3999.1
        p.observe_mt5_deals([d])
        self.assertEqual(c.engine()._range('1')['full_sl_count'],1)

    def test_verified_support_sl_evidence(self):
        c,a,p=self.make_open();a.ask=4000.7;p.protect_quote('4000.5','4000.7')
        d=a.deal(101,1,a.DEAL_REASON_SL);d.sl=4000;d.price=3999.9
        p.observe_mt5_deals([d])
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
        self.assertTrue(any(o['kind']=='reentry' for o in c.engine().data['orders']))

    def test_invalid_sl_evidence_blocks(self):
        for sl in (0,-1,None,'NaN','Infinity'):
            with self.subTest(sl=sl):
                c,a,p=self.make_open();d=a.deal(101,1,a.DEAL_REASON_SL);d.sl=sl
                with self.assertRaisesRegex(ValueError,'SL evidence'):
                    p.observe_mt5_deals([d])
                self.assertNotIn('101',c.data['deals'])
                self.assertIn('1',c.data['blocked'])
