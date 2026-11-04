from copy import deepcopy
from decimal import Decimal as D
import unittest
from tradfi.basket_math import last_closed_exit
from tradfi.basket_latest_close import LatestCloseBasketController
import test_basket_latest_close as fixtures

class MixedTPDebt(unittest.TestCase):
    def test_mixed_final_tp_previous_main_and_restart_both_sides(self):
        for initial,side in [('BUY','SELL'),('SELL','BUY')]:
            with self.subTest(side=side):
                c=fixtures.LatestClose();c.setUp();self.addCleanup(c.doCleanups)
                c.first_sl(initial);c.fill_basket(side);c.f.tick(2);f=c.f
                main=deepcopy(f.bot.state['cycle']['main_range'])
                for i,p in enumerate(list(f.b.data['positions'])):
                    f.b.data['now']+=1
                    f.b.close('SL' if i<3 else 'TP',position_id=p['id'],commission='-1')
                mid=(D(main['BUY'])+D(main['SELL']))/2
                f.b.quote(str(mid),str(mid+D('.1')))
                f.bot=LatestCloseBasketController(f.store,f.b);f.tick(5)
                self.assertEqual(f.bot.state['cycle']['expanded'],main)
                self.assertGreater(D(f.bot.state['cycle']['recovery']),0)
                self.assertEqual(len(f.b.data['orders']),12)
                before=deepcopy(f.b.data['orders'])
                f.bot=LatestCloseBasketController(f.store,f.b);f.tick(3)
                self.assertEqual(before,f.b.data['orders'])

    def test_mixed_exit_obeys_once_and_permanent_manual_ranges(self):
        from test_manual_range_control import ManualControl
        for duration in ('once','permanent'):
            with self.subTest(duration=duration):
                c=ManualControl();c.setUp();self.addCleanup(c.doCleanups)
                c.close(duration=duration);chosen=deepcopy(c.f.bot.state['cycle']['expanded']);c.fill_next()
                for i,p in enumerate(list(c.f.b.data['positions'])):
                    c.f.b.data['now']+=1
                    c.f.b.close('SL' if i<2 else 'TP',position_id=p['id'],commission='-1')
                c.f.b.quote('3998','3998.1');c.f.tick(5)
                self.assertEqual(c.f.bot.state['cycle']['main_range'],c.main if duration=='once' else chosen)
                self.assertEqual(len(c.f.b.data['orders']),12)
                self.assertGreater(D(c.f.bot.state['cycle']['recovery']),0)

    def test_conflicting_final_reasons_halt_without_new_orders(self):
        c=fixtures.LatestClose();c.setUp();self.addCleanup(c.doCleanups)
        c.first_sl();c.fill_basket();c.f.tick(2);f=c.f
        for i,p in enumerate(list(f.b.data['positions'])):
            f.b.close('SL' if i<3 else 'TP',position_id=p['id'],commission='-1')
        f.b.quote('3998','3998.1');f.tick(5)
        self.assertEqual(f.bot.state['cycle']['manual_reason'],'LAST_RECOVERY_EXIT_UNCONFIRMED_OR_AMBIGUOUS')
        self.assertGreater(D(f.bot.state['cycle']['recovery']),0);self.assertFalse(f.b.data['orders'])

    def test_exit_evidence_requires_complete_unique_positive_facts(self):
        valid=dict(closed=True,size='.02',exit_facts=[dict(id='later-id',price='20',volume='.01',time=1,reason='SL'),dict(id='earlier-id',price='21',volume='.01',time=2,reason='TP')])
        self.assertEqual(last_closed_exit([valid]),(None,'TP'))
        valid['exit_facts'].reverse();self.assertEqual(last_closed_exit([valid]),(None,'TP'))
        invalid=[]
        t=deepcopy(valid);t['closed']=False;invalid.append(t)
        t=deepcopy(valid);t['exit_facts'].pop();invalid.append(t)
        t=deepcopy(valid);t['exit_facts'][0]['id']=t['exit_facts'][1]['id'];invalid.append(t)
        for key,value in [('time',-1),('volume','0'),('price','0'),('reason','UNKNOWN')]:
            t=deepcopy(valid);t['exit_facts'][0][key]=value;invalid.append(t)
        for t in invalid:
            with self.subTest(ticket=t),self.assertRaises(ValueError):last_closed_exit([t])

    def test_same_time_tp_prices_need_no_price_anchor_but_sl_does(self):
        t=dict(closed=True,size='.02',exit_facts=[dict(id='1',price='20',volume='.01',time=2,reason='TP'),dict(id='2',price='21',volume='.01',time=2,reason='TP')])
        self.assertEqual(last_closed_exit([t]),(None,'TP'))
        for row in t['exit_facts']:row['reason']='SL'
        with self.assertRaises(ValueError):last_closed_exit([t])
