"""Integration on artificial, in-memory broker facts only."""
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from tradfi.interactive import InteractiveController
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.strategies.fixed_recovery import RecoveryConfig
from tradfi.storage import Store


class InteractiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'state.sqlite3');self.addCleanup(self.store.close)
        self.broker=InteractiveSimulator();self.bot=InteractiveController(self.store,self.broker)

    def tick(self,n=1):
        for _ in range(n):self.bot.tick()

    def start(self):
        self.bot.configure('fixed',RecoveryConfig().dump())
        self.bot.command('/start semi')
        self.broker.quote('4135.9','4136')
        self.bot.triggers('prices',{'BUY':'4139','SELL':'4133'})
        self.tick(2)

    def fill(self,side='SELL'):
        self.broker.fill(side)
        p=self.broker.data['positions'][0];entry=D(p['entry'])
        self.broker.quote(entry-D('.05'),entry+D('.05'))
        self.tick(4)
        return p

    def recovery(self):
        self.start();self.fill()
        self.broker.close('SL',price='4136');self.tick(2)
        self.fill('BUY')

    def test_first_tp_default_and_semi_waits_without_candle(self):
        self.start()
        self.assertEqual(D(self.broker.data['orders'][0]['tp']),D('4139.25'))
        self.fill();self.broker.close('TP');self.tick(2)
        self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.broker.candle(60);self.tick(2)
        self.assertFalse(self.broker.data['orders'])

    def test_fixed_example_sl_pair_tp_and_continuation(self):
        self.recovery()
        c=self.bot.state['cycle'];p=self.broker.data['positions'][0]
        self.assertEqual(D(c['fixed_recovery']['debt']),54)
        self.assertEqual(D(p['entry']),4139)
        self.assertEqual(D(p['sl']),4136)
        self.assertEqual(D(p['tp']),4140)
        self.broker.quote('4139.9','4140');self.tick(2)
        self.assertEqual(D(p['sl']),4136)
        self.broker.close('TP');self.tick(2)
        self.assertEqual(D(c['fixed_recovery']['debt']),36)
        self.assertEqual({o['side']:D(o['price']) for o in self.broker.data['orders']},{'BUY':D(4141),'SELL':D(4137)})

    def test_resize_preserves_debt_updates_tp_not_sl(self):
        self.recovery();p=self.broker.data['positions'][0];sl=p['sl']
        self.bot.configure('fixed',RecoveryConfig(parts=5).dump());self.tick(2)
        self.assertEqual(D(p['tp']),D('4139.60'));self.assertEqual(p['sl'],sl)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),54)

    def test_resize_reached_tp_closes_once_and_confirms_actual_result(self):
        self.recovery();self.broker.quote('4139.7','4139.8')
        self.broker.fail_after.add('CLOSE')
        self.bot.configure('fixed',RecoveryConfig(parts=5).dump());self.tick(3)
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='CLOSE']),1)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),D('41.4'))

    def test_manual_close_waits_and_preserves_net_on_restart(self):
        self.recovery();p=self.broker.data['positions'][0]
        self.broker.quote('4139.5','4139.6');self.broker.fail_after.add('CLOSE')
        self.bot.close_position(p['id'],p['size']);self.tick(3)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),45)
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.bot=InteractiveController(self.store,self.broker);self.tick(3)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),45)
        self.assertFalse(self.broker.data['orders'])
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='CLOSE']),1)

    def test_unknown_close_never_retries(self):
        self.recovery();p=self.broker.data['positions'][0];self.broker.fail_before.add('CLOSE')
        self.bot.close_position(p['id'],p['size']);self.tick(3)
        self.bot=InteractiveController(self.store,self.broker);self.tick(3)
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='CLOSE']),1)
        self.assertTrue(self.broker.data['positions'])

    def test_replacement_cancel_fill_race_preserves_position(self):
        self.start();self.broker.cancel_race='SELL'
        self.bot.triggers('prices',{'BUY':'4140','SELL':'4132'});self.tick(4)
        self.assertEqual(len(self.broker.data['positions']),1)
        self.assertFalse(self.broker.data['orders'])
        self.assertEqual(self.bot.state['phase'],'POSITION')
        self.assertFalse([c for c in self.broker.commands if c['kind']=='CLOSE'])

    def test_replacement_waits_for_confirmed_cancel(self):
        self.start();self.broker.fail_before.add('CANCEL')
        self.bot.triggers('prices',{'BUY':'4140','SELL':'4132'});self.tick(3)
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='STOP']),2)
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='CANCEL']),2)

    def test_time_block_cancels_pending_no_new_trigger(self):
        self.start();self.bot.state['time_block']={'reason':'EXTERNAL_UTC_UNVERIFIED'}
        self.bot.save('test_latch');self.tick(2)
        self.assertFalse(self.broker.data['orders'])
        with self.assertRaises(ValueError):self.bot.triggers('prices',{'BUY':'4140'})
        self.assertIn('time_block',self.bot.state)

    def test_engine_switch_deferred(self):
        self.recovery();self.bot.configure('legacy',RecoveryConfig().dump());self.tick()
        self.assertEqual(self.bot.state['cycle']['engine'],'fixed')
        self.assertEqual(self.bot.state['modules']['engine'],'legacy')

    def test_ranges_anchor_received_ask_bid_not_candle(self):
        self.bot.command('/start semi');self.broker.quote('4135.9','4136')
        self.bot.triggers('ranges',{'BUY':'3','SELL':'3'});self.tick(2)
        self.assertEqual({o['side']:D(o['price']) for o in self.broker.data['orders']},{'BUY':D(4139),'SELL':D('4132.9')})

    def test_manual_close_next_trigger_resumes_same_debt(self):
        self.recovery();p=self.broker.data['positions'][0]
        self.broker.quote('4139.5','4139.6')
        self.bot.close_position(p['id'],p['size']);self.tick(2)
        cid=self.bot.state['cycle']['id']
        self.bot.triggers('prices',{'BUY':'4141','SELL':'4137'});self.tick(3)
        self.assertEqual(self.bot.state['cycle']['id'],cid)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),45)
        self.assertEqual(len(self.broker.data['orders']),2)

    def test_manual_close_unknown_then_sl_stops_after_confirmed_actual_loss(self):
        self.recovery();p=self.broker.data['positions'][0];self.broker.fail_before.add('CLOSE')
        self.bot.close_position(p['id'],p['size'])
        self.broker.close('SL');self.tick(3)
        self.assertFalse(self.broker.data['orders'])
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),108)

    def test_manual_protection_persists_until_explicit_n_recalculation(self):
        self.recovery();p=self.broker.data['positions'][0];old={k:p[k] for k in ('sl','tp')}
        self.bot.protection(p['id'],old,dict(sl='4136.50',tp='4140.50'));self.tick(4)
        self.assertEqual(D(p['sl']),D('4136.50'));self.assertEqual(D(p['tp']),D('4140.50'))
        self.bot.configure('fixed',RecoveryConfig(parts=5).dump());self.tick(3)
        self.assertEqual(D(p['sl']),D('4136.50'));self.assertEqual(D(p['tp']),D('4139.60'))

    def test_slippage_reanchors_fixed_sl_once_then_never_trails(self):
        self.start();self.broker.fill('BUY',price='4139.10')
        self.broker.quote('4139.05','4139.15');self.tick(4)
        p=self.broker.data['positions'][0]
        self.assertEqual(D(p['sl']),D('4137.10'))
        self.broker.quote('4139.9','4140');self.tick(3)
        self.assertEqual(D(p['sl']),D('4137.10'))

    def test_crash_during_mode_ack_never_enters_auto(self):
        from tradfi.storage import DurabilityError
        def crash(event):
            if event=='mode_selected':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.bot.command('/start semi',10)
        self.store.fault=None
        self.bot=InteractiveController(self.store,self.broker)
        self.bot.command('/start semi',10);self.broker.candle(60);self.tick(3)
        self.assertEqual(self.bot.state['operation_mode'],'semi')
        self.assertFalse(self.broker.data['orders'])

    def test_manual_close_with_no_debt_finishes_cycle_and_waits(self):
        self.start();self.fill('BUY');self.broker.quote('4139.5','4139.6')
        self.bot.configure('legacy',RecoveryConfig().dump())
        p=self.broker.data['positions'][0];self.bot.close_position(p['id'],p['size']);self.tick(3)
        self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.bot.triggers('prices',{'BUY':'4142','SELL':'4130'});self.tick(2)
        self.assertEqual(self.bot.state['cycle']['engine'],'legacy')
        self.assertEqual(D(self.broker.data['orders'][0]['tp']),D('4142.25'))

    def test_fill_during_replace_then_sl_does_not_deadlock(self):
        self.start();self.broker.cancel_race='SELL'
        self.bot.triggers('prices',{'BUY':'4140','SELL':'4132'});self.tick(4)
        self.broker.close('SL',price='4136');self.tick(4)
        self.assertEqual(len(self.broker.data['orders']),2)
        self.assertNotIn('replacement',self.bot.state['cycle'])

    def test_rejected_modify_and_recovery_close_do_not_repeat(self):
        for kind in ('MODIFY','CLOSE'):
            with self.subTest(kind=kind):
                if kind=='CLOSE':self.setUp()
                self.recovery();p=self.broker.data['positions'][0]
                original=self.broker.submit
                def reject(cmd):
                    if cmd['kind']==kind:
                        self.broker.commands.append(dict(cmd))
                        self.broker.results[cmd['id']]={'status':'REJECTED','reason':'broker refusal'}
                    else:original(cmd)
                self.broker.submit=reject
                if kind=='MODIFY':self.bot.protection(p['id'],{k:p[k] for k in ('sl','tp')},dict(sl=p['sl'],tp='4141'))
                else:
                    self.broker.quote('4139.7','4139.8')
                    self.bot.configure('fixed',RecoveryConfig(parts=5).dump())
                before=len([x for x in self.broker.commands if x['kind']==kind])
                self.tick(5)
                self.assertEqual(len([x for x in self.broker.commands if x['kind']==kind]),before)
                self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')

    def test_unknown_extra_owned_position_blocks_synchronous_close(self):
        self.recovery();p=self.broker.data['positions'][0]
        self.broker.data['positions'].insert(0,dict(p,id='other-owned-position'))
        with self.assertRaises(ValueError):self.bot.close_position(p['id'],p['size'])
        self.assertFalse([c for c in self.broker.commands if c['kind']=='CLOSE'])

    def test_simulator_close_exact_id(self):
        self.recovery();p=self.broker.data['positions'][0]
        self.broker.data['positions'].insert(0,dict(p,id='other-owned-position'))
        self.broker.submit(dict(id='fixture-exact-close',kind='CLOSE',position_id=p['id'],size=p['size'],purpose='MANUAL'))
        self.assertEqual([x['id'] for x in self.broker.data['positions']],['other-owned-position'])

    def test_paused_semi_start_crash_never_rearms_without_levels(self):
        from tradfi.storage import DurabilityError
        self.start();self.bot.command('/stop');self.tick(3)
        def crash(event):
            if event=='mode_selected':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.bot.command('/start semi',10)
        self.store.fault=None;self.bot=InteractiveController(self.store,self.broker)
        self.tick(3)
        self.assertFalse(self.broker.data['orders'])

    def test_telegram_action_crash_after_execution_does_not_replay(self):
        import json
        from tradfi.storage import DurabilityError
        self.recovery();p=self.broker.data['positions'][0]
        text='/action '+json.dumps(dict(type='close_position',position_id=p['id'],expected_size=p['size'],confirmed=True))
        def crash(event):
            if event=='interactive_action':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.bot.command(text,44)
        self.store.fault=None;self.bot=InteractiveController(self.store,self.broker)
        self.bot.command(text,44);self.tick(3)
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='CLOSE']),1)
        self.assertEqual(self.bot.state['telegram_offset'],45)

    def test_telegram_mode_button_requires_owner_and_keeps_open_panel_button(self):
        from tradfi.telegram import Telegram
        telegram=Telegram('FAKE_OFFLINE_FIXTURE','123',user_id='456')
        self.bot.command('/start')
        markup=self.bot.state['notifications'][-1]['reply_markup']
        self.assertIn('127.0.0.1',markup['inline_keyboard'][1][0]['url'])
        payload=telegram.payload('123','Mode',markup)
        self.assertEqual(payload['reply_markup'],markup)
        def callback(uid,user):
            return dict(update_id=uid,callback_query=dict(data='/start semi',
                message=dict(chat=dict(id=123)),**{'from':dict(id=user)}))
        telegram.incoming.put(callback(1,999));telegram.process(self.bot)
        self.assertEqual(self.bot.state['phase'],'IDLE')
        telegram.incoming.put(callback(2,456));telegram.process(self.bot)
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')

    def test_three_actual_full_tp_closures_complete_and_semi_waits(self):
        self.recovery()
        for index in range(3):
            self.broker.close('TP');self.tick(2)
            if index<2:self.fill('BUY')
        self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.assertEqual(D(self.bot.state['completed'][-1]['realized']),0)
        self.assertFalse(self.broker.data['orders'])

    def test_actual_cost_residual_one_cent_is_not_forgiven(self):
        self.recovery();self.broker.close('TP',profit='54',commission='-0.01');self.tick(2)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),D('.01'))
        self.assertFalse(self.bot.state['completed'])
        self.fill('BUY');self.broker.close('TP');self.tick(2)
        self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.bot.state['completed'][-1]['forgiven_residual'],'0')

    def test_new_sl_adds_actual_loss_and_repartitions(self):
        self.recovery();self.broker.close('SL',commission='-1');self.tick(2)
        c=self.bot.state['cycle']
        self.assertEqual(D(c['fixed_recovery']['debt']),109)
        self.assertEqual(c['fixed_recovery']['parts_remaining'],3)

    def test_fixed_sell_mirror(self):
        self.start();self.fill('BUY');self.broker.close('SL',price='4136');self.tick(2)
        self.fill('SELL');p=self.broker.data['positions'][0]
        self.assertEqual(D(p['sl']),4136);self.assertEqual(D(p['tp']),4132)
        self.broker.close('TP');self.tick(2)
        self.assertEqual({o['side']:D(o['price']) for o in self.broker.data['orders']},{'BUY':D(4135),'SELL':D(4131)})

    def test_auto_uses_closed_candle_while_semi_uses_received_quote(self):
        self.bot.command('/start auto');self.tick(2)
        self.assertFalse(self.broker.data['orders'])
        self.broker.candle(60,'4001');self.tick(2)
        self.assertEqual({D(o['price']) for o in self.broker.data['orders']},{D(4004),D(3998)})
        self.assertEqual(D(self.broker.data['orders'][0]['tp']),D('4004.25'))

    def test_time_latch_retained_across_active_protection_and_restart(self):
        self.recovery();self.bot.state['time_block']={'reason':'EXTERNAL_UTC_UNVERIFIED'};self.bot.save('test_latch')
        p=self.broker.data['positions'][0]
        self.bot.protection(p['id'],{k:p[k] for k in ('sl','tp')},dict(sl='4136.5',tp='4140.5'));self.tick(2)
        self.bot=InteractiveController(self.store,self.broker);self.tick(2)
        self.assertEqual(D(p['sl']),D('4136.5'))
        self.assertIn('time_block',self.bot.state)
        with self.assertRaises(ValueError):self.bot.command('/start semi')

    def test_settings_mode_refusal_changes_neither_mode_nor_modules(self):
        from tradfi.control import ActionGateway
        self.bot.state['phase']='WAIT_START';self.broker.data['now']=181;self.bot.save('wait_start_fixture')
        result=ActionGateway(self.bot,execute=self.bot.action).apply(dict(id='settings-reject-0001',revision=0,
            action=dict(type='settings',mode='semi',engine='fixed',recovery=RecoveryConfig().dump())))
        self.assertEqual(result['status'],'REJECTED')
        saved=self.store.load();self.assertEqual(saved['operation_mode'],'auto');self.assertEqual(saved['modules']['engine'],'legacy')

    def test_settings_mode_and_modules_are_atomic_on_crash(self):
        from tradfi.storage import DurabilityError
        self.bot.save('initial')
        def crash(event):
            if event=='interactive_settings_mode':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):self.bot.action(dict(type='settings',mode='semi',engine='fixed',recovery=RecoveryConfig().dump()))
        self.store.fault=None
        saved=self.store.load();self.assertEqual(saved['operation_mode'],'auto');self.assertEqual(saved['modules']['engine'],'legacy')
        self.bot=InteractiveController(self.store,self.broker)
        self.bot.action(dict(type='settings',mode='semi',engine='fixed',recovery=RecoveryConfig().dump()))
        saved=self.store.load();self.assertEqual(saved['phase'],'WAIT_TRIGGER');self.assertEqual(saved['modules']['engine'],'fixed')
