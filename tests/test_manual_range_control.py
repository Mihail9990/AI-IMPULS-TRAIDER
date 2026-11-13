from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_basket_latest_close as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController
from tradfi.control import ActionRejected,ActionGateway

class ManualControl(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.LatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
        self.case.first_sl();self.case.fill_basket();self.f.tick(2)
        original=self.f.b.submit
        def timed_submit(cmd):
            if cmd['kind']=='CLOSE':self.f.b.data['now']+=1
            return original(cmd)
        self.f.b.submit=timed_submit
        self.main=deepcopy(self.f.bot.state['cycle']['expanded'])
    def request(self,mode='from_close',duration='once',levels=None):
        return dict(type='manual_close',cycle_id=self.f.bot.state['cycle']['id'],expected_positions=sorted(p['id'] for p in self.f.b.data['positions']),mode=mode,duration=duration,same_distance='1',opposite_distance='2',levels=levels)
    def close(self,**kw):
        self.f.bot.action(self.request(**kw));self.f.tick(6)
    def fill_next(self,side='BUY'):
        for i in range(6):self.f.b.fill(side)
        entry=D(self.f.b.data['positions'][0]['entry']);self.f.b.quote(entry-D('.05'),entry+D('.05'));self.f.tick(4)
    def stop_all(self):
        for p in list(self.f.b.data['positions']):
            self.f.b.data['now']+=1;self.f.b.close('SL',position_id=p['id'])
        self.f.tick(5)
    def test_manual_close_uses_actual_last_price_mirror_sell(self):
        self.close();c=self.f.bot.state['cycle'];last=D(c['manual_close_history'][-1]['last_exit'])
        self.assertEqual(c['expanded'],{'BUY':str(last+2),'SELL':str(last-1)})
        self.assertFalse(self.f.b.data['positions']);self.assertEqual(len(self.f.b.data['orders']),12)
    def test_once_restores_previous_main_after_complete_next_basket(self):
        self.close();self.fill_next();self.stop_all();self.assertEqual(self.f.bot.state['cycle']['expanded'],self.main)
        self.assertNotIn('range_override',self.f.bot.state['cycle'])
    def test_permanent_survives_repeated_sl_and_restart(self):
        self.close(duration='permanent');levels=deepcopy(self.f.bot.state['cycle']['expanded'])
        for _ in range(2):
            self.fill_next();self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.stop_all()
            self.assertEqual(self.f.bot.state['cycle']['expanded'],levels)
    def test_restart_prepared_and_wait(self):
        self.f.bot.action(self.request());self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(1)
        count=len(self.f.b.commands);self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(5)
        self.assertEqual(len([c for c in self.f.b.commands if c['kind']=='CLOSE']),6)
        self.assertFalse(self.f.b.data['positions'])
    def test_explicit_main_and_manual_levels(self):
        self.close(mode='manual',levels={'BUY':'4000','SELL':'3995'})
        self.assertEqual(self.f.bot.state['cycle']['expanded'],{'BUY':'4000','SELL':'3995'})
    def test_main_mode_reuses_range(self):
        self.f.b.quote('3997','3997.1');self.close(mode='main',duration='permanent');self.assertEqual(self.f.bot.state['cycle']['expanded'],self.main)
    def test_read_only_stale_and_bad_settings_no_close(self):
        before=len(self.f.b.commands);self.f.b.read_only=True
        with self.assertRaises(ActionRejected):self.f.bot.action(self.request())
        self.f.b.read_only=False
        for change in (dict(cycle_id='old'),dict(expected_positions=[]),dict(same_distance='NaN'),dict(levels={'BUY':'1','SELL':'2'},mode='manual')):
            with self.assertRaises(ActionRejected):self.f.bot.action(dict(self.request(),**change))
        self.assertEqual(before,len(self.f.b.commands))
    def test_stop_before_dispatch_preserves_positions(self):
        self.f.bot.action(self.request());self.f.bot.command('/stop');self.f.tick(3)
        self.assertEqual(len(self.f.b.data['positions']),6);self.assertFalse(any(c['kind']=='CLOSE' for c in self.f.b.commands))
    def test_unknown_no_repeat_across_restart(self):
        self.f.b.fail_before.add('CLOSE');self.f.bot.action(self.request());self.f.tick(3);count=len(self.f.b.commands)
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b);self.f.tick(4)
        self.assertEqual(count,len(self.f.b.commands));self.assertFalse(self.f.b.data['orders']);self.assertEqual(len(self.f.b.data['positions']),6)
    def test_natural_tp_race_does_not_rearm_manual_range(self):
        self.f.bot.action(self.request());self.f.b.close('TP',position_id=self.f.b.data['positions'][0]['id']);self.f.tick(3)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'MANUAL_CLOSE_NATURAL_EXIT_RACE');self.assertFalse(self.f.b.data['orders'])
    def test_duplicate_receipt_closes_once(self):
        gate=ActionGateway(self.f.bot,execute=self.f.bot.action);r=dict(id='manual-close-id',revision=0,action=self.request())
        first=gate.apply(r);self.assertEqual(first['status'],'APPLIED');self.assertEqual(gate.apply(r),first);self.f.tick(6)
        self.assertEqual(gate.apply(r),first);self.assertEqual(len([c for c in self.f.b.commands if c['kind']=='CLOSE']),6)
    def test_no_rearm_before_last_close_evidence(self):
        self.f.bot.action(self.request());original=self.f.b.submit;held=[]
        def submit(cmd):
            if cmd['kind']=='CLOSE' and not held:held.append(deepcopy(cmd));return
            original(cmd)
        self.f.b.submit=submit;self.f.tick(4)
        self.assertEqual(len(self.f.b.data['positions']),1);self.assertFalse(self.f.b.data['orders'])
        original(held[0]);self.f.tick(5);self.assertEqual(len(self.f.b.data['orders']),12)

    def test_partial_tp_then_manual_close_accounts_only_live_remainder(self):
        self.f.b.close('TP',position_id=self.f.b.data['positions'][0]['id']);self.f.tick(5)
        self.close();self.assertEqual(len([cmd for cmd in self.f.b.commands if cmd['kind']=='CLOSE']),5)
        self.assertTrue(self.f.bot.state['cycle'].get('range_override'))
    def test_partial_close_rejection_preserves_remaining_protection(self):
        original=self.f.b.submit;count=[0]
        def submit(cmd):
            if cmd['kind']=='CLOSE':
                count[0]+=1
                if count[0]==2:
                    self.f.b.commands.append(deepcopy(cmd));self.f.b.results[cmd['id']]={'status':'REJECTED'};return
            original(cmd)
        self.f.b.submit=submit;self.close()
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'MANUAL_CLOSE_PARTIAL_FAILURE')
        self.assertEqual(len(self.f.b.data['positions']),1);self.assertFalse(self.f.b.data['orders'])
        self.assertTrue(all(p['sl'] and p['tp'] for p in self.f.b.data['positions']))
    def test_ambiguous_same_timestamp_actual_exit_blocks_range(self):
        self.f.bot.action(self.request());self.f.tick(1)
        for d in self.f.b.data['deals']:
            if d['entry']=='OUT' and d['reason']=='MANUAL':d['time']=999
        self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'MANUAL_CLOSE_LAST_EXIT_AMBIGUOUS');self.assertFalse(self.f.b.data['orders'])
    def test_manual_close_of_once_basket_does_not_resurrect_spent_override(self):
        self.close();self.fill_next();self.close();self.fill_next();self.stop_all()
        self.assertEqual(self.f.bot.state['cycle']['expanded'],self.main);self.assertNotIn('range_override',self.f.bot.state['cycle'])
    def test_temporary_override_of_permanent_restores_permanent(self):
        self.close(duration='permanent');permanent=deepcopy(self.f.bot.state['cycle']['expanded'])
        self.fill_next();self.close(duration='once');self.fill_next();self.stop_all()
        self.assertEqual(self.f.bot.state['cycle']['expanded'],permanent)
        self.assertEqual(self.f.bot.state['cycle']['range_override']['duration'],'permanent')
    def test_other_side_fill_still_uses_safe_oco(self):
        self.close();self.f.b.fill('BUY');self.f.b.fill('SELL');self.f.tick(4)
        self.assertEqual(self.f.bot.state['cycle']['manual_reason'],'BOTH_RECOVERY_SIDES_FILLED')
    def test_initial_once_without_main_range_rejected(self):
        # A fresh standalone fixture has no invented symmetric main range.
        case=fixtures.LatestClose();case.setUp();self.addCleanup(case.doCleanups);f=case.f
        f.start();f.b.fill('BUY');f.tick(3)
        a=self.request();a['cycle_id']=f.bot.state['cycle']['id'];a['expected_positions']=[p['id'] for p in f.b.data['positions']]
        with self.assertRaises(ActionRejected):f.bot.action(a)
        a['duration']='permanent';f.bot.action(a);f.tick(5)
        self.assertTrue(f.bot.state['cycle'].get('range_override'))
    def test_once_consumed_on_full_tp_close_with_remaining_cost_debt(self):
        self.close();self.fill_next()
        for p in list(self.f.b.data['positions']):
            self.f.b.data['now']+=1;self.f.b.close('TP',position_id=p['id'],commission='-1')
        self.f.tick(5)
        self.assertGreater(D(self.f.bot.state['cycle']['recovery']),0)
        self.assertEqual(self.f.bot.state['cycle']['expanded'],self.main)
        self.assertNotIn('range_override',self.f.bot.state['cycle'])

    def test_absent_order_with_pending_intent_blocks_manual_close(self):
        c=self.f.bot.state['cycle'];cid=c['entries'][-1]
        self.f.bot.state['commands'][cid]['status']='PENDING'
        self.assertFalse(self.f.b.data['orders'])
        with self.assertRaises(ActionRejected):self.f.bot.action(self.request())
        self.assertFalse(any(cmd['kind']=='CLOSE' for cmd in self.f.b.commands))


