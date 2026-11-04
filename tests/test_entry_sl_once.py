from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_basket_latest_close as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController
from tradfi.control import ActionRejected

class ManualStageController(LatestCloseBasketController):
    def _stage_first_tp_if_ready(self,snap):return False

class EntrySLOnce(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.case.first_sl();self.case.fill_basket();self.f=self.case.f
        self.f.bot=ManualStageController(self.f.store,self.f.b)
        c=self.f.bot.state['cycle'];self.group=next(g for g in c['groups'] if g.get('activated')=='SELL')
        self.first=min((t for t in c['tickets'].values() if t['group']==self.group['id']),key=lambda t:t['index'])
    def trigger(self):
        self.f.b.close('TP',position_id=self.first['id']);self.f.tick(2)
    def stage(self):self.f.bot.stage_entry_sl_once(self.group['id'],self.first['id'])
    def status(self):return next(g for g in self.f.bot.state['cycle']['groups'] if g['id']==self.group['id'])['entry_sl_once']['status']
    def test_confirmed_one_shot_then_manual_edit_persists(self):
        self.trigger();self.stage();self.f.tick(4);self.assertEqual(self.status(),'DONE')
        for p in self.f.b.data['positions']:self.assertEqual(D(p['sl']),D(p['entry']))
        p=self.f.b.data['positions'][0];new=str(D(p['sl'])+D('.10'))
        self.f.bot.edit(protection=dict(position_id=p['id'],old=dict(sl=p['sl'],tp=p['tp']),new=dict(sl=new,tp=p['tp'])))
        self.f.tick(4);self.assertEqual(p['sl'],new)
        before=len(self.f.b.commands);self.stage();self.f.tick(4);self.assertEqual(p['sl'],new);self.assertEqual(len(self.f.b.commands),before)
    def test_no_unconfirmed_trigger(self):
        with self.assertRaises(ActionRejected):self.stage()
        self.assertNotIn('entry_sl_once',self.group)
    def test_read_only_reject(self):
        self.trigger();self.f.b.read_only=True
        with self.assertRaises(ActionRejected):self.stage()
    def test_bad_quote_rejects_all_before_intents(self):
        self.trigger();self.f.b.quote('4000','4000.1');before=len(self.f.b.commands)
        with self.assertRaises(ActionRejected):self.stage()
        self.assertEqual(len(self.f.b.commands),before)
    def test_restart_prepared(self):
        self.trigger();self.stage();self.f.bot=ManualStageController(self.f.store,self.f.b);self.f.tick(4)
        self.assertEqual(self.status(),'DONE')
    def test_pending_unknown_no_duplicate_and_edit_blocked(self):
        self.trigger();self.stage();self.f.b.fail_before.add('MODIFY');self.f.tick(3)
        before=len(self.f.b.commands);self.stage();self.f.tick(3);self.assertEqual(len(self.f.b.commands),before);self.assertEqual(self.status(),'PENDING')
        with self.assertRaises(ActionRejected):self.f.bot.edit(settings=self.f.bot.state['basket_settings'])
    def test_external_edit_between_prepare_and_send_not_overwritten(self):
        self.trigger();self.stage();p=self.f.b.data['positions'][0];p['sl']=str(D(p['sl'])+D('.1'));value=p['sl']
        self.f.tick(2);self.assertEqual(self.status(),'FAILED');self.assertEqual(p['sl'],value)
    def test_closed_remainder_not_modified(self):
        self.trigger();self.stage();p=self.f.b.data['positions'][0];pid=p['id'];self.f.b.close('TP',position_id=pid)
        before=len(self.f.b.commands);self.f.tick(4)
        self.assertFalse(any(c['kind']=='MODIFY' and c['position_id']==pid for c in self.f.b.commands[before:]))
        # Fresh quote may make another target too close; either fail closed or complete.
        self.assertIn(self.status(),('DONE','FAILED'))
    def test_ack_without_readback_never_done(self):
        self.trigger();self.stage();original=self.f.b.submit
        def submit(cmd):
            if cmd['kind']=='MODIFY':
                self.f.b.commands.append(deepcopy(cmd));self.f.b.results[cmd['id']]={'status':'DONE'}
            else:original(cmd)
        self.f.b.submit=submit;self.f.tick(4)
        self.assertEqual(self.status(),'PENDING')
    def test_partial_reject_preserves_actual_protection(self):
        self.trigger();self.stage();original=self.f.b.submit;count=[0]
        def submit(cmd):
            if cmd['kind']=='MODIFY':
                count[0]+=1
                if count[0]==2:
                    self.f.b.commands.append(deepcopy(cmd));self.f.b.results[cmd['id']]={'status':'REJECTED'};return
            original(cmd)
        self.f.b.submit=submit;self.f.tick(4)
        self.assertEqual(self.status(),'FAILED');self.assertTrue(self.f.bot.state['cycle'].get('manual'))
        self.assertTrue(any(D(p['sl'])==D(p['entry']) for p in self.f.b.data['positions']))
        self.assertTrue(any(D(p['sl'])!=D(p['entry']) for p in self.f.b.data['positions']))
