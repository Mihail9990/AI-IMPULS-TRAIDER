from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_basket_latest_close as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController
from tradfi.control import ActionRejected,ActionGateway

class DragTP(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
    def basket(self,side='SELL'):
        self.case.first_sl('BUY' if side=='SELL' else 'SELL');self.case.fill_basket(side);self.f.tick(2)
    def ticket(self,index):return next(t for t in self.f.bot.state['cycle']['tickets'].values() if t['role']!='initial' and t['index']==index and not t['closed'])
    def action(self,index,distance):
        t=self.ticket(index);p=next(p for p in self.f.b.data['positions'] if p['id']==t['id']);sign=1 if t['side']=='BUY' else -1
        return dict(type='protection',cycle_id=self.f.bot.state['cycle']['id'],position_id=t['id'],old=dict(sl=p['sl'],tp=p['tp']),new=dict(sl=p['sl'],tp=str(D(t['entry'])+sign*D(distance))))
    def drag(self,index,distance):self.f.bot.action(self.action(index,distance));self.f.tick(5)
    def prices(self):return {p['id']:p['tp'] for p in self.f.b.data['positions']}
    def offsets(self):return [(D(p['tp'])-D(p['entry']))*(1 if p['side']=='BUY' else -1) for p in self.f.b.data['positions']]
    def assert_cover(self):
        ps=self.f.b.data['positions'];profit=sum(self.f.b.profit(p['side'],D(p['size']),D(p['entry']),D(p['tp'])) for p in ps)
        self.assertGreaterEqual(profit,D(self.f.bot.state['cycle']['recovery']))
    def test_drag_interpolates_free_slots_and_mirrors(self):
        self.basket();before=self.prices();self.drag(2,'.40');offsets=self.offsets()
        self.assertEqual(offsets[:3],[D('.2'),D('.3'),D('.4')]);self.assert_cover();self.assertNotEqual(before,self.prices())
    def test_buy_mirror(self):
        self.basket('BUY');self.drag(2,'.40');self.assertEqual(self.offsets()[:3],[D('.2'),D('.3'),D('.4')]);self.assert_cover()
    def test_prior_drag_fixed_after_next_drag_and_restart(self):
        self.basket();self.drag(3,'.65');price=self.prices()[self.ticket(3)['id']]
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.drag(2,'.50')
        self.assertEqual(self.prices()[self.ticket(3)['id']],price);self.assertEqual(self.offsets()[:4],[D('.2'),D('.35'),D('.5'),D('.65')])
    def test_settings_manual_slot_preserved(self):
        self.f.bot.state['tp_distances'][3]='.62';self.basket();price=self.prices()[self.ticket(3)['id']]
        self.drag(2,'.50');self.assertEqual(self.prices()[self.ticket(3)['id']],price)
    def test_partial_tp_uses_only_remaining_debt_and_preserves_sl(self):
        self.basket();pid=self.ticket(0)['id'];self.f.b.close('TP',position_id=pid,commission='-.03');self.f.tick(5)
        before_sl={p['id']:p['sl'] for p in self.f.b.data['positions']};before=len(self.f.b.commands)
        self.drag(2,'.40');self.assert_cover()
        self.assertEqual({p['id']:p['sl'] for p in self.f.b.data['positions']},before_sl)
        self.assertFalse(any(c.get('position_id')==pid for c in self.f.b.commands[before:]))
        self.assertEqual(D(self.f.bot.state['cycle']['recovery']),D('3.03'))
    def test_noncontiguous_closed_part_never_receives_modify(self):
        self.basket();pid=self.ticket(1)['id'];self.f.b.close('TP',position_id=pid);self.f.tick(2)
        entry=D(self.ticket(0)['entry']);self.f.b.quote(entry-D('.05'),entry+D('.05'))
        before=len(self.f.b.commands);self.drag(2,'.50');self.assert_cover()
        self.assertFalse(any(c.get('position_id')==pid for c in self.f.b.commands[before:]))
    def test_impossible_interpolation_rejected_before_effects(self):
        self.basket();before=deepcopy(self.f.bot.state);commands=len(self.f.b.commands)
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action(2,'.41'))
        self.assertEqual(before,self.f.bot.state);self.assertEqual(commands,len(self.f.b.commands))
    def test_fully_fixed_insufficient_grid_rejects(self):
        self.basket();g=next(g for g in self.f.bot.state['cycle']['groups'] if g.get('activated')=='SELL')
        g['manual_tp_distances']={str(i):str(D('.2')+D('.02')*i) for i in range(6)}
        before=self.prices()
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action(5,'.32'))
        self.assertEqual(before,self.prices())
    def test_close_during_staged_edit_halts(self):
        self.basket();self.f.bot.action(self.action(2,'.40'));self.f.b.close('TP',position_id=self.ticket(1)['id']);self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'EDIT_FILL_OR_CLOSE_RACE')
    def test_unknown_restart_no_duplicate(self):
        self.basket();self.f.bot.action(self.action(2,'.40'));self.f.b.fail_before.add('MODIFY');self.f.tick(3)
        count=len(self.f.b.commands);self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(4)
        self.assertEqual(count,len(self.f.b.commands));self.assertIsNotNone(self.f.bot.state['cycle']['edit'])
    def test_receipt_duplicate_executes_once_and_read_only_rejects(self):
        self.basket();gate=ActionGateway(self.f.bot,execute=self.f.bot.action);request=dict(id='drag-tp-duplicate',revision=0,action=self.action(2,'.40'))
        first=gate.apply(request);self.assertEqual(first['status'],'APPLIED');self.assertEqual(gate.apply(request),first);self.f.tick(5)
        before=len(self.f.b.commands);self.assertEqual(gate.apply(request),first);self.f.tick(2);self.assertEqual(before,len(self.f.b.commands))
        self.f.b.read_only=True
        with self.assertRaises(ActionRejected):self.f.bot.action(self.action(2,'.50'))
    def test_ack_without_readback_pending(self):
        self.basket();self.f.bot.action(self.action(2,'.40'));self.f.bot._advance_edit(self.f.bot.snapshot())
        e=self.f.bot.state['cycle']['edit']
        for cid in e['modify_ids']:self.f.bot.state['commands'][cid]['status']='DONE'
        self.assertTrue(self.f.bot._advance_edit(self.f.bot.snapshot()));self.assertNotIn('manual_tp_distances',next(g for g in self.f.bot.state['cycle']['groups'] if g.get('activated')=='SELL'))
    def test_partial_reject_does_not_claim_grid_success(self):
        self.basket();self.f.bot.action(self.action(2,'.40'));original=self.f.b.submit;count=[0]
        def submit(cmd):
            if cmd['kind']=='MODIFY':
                count[0]+=1
                if count[0]==2:
                    self.f.b.commands.append(deepcopy(cmd));self.f.b.results[cmd['id']]={'status':'REJECTED'};return
            original(cmd)
        self.f.b.submit=submit;self.f.tick(5)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'EDIT_PARTIAL_FAILURE_REVIEW_BROKER_TRUTH')
    def test_external_sl_change_before_dispatch_never_overwritten(self):
        self.basket();self.f.bot.action(self.action(2,'.40'));p=self.f.b.data['positions'][1]
        p['sl']=str(D(p['sl'])-D('.10'));expected=p['sl'];before=len(self.f.b.commands)
        self.f.tick(4)
        self.assertEqual(p['sl'],expected);self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'EDIT_PROTECTION_CHANGED')
        self.assertFalse(any(c['kind']=='MODIFY' for c in self.f.b.commands[before:]))
    def test_later_grid_settings_preserve_chart_fixed_target(self):
        self.basket();self.drag(2,'.40');pid=self.ticket(2)['id'];price=self.prices()[pid]
        self.f.bot.edit_tp_distances(['.30']+[None]*19);self.f.tick(5)
        self.assertEqual(self.prices()[pid],price)
