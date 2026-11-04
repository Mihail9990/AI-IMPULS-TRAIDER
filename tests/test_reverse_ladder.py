from decimal import Decimal as D
from copy import deepcopy
from dataclasses import replace
import unittest
import test_basket_latest_close as fixtures
from tradfi.broker import Spec
from tradfi.simulator import ARTIFICIAL_SPEC
from tradfi.reverse_ladder import reverse_sl_prices
from tradfi.basket_latest_close import LatestCloseBasketController
from tradfi.control import ActionRejected,ActionGateway

class ReverseMath(unittest.TestCase):
    def setUp(self):self.spec=Spec.load(ARTIFICIAL_SPEC)
    def test_confirmed_buy_example(self):self.assertEqual(reverse_sl_prices(self.spec,'BUY','4000','4002',4),['4000.0','4000.5','4001.0','4001.5'])
    def test_sell_mirror(self):self.assertEqual(reverse_sl_prices(self.spec,'SELL','4000','3998',4),['4000.0','3999.5','3999.0','3998.5'])
    def test_single_remainder_is_anchor(self):self.assertEqual(reverse_sl_prices(self.spec,'BUY','4000','4002',1),['4000'])
    def test_no_rounding_or_crossed_direction(self):
        for args in [('BUY','4000.02','4002',4),('SELL','4000','4002',4),('BUY','4000','4000',4),('BUY','4000','4002',True)]:
            with self.assertRaises(ValueError):reverse_sl_prices(self.spec,*args)

