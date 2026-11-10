from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_tradfi_basket as fixtures
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.control import ActionRejected,ActionGateway

class V3(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BasketTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;f.bot=ControlledRecoveryController(f.store,f.b)
        f.bot.state['tp_distances']=['.20']+[None]*19
    def a(self,kind,**kw):return dict(type=kind,cycle_id=self.f.bot.state['cycle']['id'],**kw)
    def initial(self):
        f=self.f;f.start();f.b.fill('BUY');f.tick(3)
    def loss(self):
        self.initial();f=self.f;f.b.close('SL');f.tick(4)
        self.assertEqual(f.bot.state['phase'],'V3_WAIT_ENTRY');self.assertFalse(f.b.data['orders'])
    def entry(self,kind='MARKET',timing='now',side='SELL',count=3):
        self.f.bot.action(self.a('v3_entry',order_type=kind,timing=timing,side=side,count=count));self.f.tick(5)
    def ids(self):return sorted(p['id'] for p in self.f.b.data['positions'])
    def test_v3_default_and_version_switch_removed(self):
        f=self.f;self.assertEqual(f.bot.state['recovery_mode'],'V3')
        for mode in ('V1','V3','V3.3'):
            with self.assertRaises(ActionRejected):f.bot.action(dict(type='recovery_mode',mode=mode,cycle_id=None))
        self.initial();f.b.close('SL');f.tick(4)
        self.assertFalse(f.b.data['orders']);self.assertEqual(f.bot.state['phase'],'V3_WAIT_ENTRY')
    def test_initial_cancel_still_one_shot(self):
        f=self.f;f.start();f.bot.action(self.a('cancel_initial',expected_orders=[o['id'] for o in f.b.data['orders']]));f.tick(4)
        self.assertFalse(f.b.data['orders']);f.bar(120);f.tick(2);self.assertEqual(len(f.b.data['orders']),1)
    def test_market_selected_count_and_wait_whole_basket(self):
        self.loss();self.entry();f=self.f
        self.assertEqual(len(f.b.data['positions']),3);self.assertEqual(f.bot.state['phase'],'RECOVERY_POSITION')
        for p in f.b.data['positions']:self.assertEqual(D(p['size']),D('.01'));self.assertGreater(D(p['sl']),D(p['entry']))
        f.b.close('SL',position_id=f.b.data['positions'][-1]['id']);f.tick(3)
        with self.assertRaises(ActionRejected):self.entry()
        for p in list(f.b.data['positions']):f.b.close('SL',position_id=p['id'])
        f.tick(4);self.assertEqual(f.bot.state['phase'],'V3_WAIT_ENTRY');self.assertFalse(f.b.data['orders'])
    def test_current_next_candle_color_and_close_offset(self):
        for timing in ('current','next'):
            with self.subTest(timing=timing):
                x=V3();x.setUp();self.addCleanup(x.doCleanups);x.loss();f=x.f
                x.entry('STOP',timing,'BUY',2);r=f.bot.state['cycle']['v3_request'];target=r['bar_id']
                f.b.data.update(now=target+60,candle=dict(id=target,open='4001',close='4000'));f.b.quote('3999.7','3999.8');f.tick(3)
                self.assertEqual(len(f.b.data['orders']),2);self.assertEqual({o['side'] for o in f.b.data['orders']},{'SELL'});self.assertEqual({D(o['price']) for o in f.b.data['orders']},{D('3999')})
    def test_doji_missed_and_restart_never_replay(self):
        for mode in ('doji','missed','restart'):
            with self.subTest(mode=mode):
                x=V3();x.setUp();self.addCleanup(x.doCleanups);x.loss();x.entry('MARKET','current');f=x.f;r=f.bot.state['cycle']['v3_request'];target=r['bar_id']
                if mode=='restart':f.bot=ControlledRecoveryController(f.store,f.b)
                f.b.data.update(now=target+60+(60 if mode=='missed' else 0),candle=dict(id=target+(60 if mode=='missed' else 0),open='4000' if mode=='doji' else '3999',close='4000'));f.tick(3)
                self.assertFalse(f.b.data['positions']);self.assertNotIn('v3_request',f.bot.state['cycle'])
    def test_cancel_schedule_and_stop(self):
        self.loss();self.entry('STOP','next');f=self.f
        f.bot.action(self.a('v3_cancel_request'));self.assertNotIn('v3_request',f.bot.state['cycle'])
        self.entry('MARKET','current');f.bot.command('/stop');f.tick(2);self.assertNotIn('v3_request',f.bot.state['cycle']);self.assertFalse(f.b.data['positions'])
    def test_unknown_no_duplicate_and_invalid_payload(self):
        self.loss();f=self.f;f.b.fail_before.add('MARKET');self.entry()
        before=len(f.b.commands);f.tick(5);self.assertEqual(len(f.b.commands),before)
        with self.assertRaises(ActionRejected):self.entry()
    def test_mode_cannot_change_active(self):
        self.initial()
        with self.assertRaises(ActionRejected):self.f.bot.action(self.a('recovery_mode',mode='V1'))
        with self.assertRaises(ActionRejected):self.f.bot.action(self.a('manual_close'))
    def link(self,count=2):
        f=self.f;f.bot.action(self.a('v3_link',count=count,expected_positions=self.ids()));f.tick(3)
    def test_link_cancel_one_shot_restart(self):
        self.initial();self.link();f=self.f;self.assertEqual(len(f.b.data['orders']),2)
        action=self.a('v3_cancel_link');gateway=ActionGateway(f.bot,execute=f.bot.action);request=dict(id='cancel-link-one',revision=f.bot.state.get('control_revision',0),action=action)
        self.assertEqual(gateway.apply(request)['status'],'APPLIED');self.assertEqual(gateway.apply(request)['status'],'APPLIED');f.tick(4)
        self.assertFalse(f.b.data['orders']);self.assertEqual(len(f.b.data['positions']),1)
        f.bot=ControlledRecoveryController(f.store,f.b);f.tick(3);self.assertFalse(f.b.data['orders'])
    def test_initial_tp_cancels_link(self):
        self.initial();self.link();f=self.f;f.b.close('TP');f.tick(8);self.assertFalse(f.b.data['orders']);self.assertIsNone(f.bot.state['cycle'])
    def test_sl_link_fills_actual_debt_recalculation(self):
        self.initial();self.link();f=self.f;f.b.close('SL')
        f.b.fill('SELL');f.b.fill('SELL');entry=D(f.b.data['positions'][0]['entry']);f.b.quote(entry-D('.05'),entry+D('.05'));f.tick(6)
        self.assertEqual(f.bot.state['phase'],'RECOVERY_POSITION');self.assertFalse(f.bot.state['cycle'].get('manual'));self.assertEqual(len(f.b.data['positions']),2)
    def test_link_follows_confirmed_adjustment(self):
        self.initial();self.link();f=self.f
        f.bot.action(self.a('v3_adjust_sl',distance='2.90',expected_positions=self.ids()));f.tick(12)
        self.assertFalse(f.bot.state['cycle'].get('manual'));self.assertEqual(len(f.b.data['orders']),2)
        self.assertEqual({D(o['price']) for o in f.b.data['orders']},{D(f.b.data['positions'][0]['sl'])})
    def test_sl_adjust_exact_steps_and_actual_entries(self):
        self.loss();self.entry();f=self.f
        with self.assertRaises(ActionRejected):f.bot.action(self.a('v3_adjust_sl',distance='1.55',expected_positions=self.ids()))
        f.bot.action(self.a('v3_adjust_sl',distance='1.40',expected_positions=self.ids()));f.tick(5)
        for p in f.b.data['positions']:self.assertEqual(D(p['sl'])-D(p['entry']),D('1.40'))
    def test_readonly_rejects_all_new_entries(self):
        self.loss();self.f.b.read_only=True
        with self.assertRaises(ActionRejected):self.entry()
    def test_settings_validation_and_request_snapshot(self):
        self.loss();f=self.f
        f.bot.action(self.a('v3_settings',entry_distance='2.00',count_presets=[2,5,9]));self.entry('STOP','next')
        f.bot.action(self.a('v3_settings',entry_distance='3.00',count_presets=[3,6,10]));self.assertEqual(f.bot.state['cycle']['v3_request']['distance'],'2.00')

    def test_tp1_keeps_link_frozen_and_overlap_is_supported(self):
        self.loss();self.entry(count=3);f=self.f;self.link(2)
        old=list(f.b.data['positions']);level=f.bot.state['cycle']['v3_link']['price']
        # Move into profit so automatic own-entry SL remains broker-valid.
        f.b.close('TP',position_id=old[0]['id']);f.tick(7)
        self.assertEqual(len(f.b.data['orders']),2);self.assertEqual(f.bot.state['cycle']['v3_link']['price'],level)
        for p in f.b.data['positions']:self.assertEqual(D(p['sl']),D(p['entry']))
        f.b.fill('BUY');f.b.fill('BUY');entry=D(f.b.data['positions'][-1]['entry']);f.b.quote(entry-D('.05'),entry+D('.05'));f.tick(6)
        self.assertEqual(len(f.b.data['positions']),4);self.assertFalse(f.bot.state['cycle'].get('manual'))
        self.assertEqual(len({t['group'] for t in f.bot.state['cycle']['tickets'].values() if not t['closed']}),2)
        self.assertFalse(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_enter'])
        f.bot=ControlledRecoveryController(f.store,f.b);f.tick(3);self.assertEqual(len(f.b.data['positions']),4)
        for p in list(f.b.data['positions']):f.b.close('SL',position_id=p['id'])
        f.tick(5);self.assertEqual(f.bot.state['phase'],'V3_WAIT_ENTRY')

    def test_link_partial_fill_retains_cancel_and_waits_source_ledger(self):
        self.initial();self.link();f=self.f;source=f.b.data['positions'][0]['id']
        f.b.fill('SELL');f.tick(4)
        self.assertIsNotNone(f.bot.state['cycle'].get('v3_link'))
        self.assertTrue(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_cancel_link'])
        f.b.fill('SELL');f.tick(5)
        g=f.bot.state['cycle']['groups'][-1]
        self.assertFalse(g['ladder_applied'])
        f.b.close('SL',position_id=source)
        entry=D(next(p['entry'] for p in f.b.data['positions']));f.b.quote(entry-D('.05'),entry+D('.05'));f.tick(6)
        self.assertTrue(g['ladder_applied']);self.assertGreater(D(f.bot.state['cycle']['recovery']),0)
        self.assertFalse(f.bot.state['cycle'].get('manual'))


    def test_frozen_link_survives_sl_edits_and_final_source_sl(self):
        self.loss();self.entry(count=3);self.link();f=self.f
        first=min(f.bot.state['cycle']['tickets'].values(),key=lambda t:1000 if t['role']=='initial' else t['index'])
        f.b.close('TP',position_id=first['id']);f.tick(7)
        before=deepcopy(f.b.data['orders'])
        f.bot.action(self.a('v3_adjust_sl',distance='1.40',expected_positions=self.ids()));f.tick(8)
        self.assertEqual(f.b.data['orders'],before)
        self.assertTrue(all(abs(D(p['sl'])-D(p['entry']))==D('1.40') for p in f.b.data['positions']))
        p=f.b.data['positions'][0];old={k:p[k] for k in ('sl','tp')}
        f.bot.edit(protection=dict(position_id=p['id'],old=old,new=dict(sl=str(D(p['entry'])+D('1.30')),tp=p['tp'])));f.tick(6)
        self.assertEqual(f.b.data['orders'],before)
        for p in list(f.b.data['positions']):f.b.close('SL',position_id=p['id'])
        f.tick(6);self.assertEqual(f.b.data['orders'],before);self.assertEqual(f.bot.state['phase'],'RECOVERY_PENDING')

    def test_cancel_partial_link_keeps_filled_positions_managed(self):
        self.initial();self.link();f=self.f;source=f.b.data['positions'][0]['id']
        f.b.fill('SELL');f.tick(3)
        f.bot.action(self.a('v3_cancel_link'));f.tick(5)
        self.assertFalse(f.b.data['orders']);self.assertFalse(f.bot.state['cycle'].get('manual'))
        f.b.close('SL',position_id=source)
        entry=D(f.b.data['positions'][0]['entry']);f.b.quote(entry-D('.05'),entry+D('.05'));f.tick(6)
        self.assertEqual(len(f.b.data['positions']),1);self.assertTrue(f.bot.state['cycle']['groups'][-1]['ladder_applied'])
        self.assertFalse(f.bot.state['cycle'].get('manual'))
        p=f.b.data['positions'][0];self.assertEqual(abs(D(p['entry'])-D(p['tp'])),D('.20'))
        f.b.close('TP',position_id=p['id']);f.tick(6)
        self.assertGreater(D(f.bot.state['cycle']['recovery']),0);self.assertEqual(f.bot.state['phase'],'V3_WAIT_ENTRY');self.assertFalse(f.b.data['orders'])


    def test_link_intents_and_source_are_one_durable_write(self):
        self.initial();f=self.f;original=f.bot.save;seen=[]
        def save(reason):
            if reason.startswith('v3_'):
                seen.append(reason)
                self.assertIsNotNone(f.bot.state['cycle'].get('v3_link'))
                self.assertIsNotNone(f.bot.state['cycle']['groups'][-1].get('linked_source'))
            return original(reason)
        f.bot.save=save
        f.bot.action(self.a('v3_link',count=2,expected_positions=self.ids()))
        self.assertEqual(seen,['v3_link_created'])
        f.bot=ControlledRecoveryController(f.store,f.b);f.tick(3)
        self.assertEqual(len(f.b.data['orders']),2)
        self.assertTrue(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_cancel_link'])


    def test_existing_sl_buttons_with_frozen_pending(self):
        self.loss();self.entry(count=3);self.link();f=self.f
        first=next(t for t in f.bot.state['cycle']['tickets'].values() if t['role']=='v3' and t['index']==0)
        f.b.close('TP',position_id=first['id']);f.tick(7);before=deepcopy(f.b.data['orders'])
        f.bot.action(self.a('own_entry_sl',expected_positions=self.ids()));f.tick(5)
        self.assertEqual(f.b.data['orders'],before)
        # Choose an exact tick grid for the existing reverse ladder algorithm.
        entry=D(f.b.data['positions'][0]['entry']);f.b.quote(entry-D('.50'),entry-D('.40'));f.tick(2)
        self.assertTrue(f.bot.reverse_controls(f.bot.snapshot())['can_enable'])
        f.bot.action(self.a('reverse_ladder',enabled=True,expected_positions=self.ids()));f.tick(5)
        self.assertEqual(f.b.data['orders'],before);self.assertFalse(f.bot.state['cycle'].get('manual'))

    def test_tp_settings_preserve_unfilled_link_and_count(self):
        self.initial();self.link(count=3);f=self.f;before=deepcopy(f.b.data['orders'])
        f.bot.edit_tp_distances(['.30']+[None]*19);f.tick(5)
        self.assertEqual(f.b.data['orders'],before);self.assertEqual(D(f.bot.state['cycle']['tp_distances'][0]),D('.30'))


    def test_ordinary_pending_settings_rearm_same_count_and_level(self):
        self.loss();self.entry('STOP','current',count=2);f=self.f
        target=f.bot.state['cycle']['v3_request']['bar_id']
        f.b.data.update(now=target+60,candle=dict(id=target,open='4001',close='4000'));f.b.quote('3999.7','3999.8');f.tick(4)
        self.assertEqual(len(f.b.data['orders']),2)
        before=f.b.data['orders'][0]['price'];cfg=deepcopy(f.bot.state['cycle']['cfg']);cfg['RECOVERY_SL_DISTANCE']='1.40'
        f.bot.edit(settings=cfg);f.tick(10)
        self.assertEqual(len(f.b.data['orders']),2);self.assertEqual({p['price'] for p in f.b.data['orders']},{before})
        self.assertTrue(all(D(p['sl'])-D(p['price'])==D('1.40') for p in f.b.data['orders']))
        self.assertFalse(f.bot.state['cycle'].get('manual'))


    def test_stop_during_pending_settings_never_rearms(self):
        self.loss();self.entry('STOP','current',count=2);f=self.f
        target=f.bot.state['cycle']['v3_request']['bar_id']
        f.b.data.update(now=target+60,candle=dict(id=target,open='4001',close='4000'));f.b.quote('3999.7','3999.8');f.tick(4)
        cfg=deepcopy(f.bot.state['cycle']['cfg']);cfg['RECOVERY_SL_DISTANCE']='1.40'
        f.bot.edit(settings=cfg);f.bot.command('/stop');f.tick(10)
        self.assertFalse(f.b.data['orders']);self.assertFalse(f.bot.state['cycle'].get('v3_rearm'))
        self.assertFalse(f.b.data['positions'])
