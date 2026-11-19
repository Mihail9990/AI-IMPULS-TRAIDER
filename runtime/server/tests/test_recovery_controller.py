import tempfile
import unittest
from decimal import Decimal as D
from recovery.controller import Controller
from recovery.engine import Engine

class ControllerTests(unittest.TestCase):
    def make(self,path=':memory:'):
        e=Engine();e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}])
        c=Controller(e,path,test_mode=True)
        c.bind_verified_order(e.data['orders'][0]['id'],100,account=1,server='demo',symbol='XAUUSD.s',magic=123)
        return c,e
    def ev(self,deal=1,entry='IN',volume='.01',price='4000',pnl='0',reason='initial_sl'):
        return dict(deal_id=deal,entry_order_ticket=100,account=1,server='demo',symbol='XAUUSD.s',magic=123,
                    position_identifier=500,entry=entry,side='BUY',volume=volume,price=price,
                    realized_net=pnl,close_reason=reason)
    def test_hard_mock_lock(self):
        with self.assertRaises(RuntimeError):Controller(Engine())
    def test_partial_entries_weighted_once(self):
        c,_=self.make();c.deal(self.ev(volume='.004',price='4000'))
        self.assertEqual(c.state()['bindings']['100']['phase'],'partial_entry')
        self.assertEqual(c.engine().data['positions'],[])
        e=self.ev(deal=2,volume='.006',price='4001');c.deal(e);c.deal(e)
        p=c.engine().data['positions'][0]
        self.assertEqual(D(p['entry']),D('4000.6'));self.assertEqual(D(p['sl']),D('3999.9'))
        self.assertEqual(len(c.engine().data['positions']),1)
    def test_partial_exit_only_counts_when_fully_closed(self):
        c,_=self.make();c.deal(self.ev(pnl='-.10'))
        c.deal(self.ev(2,'OUT','.004','3999.3','-.4'))
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
        self.assertEqual(c.state()['bindings']['100']['phase'],'partial_exit')
        self.assertEqual(len(c.engine().data['positions']),1)
        c.deal(self.ev(3,'OUT','.006','3999.2','-.6'))
        self.assertEqual(c.engine()._range('1')['full_sl_count'],1)
        self.assertEqual(D(c.engine()._range('1')['loss']),D('1.1'))
        self.assertEqual(c.engine().data['positions'],[])
    def test_restart_deduplicates(self):
        with tempfile.TemporaryDirectory() as d:
            c,e=self.make(d+'/c.db');event=self.ev();c.deal(event);before=c.state();c.db.close()
            c=Controller(e,d+'/c.db',test_mode=True);c.deal(event)
            self.assertEqual(c.state(),before);c.db.close()
    def test_exit_before_full_entry_is_persisted_and_blocked(self):
        c,_=self.make();c.deal(self.ev(volume='.004'))
        c.deal(self.ev(2,'OUT','.004','3999','-1'))
        self.assertIn('1',c.state()['blocked']);self.assertEqual(len(c.data['deals']),2)
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
    def test_mixed_close_reasons_do_not_guess_counter(self):
        c,_=self.make();c.deal(self.ev())
        c.deal(self.ev(2,'OUT','.004','3999','-.4','manual'))
        c.deal(self.ev(3,'OUT','.006','3999','-.6','initial_sl'))
        self.assertIn('Mixed',c.state()['blocked']['1'])
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
    def test_foreign_deal_does_not_mutate(self):
        c,_=self.make();before=c.state()
        with self.assertRaises(ValueError):c.deal(dict(self.ev(),magic=999))
        self.assertEqual(c.state(),before)
    def test_overfill_is_recorded_not_dropped(self):
        c,_=self.make();c.deal(self.ev(volume='.02'))
        self.assertIn('1',c.state()['blocked']);self.assertEqual(len(c.data['deals']),1)
        self.assertEqual(c.engine().data['positions'],[])
    def test_support_close_not_full_stop(self):
        c,_=self.make();c.deal(self.ev());c.quote('4000.5','4000.7')
        c.deal(self.ev(2,'OUT','.01','4000','0','support_sl'))
        self.assertEqual(c.engine()._range('1')['full_sl_count'],0)
        self.assertTrue(any(o['kind']=='reentry' for o in c.engine().data['orders']))
    def test_second_ticket_for_same_logical_order_rejected(self):
        c,e=self.make();before=c.state();logical=e.data['orders'][0]['id']
        with self.assertRaisesRegex(ValueError,'another broker ticket'):
            c.bind_verified_order(logical,101,account=1,server='demo',symbol='XAUUSD.s',magic=123)
        self.assertEqual(c.state(),before)
        c.deal(self.ev(volume='.005'))
        with self.assertRaises(ValueError):
            c.deal(dict(self.ev(2,volume='.005'),entry_order_ticket=101,position_identifier=501))
        self.assertEqual(c.engine().data['positions'],[])
        self.assertEqual(c.state()['bindings']['100']['observed_open_volume'],'0.005')
    def test_identifier_change_does_not_merge_partial_positions(self):
        c,_=self.make();c.deal(self.ev(volume='.005'));before=c.state()
        with self.assertRaisesRegex(ValueError,'identifier changed'):
            c.deal(dict(self.ev(2,volume='.005'),position_identifier=501))
        self.assertEqual(c.state(),before);self.assertEqual(c.engine().data['positions'],[])
    def test_quote_db_failure_rolls_back_memory(self):
        c,_=self.make();c.deal(self.ev());before=c.state()
        c.db.close()
        with self.assertRaises(Exception):c.quote('4000.50','4000.70')
        self.assertEqual(c.state(),before)
    def test_binding_db_failure_rolls_back_memory(self):
        e=Engine();e.replace_ranges([{'id':'1','price':4000,'side':'BUY','volume':'.01'}])
        c=Controller(e,test_mode=True);before=c.state();c.db.close()
        with self.assertRaises(Exception):
            c.bind_verified_order(e.data['orders'][0]['id'],100,account=1,server='demo',symbol='XAUUSD.s',magic=123)
        self.assertEqual(c.state(),before)