class NativeManualControl(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.NativeLatestClose();self.case.setUp();self.addCleanup(self.case.doCleanups);self.f=self.case.f
        self.case.first_sl();f=self.f
        for _ in range(6):f.api.fill(index=next(i for i,o in enumerate(f.api.orders) if o.type==f.api.ORDER_TYPE_SELL_STOP))
        f.tick(5)
    def test_native_evidence_manual_close_and_rearm_once(self):
        f=self.f;f.api.lose_close_reply=True;snap=f.bot.snapshot();c=f.bot.state['cycle']
        f.bot.action(dict(type='manual_close',cycle_id=c['id'],expected_positions=sorted(p['id'] for p in snap['positions']),mode='from_close',duration='permanent',same_distance='1',opposite_distance='2',levels=None))
        f.tick(7)
        self.assertFalse(f.api.positions);self.assertEqual(len(f.api.orders),12)
        self.assertEqual(c['expanded'],{'BUY':'3999.1','SELL':'3996.1'})
        self.assertEqual(len([r for r in f.api.sends if r['action']==f.api.TRADE_ACTION_DEAL]),6)
        self.assertGreaterEqual(f.store.db.execute('SELECT count(*) FROM consumed_deals').fetchone()[0],7)
        f.bot=LatestCloseBasketController(f.store,f.broker,clock=lambda:f.api.now,monotonic=lambda:f.api.now);f.tick(3)
        self.assertEqual(len([r for r in f.api.sends if r['action']==f.api.TRADE_ACTION_DEAL]),6)
