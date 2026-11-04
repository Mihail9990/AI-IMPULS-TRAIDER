from copy import deepcopy
from decimal import Decimal as D
import unittest
from unittest.mock import patch
import test_basket_latest_close as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController
from tradfi.control import ActionRejected,ActionGateway
from tradfi.basket import BasketController

class V2Integration(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
    def values(self,*values):return list(values)+[None]*(20-len(values))
    def edit(self,values):
        c=self.f.bot.state.get('cycle')
        return self.f.bot.action(dict(type='tp_distances',cycle_id=c['id'] if c else None,distances=values))
    def basket(self):self.case.first_sl();self.case.fill_basket();self.f.tick(2)
    def test_missing_first_rejected_before_any_order_then_stop(self):
        self.f.bot.state['tp_distances']=[None]*20;gate=ActionGateway(self.f.bot)
        r=gate.apply(dict(id='missing-first',revision=0,action=dict(type='command',text='/start')))
        self.assertEqual(r['status'],'REJECTED');self.assertFalse(self.f.b.commands)
        self.assertEqual(gate.apply(dict(id='stop-after-reject',revision=1,action=dict(type='command',text='/stop')))['status'],'APPLIED')
    def test_settings_persist_and_initial_still_ordinary(self):
        self.edit(self.values('.50',None,'1.50'));self.f.bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.f.start();p=self.f.b.data['orders'][0]
        self.assertEqual(D(p['tp'])-D(p['price']),D('.25'));self.assertEqual(self.f.bot.state['cycle']['tp_distances'][2],'1.50')
    def test_invalid_values_no_effects(self):
        for values in ([None]*20,self.values('NaN'),self.values('0.205'),self.values('.5','.4'),self.values('.20',None,'.23'),['.5']):
            before=deepcopy(self.f.bot.state)
            with self.assertRaises(ActionRejected):self.edit(values)
            self.assertEqual(before,self.f.bot.state)
        self.assertFalse(self.f.b.commands)
    def test_read_only_and_stale_cycle_rejected(self):
        self.f.b.read_only=True
        with self.assertRaises(ActionRejected):self.edit(self.values('.5'))
        self.f.b.read_only=False
        with self.assertRaises(ActionRejected):self.f.bot.action(dict(type='tp_distances',cycle_id='other',distances=self.values('.5')))
    def test_active_grid_modification_preserves_actual_sl_and_restart(self):
        self.basket();before={p['id']:p['sl'] for p in self.f.b.data['positions']}
        self.edit(self.values('.30',None,'.50'));self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(5)
        self.assertIsNone(self.f.bot.state['cycle'].get('edit'))
        ps=self.f.b.data['positions'];self.assertEqual([D(p['entry'])-D(p['tp']) for p in ps[:3]],[D('.3'),D('.4'),D('.5')])
        self.assertEqual(before,{p['id']:p['sl'] for p in ps})
        self.assertEqual(self.f.bot.state['tp_distances'][0],'0.30')
    def test_pending_replacement_uses_new_grid_and_safe_cancel(self):
        self.case.first_sl();old={p['id'] for p in self.f.b.data['orders']}
        self.edit(self.values('.30'));self.f.tick(6)
        self.assertEqual(len(self.f.b.data['orders']),12);self.assertFalse(old&{p['id'] for p in self.f.b.data['orders']})
        self.assertEqual(self.f.bot.state['cycle']['tp_distances'][0],'0.30')
    def test_fill_during_grid_cancel_halts_no_second_basket(self):
        self.case.first_sl();self.edit(self.values('.30'));self.f.b.cancel_race='SELL';self.f.tick(6)
        self.assertTrue(self.f.bot.state['cycle'].get('manual'));self.assertTrue(self.f.b.data['positions'])
        self.assertLessEqual(len(self.f.b.data['positions']),6)
    def test_unknown_modify_no_blind_retry_across_restart(self):
        self.basket();self.edit(self.values('.30'));self.f.b.fail_before.add('MODIFY');self.f.tick(4)
        count=len(self.f.b.commands);self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(4)
        self.assertEqual(len(self.f.b.commands),count);self.assertIsNotNone(self.f.bot.state['cycle']['edit'])
    def test_ack_without_readback_does_not_commit_grid(self):
        self.basket();self.edit(self.values('.30'));self.f.bot._advance_edit(self.f.bot.snapshot())
        edit=self.f.bot.state['cycle']['edit'];self.assertTrue(edit['modify_ids'])
        for cid in edit['modify_ids']:self.f.bot.state['commands'][cid]['status']='DONE'
        self.assertTrue(self.f.bot._advance_edit(self.f.bot.snapshot()))
        self.assertEqual(self.f.bot.state['cycle']['tp_distances'][0],'.20')
    def test_partial_tp_grid_rejected_without_redistribution(self):
        self.basket();self.f.b.close('TP',position_id=self.f.b.data['positions'][0]['id']);self.f.tick(5)
        before=deepcopy(self.f.b.data['positions'])
        with self.assertRaises(ActionRejected):self.edit(self.values('.4'))
        self.assertEqual(before,self.f.b.data['positions'])
    def test_old_active_cycle_blocked_preserves_debt_and_positions(self):
        self.f.bot=BasketController(self.f.store,self.f.b);self.f.start();before=deepcopy(self.f.b.data)
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.assertEqual(self.f.bot.state['phase'],'MIGRATION_BLOCK');self.assertEqual(before,self.f.b.data)
        with self.assertRaises(ActionRejected):self.f.bot.command('/start')
    def test_retired_profit_options_cannot_restore_old_ladder(self):
        settings=dict(self.f.bot.state['basket_settings'],TP_STEP_MULTIPLIER='2')
        with self.assertRaises(ActionRejected):self.f.bot.edit(settings=settings)
        self.assertFalse(self.f.b.commands)
    def test_actual_ladder_requires_readback(self):
        self.basket();g=next(g for g in self.f.bot.state['cycle']['groups'] if g.get('actual_ladder_tx'))
        tx=g['actual_ladder_tx'];tx['status']='PENDING';tx['targets'][0]['tp']=str(D(tx['targets'][0]['tp'])-D('.01'))
        self.assertTrue(self.f.bot._advance_actual_ladder(self.f.bot.snapshot(),g))
        self.assertEqual(tx['status'],'PENDING')
    def test_partial_gap_exit_before_actual_ladder_halts(self):
        self.basket();c=self.f.bot.state['cycle'];g=next(g for g in c['groups'] if g.get('actual_ladder_tx'))
        g['ladder_applied']=False;g.pop('actual_ladder_tx')
        t=next(t for t in c['tickets'].values() if t['group']==g['id']);t['closed']=True
        self.f.bot._live_ladder(self.f.bot.snapshot(),g)
        self.assertEqual(c['manual_reason'],'ACTUAL_LADDER_GAP_EXIT_REQUIRES_REVIEW')
    def test_initial_tp_default_and_custom_value_survive_restart(self):
        from tradfi.basket_math import BasketSettings
        self.assertEqual(D(BasketSettings().INITIAL_TP_DISTANCE),D('0.25'))
        settings=dict(self.f.bot.state['basket_settings'],INITIAL_TP_DISTANCE='0.40')
        self.f.bot.edit(settings=settings)
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.f.start();order=self.f.b.data['orders'][0]
        self.assertEqual(D(order['tp'])-D(order['price']),D('0.40'))
        self.assertEqual(self.f.bot.state['basket_settings']['INITIAL_TP_DISTANCE'],'0.40')