class ReverseControl(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
    def prepare(self,side='SELL',first=True):
        self.case.first_sl('BUY' if side=='SELL' else 'SELL');self.case.fill_basket(side);self.f.tick(2)
        c=self.f.bot.state['cycle'];self.g=next(g for g in c['groups'] if g.get('activated')==side)
        tickets=sorted([t for t in c['tickets'].values() if t['group']==self.g['id']],key=lambda t:t['index']);self.anchor=D(tickets[1]['entry'])
        if first:self.f.b.close('TP',position_id=tickets[0]['id']);self.f.tick(5)
        if side=='BUY':self.f.b.quote(self.anchor+D('.25'),self.anchor+D('.35'))
        else:self.f.b.quote(self.anchor-D('.35'),self.anchor-D('.25'))
    def action(self,enabled):return dict(type='reverse_ladder',enabled=enabled,cycle_id=self.f.bot.state['cycle']['id'],expected_positions=sorted(p['id'] for p in self.f.b.data['positions']))
    def apply(self,enabled):self.f.bot.action(self.action(enabled));self.f.tick(5)
    def sls(self):
        c=self.f.bot.state['cycle'];ps=sorted(self.f.b.data['positions'],key=lambda p:c['tickets'][p['id']]['index']);return [D(p['sl']) for p in ps]
    def test_activate_buy_and_cancel_preserves_tp(self):
        self.prepare('BUY');tps=[p['tp'] for p in self.f.b.data['positions']];self.apply(True)
        self.assertEqual(self.sls(),[self.anchor+D(i)*D('.05') for i in range(5)])
        self.assertTrue(self.g['reverse_ladder']['enabled']);self.apply(False)
        self.assertEqual(self.sls(),[self.anchor]*5);self.assertFalse(self.g['reverse_ladder']['enabled']);self.assertEqual(tps,[p['tp'] for p in self.f.b.data['positions']])
    def test_activate_sell_and_restart_fixed_no_trailing(self):
        self.prepare();self.apply(True);sls=self.sls();self.f.bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.f.b.quote(self.anchor-D('.4'),self.anchor-D('.3'));self.f.tick(4)
        self.assertEqual(self.sls(),sls);self.assertEqual(sls,[self.anchor-D(i)*D('.05') for i in range(5)])
    def test_before_tp1_priority_unknown_rejected(self):
        self.prepare(first=False)
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action(True))
        self.assertNotIn('reverse_ladder',self.g)
    def test_tick_and_stop_freeze_refusal_no_modifications(self):
        self.prepare();self.f.b.quote(self.anchor-D('.36'),self.anchor-D('.26'));before=len(self.f.b.commands)
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action(True))
        self.assertEqual(before,len(self.f.b.commands))
        self.f.b.quote(self.anchor-D('.35'),self.anchor-D('.25'));self.f.b.data['spec']['freeze_level']='10'
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action(True))
        self.assertEqual(before,len(self.f.b.commands))
    def test_restart_prepared_activation_and_cancel(self):
        self.prepare();self.f.bot.action(self.action(True));self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(5)
        self.f.bot.action(self.action(False));self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(5)
        self.assertEqual(self.sls(),[self.anchor]*5)
    def test_unknown_no_duplicate_and_not_enabled_early(self):
        self.prepare();self.f.b.fail_before.add('MODIFY');self.f.bot.action(self.action(True));self.f.tick(3);before=len(self.f.b.commands)
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(3)
        self.assertEqual(before,len(self.f.b.commands));self.assertNotIn('reverse_ladder',self.g)
    def test_receipt_repeat_and_stale_ids(self):
        self.prepare();gate=ActionGateway(self.f.bot,execute=self.f.bot.action);r=dict(id='reverse-once',revision=0,action=self.action(True))
        first=gate.apply(r);self.assertEqual(first['status'],'APPLIED');self.assertEqual(gate.apply(r),first);self.f.tick(5)
        count=len(self.f.b.commands);self.assertEqual(gate.apply(r),first);self.f.tick(2);self.assertEqual(count,len(self.f.b.commands))
        a=self.action(False);a['expected_positions']=[]
        with self.assertRaises(ActionRejected):self.f.bot.action(a)
    def test_close_race_halts_without_claiming_activation(self):
        self.prepare();self.f.bot.action(self.action(True));self.f.b.close('TP',position_id=self.f.b.data['positions'][0]['id']);self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'EDIT_FILL_OR_CLOSE_RACE');self.assertNotIn('reverse_ladder',self.g)
    def test_external_sl_race_not_overwritten(self):
        self.prepare();self.f.bot.action(self.action(True));p=self.f.b.data['positions'][0];p['sl']=str(D(p['sl'])+D('.05'));expected=p['sl'];self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'EDIT_PROTECTION_CHANGED');self.assertEqual(p['sl'],expected)
    def test_read_only_cancel_rejected_protection_kept(self):
        self.prepare();self.apply(True);before=self.sls();self.f.b.read_only=True
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action(False))
        self.assertEqual(before,self.sls())
    def test_ack_without_readback_not_committed(self):
        self.prepare();self.f.bot.action(self.action(True));self.f.bot._advance_edit(self.f.bot.snapshot())
        for cid in self.f.bot.state['cycle']['edit']['modify_ids']:self.f.bot.state['commands'][cid]['status']='DONE'
        self.assertTrue(self.f.bot._advance_edit(self.f.bot.snapshot()));self.assertNotIn('reverse_ladder',self.g)
    def test_partial_rejection_preserves_broker_truth(self):
        self.prepare();self.f.bot.action(self.action(True));original=self.f.b.submit;n=[0]
        def submit(cmd):
            if cmd['kind']=='MODIFY':
                n[0]+=1
                if n[0]==2:self.f.b.commands.append(deepcopy(cmd));self.f.b.results[cmd['id']]={'status':'REJECTED'};return
            original(cmd)
        self.f.b.submit=submit;self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'EDIT_PARTIAL_FAILURE_REVIEW_BROKER_TRUTH');self.assertNotIn('reverse_ladder',self.g)
        self.assertGreater(len(set(self.sls())),1)
    def test_remaining_sl_closures_reenter_standard_range(self):
        self.prepare();self.apply(True)
        for p in sorted(list(self.f.b.data['positions']),key=lambda p:D(p['sl'])):
            self.f.b.data['now']+=1;self.f.b.close('SL',position_id=p['id'])
        self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['expanded'],{'BUY':str(self.anchor+1),'SELL':str(self.anchor-1)})

    def test_closed_second_ticket_keeps_anchor_and_remaining_index_order(self):
        self.prepare();second=next(t for t in self.f.bot.state['cycle']['tickets'].values() if t['group']==self.g['id'] and t['index']==1)
        self.f.b.close('TP',position_id=second['id']);self.f.tick(3)
        self.f.b.quote(self.anchor-D('.42'),self.anchor-D('.32'));self.apply(True)
        self.assertEqual(self.sls(),[self.anchor-D(i)*D('.08') for i in range(4)])
        self.apply(False);self.assertEqual(self.sls(),[self.anchor]*4)
    def test_cancel_after_partial_sl_only_changes_survivors(self):
        self.prepare();self.apply(True);p=self.f.b.data['positions'][2];pid=p['id']
        self.f.b.close('SL',position_id=pid);self.f.tick(3);before=len(self.f.b.commands)
        self.apply(False);self.assertEqual(self.sls(),[self.anchor]*4)
        self.assertFalse(any(c.get('position_id')==pid for c in self.f.b.commands[before:]))

class NativeReverse(unittest.TestCase):
    def test_native_evidence_activate_cancel_and_restart(self):
        case=fixtures.NativeLatestClose();case.setUp();self.addCleanup(case.doCleanups);case.first_sl();f=case.f
        for _ in range(6):f.api.fill(index=next(i for i,o in enumerate(f.api.orders) if o.type==f.api.ORDER_TYPE_SELL_STOP))
        f.tick(5);anchor=D(str(f.api.positions[1].price_open));f.api.close_position(reason=f.api.DEAL_REASON_TP)
        f.api.tick=f.api.tick._replace(bid=float(anchor-D('.35')),ask=float(anchor-D('.25')));f.tick(5)
        def action(enabled):return dict(type='reverse_ladder',cycle_id=f.bot.state['cycle']['id'],enabled=enabled,expected_positions=sorted(p['id'] for p in f.bot.snapshot()['positions']))
        f.bot.action(action(True));f.tick(5)
        self.assertEqual(sorted(D(str(p.sl)) for p in f.api.positions),sorted(anchor-D(i)*D('.05') for i in range(5)))
        f.bot=LatestCloseBasketController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now)
        f.bot.action(action(False));f.tick(5);self.assertEqual({D(str(p.sl)) for p in f.api.positions},{anchor})
