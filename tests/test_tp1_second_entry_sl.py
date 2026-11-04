from decimal import Decimal as D
import unittest
import test_basket_latest_close as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController

class TP1SecondEntry(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.case.first_sl()
        self.f=f=self.case.f
        for i in range(6):f.b.fill('SELL',price=str(D('3996.80')+D(i)/100))
        f.b.quote('3996.75','3996.85');f.tick(4)
        c=f.bot.state['cycle'];self.g=next(g for g in c['groups'] if g.get('activated')=='SELL')
        self.ts=sorted([t for t in c['tickets'].values() if t['group']==self.g['id']],key=lambda t:t['index'])
    def test_tp1_auto_uses_second_actual_entry_for_every_remaining(self):
        f=self.f;f.b.close('TP',position_id=self.ts[0]['id']);f.tick(6)
        self.assertEqual(self.g['entry_sl_once']['status'],'DONE')
        self.assertEqual(self.g['entry_sl_once']['anchor_entry'],'3996.81')
        self.assertEqual({D(p['sl']) for p in f.b.data['positions']},{D('3996.81')})
    def test_tp2_first_does_not_trigger_but_closed_second_still_anchors(self):
        f=self.f;f.b.close('TP',position_id=self.ts[1]['id']);f.tick(3)
        self.assertNotIn('entry_sl_once',self.g)
        f.b.close('TP',position_id=self.ts[0]['id']);f.tick(6)
        self.assertEqual(self.g['entry_sl_once']['status'],'DONE')
        self.assertEqual({D(p['sl']) for p in f.b.data['positions']},{D('3996.81')})
    def test_restart_after_tp1_does_not_repeat_after_manual_edit(self):
        f=self.f;f.b.close('TP',position_id=self.ts[0]['id']);f.tick(6)
        p=f.b.data['positions'][0];new=str(D(p['sl'])+D('.10'))
        f.bot.edit(protection=dict(position_id=p['id'],old=dict(sl=p['sl'],tp=p['tp']),new=dict(sl=new,tp=p['tp'])))
        f.tick(4);before=len(f.b.commands);f.bot=LatestCloseBasketController(f.store,f.b);f.tick(4)
        self.assertEqual(p['sl'],new);self.assertEqual(len(f.b.commands),before)
    def test_missing_second_not_guessed(self):
        f=self.f;f.b.close('TP',position_id=self.ts[0]['id']);f.bot._observe(f.bot.snapshot())
        f.bot.state['cycle']['tickets'][self.ts[1]['id']]['index']=9
        f.tick(3);self.assertTrue(f.bot.state['cycle'].get('manual'))
        self.assertFalse(any(c['purpose']=='ANCHOR' and c['sl']=='3996.81' for c in f.b.commands if c['kind']=='MODIFY'))

    def test_buy_basket_mirror(self):
        case=fixtures.LatestClose();case.setUp()
        try:
            case.first_sl('SELL');f=case.f
            for i in range(6):f.b.fill('BUY',price=str(D('4003.20')+D(i)/100))
            f.b.quote('4003.20','4003.30');f.tick(4)
            c=f.bot.state['cycle'];g=next(g for g in c['groups'] if g.get('activated')=='BUY')
            ts=sorted([t for t in c['tickets'].values() if t['group']==g['id']],key=lambda t:t['index'])
            f.b.close('TP',position_id=ts[0]['id']);f.tick(6)
            self.assertEqual(g['entry_sl_once']['status'],'DONE')
            self.assertEqual({D(p['sl']) for p in f.b.data['positions']},{D('4003.21')})
        finally:case.doCleanups()
