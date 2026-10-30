"""Candle entry regressions on offline, deterministic broker facts only."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from tradfi.interactive import InteractiveController
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.storage import Store
from tradfi.settings import Settings
from tradfi.strategies.fixed_recovery import RecoveryConfig

class CandleEntryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'state.db');self.addCleanup(self.store.close)
        self.broker=InteractiveSimulator();self.broker.data['now']=90
        self.broker.data['candle']=dict(id=0,open='3999',close='4000')
        self.bot=InteractiveController(self.store,self.broker)
    def tick(self,n=1):
        for _ in range(n):self.bot.tick()
    def bar(self,cid=60,opening='3999',close='4000'):
        self.broker.data['now']=cid+60
        self.broker.data['candle']=dict(id=cid,open=opening,close=close)
    def start(self,engine='legacy',side='BUY'):
        self.bot.configure(engine,RecoveryConfig().dump());self.bot.command('/start auto')
        self.bar(opening='3999' if side=='BUY' else '4001');self.tick(2)
    def restart(self):self.bot=InteractiveController(self.store,self.broker)
    def test_buy_sell_both_engines_and_exact_protection(self):
        for engine in ('legacy','fixed'):
            for side,entry,sl,tp in [('BUY','4001','3997','4001.25'),('SELL','3999','4003','3998.75')]:
                with self.subTest(engine=engine,side=side):
                    with tempfile.TemporaryDirectory() as tmp:
                        store=Store(Path(tmp)/'s.db')
                        try:
                            b=InteractiveSimulator();b.data['now']=90;b.data['candle']=dict(id=0,open='3999',close='4000')
                            bot=InteractiveController(store,b);bot.configure(engine,RecoveryConfig().dump());bot.command('/start auto')
                            b.data['now']=120;b.data['candle']=dict(id=60,open='3999' if side=='BUY' else '4001',close='4000');bot.tick();bot.tick()
                            self.assertEqual(len(b.data['orders']),1);o=b.data['orders'][0]
                            self.assertEqual((o['side'],D(o['price']),D(o['sl']),D(o['tp'])),(side,D(entry),D(sl),D(tp)))
                            self.assertEqual(b.commands[0]['kind'],'STOP')
                        finally:store.close()
    def test_start_mid_minute_waits_for_current_close(self):
        self.bot.command('/start auto');self.tick(5);self.assertFalse(self.broker.commands)
        self.bar();self.tick();self.assertEqual(len(self.broker.data['orders']),1)
    def test_same_direction_and_doji_keep_exact_order(self):
        self.start();before=deepcopy(self.broker.data['orders'])
        self.bar(120,'4000','4000.50');self.tick(3)
        self.bar(180,'4000.75','4000.75');self.tick(3)
        self.assertEqual(self.broker.data['orders'],before)
        self.assertEqual(len(self.broker.commands),1)
    def test_doji_without_order_waits(self):
        self.bot.command('/start auto');self.bar(60,'4000','4000');self.tick(3)
        self.assertIsNone(self.bot.state['cycle']);self.assertFalse(self.broker.commands)
        self.bar(120);self.tick();self.assertEqual(len(self.broker.data['orders']),1)
    def test_flip_cancel_confirm_replace_and_restart_pending(self):
        self.start();original=self.broker.data['orders'][0]['id']
        self.bar(120,'4001','4000');self.tick()
        self.assertFalse(self.broker.data['orders']);self.restart();self.tick(3)
        self.assertEqual(len(self.broker.data['orders']),1)
        o=self.broker.data['orders'][0];self.assertNotEqual(o['id'],original);self.assertEqual(o['side'],'SELL');self.assertEqual(D(o['price']),D('3999'))
        self.restart();self.tick(4);self.assertEqual(len([c for c in self.broker.commands if c['kind']=='STOP']),2)
    def test_fill_before_flip_wins(self):
        self.start();self.broker.fill('BUY');self.bar(120,'4001','4000');self.tick(3)
        self.assertEqual(self.bot.state['phase'],'POSITION');self.assertEqual(len(self.broker.data['positions']),1)
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='STOP']),1)
    def test_fill_during_cancel_wins(self):
        self.start();self.broker.cancel_race='BUY';self.bar(120,'4001','4000');self.tick(4)
        self.assertEqual(self.bot.state['phase'],'POSITION');self.assertEqual(len(self.broker.data['positions']),1)
        self.assertFalse(self.broker.data['orders']);self.assertFalse(self.bot.state['cycle'].get('candle_replacement'))
        self.assertFalse([c for c in self.broker.commands if c['kind'] in {'MARKET','CLOSE'}])
    def test_unknown_entry_never_duplicates_even_flip_restart(self):
        self.broker.fail_before.add('STOP');self.start();self.bar(120,'4001','4000');self.tick(3);self.restart();self.tick(3)
        self.assertEqual(len(self.broker.commands),1);self.assertEqual(self.broker.commands[0]['kind'],'STOP')
    def test_unknown_cancel_never_duplicates_or_replaces(self):
        self.start();self.broker.fail_before.add('CANCEL');self.bar(120,'4001','4000');self.tick(4);self.restart();self.tick(3)
        self.assertEqual([c['kind'] for c in self.broker.commands],['STOP','CANCEL']);self.assertEqual(self.broker.data['orders'][0]['side'],'BUY')
    def test_cancel_rejected_does_not_place_new_order(self):
        self.start();original=self.broker.submit
        def reject(cmd):
            if cmd['kind']=='CANCEL':
                self.broker.commands.append(deepcopy(cmd));self.broker.results[cmd['id']]=dict(status='REJECTED');return
            original(cmd)
        self.broker.submit=reject;self.bar(120,'4001','4000');self.tick(6)
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='STOP']),1)
        self.assertEqual(self.broker.data['orders'][0]['side'],'BUY');self.assertEqual(self.bot.state['phase'],'SAFE_PAUSE')
    def test_stale_out_of_order_candle_ignored(self):
        self.start();before=deepcopy(self.broker.data['orders'])
        self.bar(0,'4001','4000');self.broker.data['now']=180;self.tick(2)
        self.bar(120,'4001','4000');self.broker.data['now']=300;self.tick(2)
        self.assertEqual(self.broker.data['orders'],before);self.assertEqual(self.bot.state['last_candle'],60)
    def test_crossed_and_stop_constraints_wait_without_market(self):
        self.broker.quote('4001','4001.1');self.start();self.assertFalse(self.broker.commands);self.assertEqual(self.bot.state['phase'],'WAIT_SIGNAL')
        self.broker.quote('4000','4000.1');self.bar(120);self.tick(3);self.assertEqual(len(self.broker.data['orders']),1)
    def test_initial_tp_new_cycle_waits_fresh_close(self):
        for engine in ('legacy','fixed'):
            with self.subTest(engine=engine):
                # Each subcase uses a distinct Store/fixture.
                if engine=='fixed':self.tearDown();self.setUp()
                self.start(engine);self.broker.fill('BUY');self.tick(3);self.broker.close('TP');self.tick(3)
                self.assertIsNone(self.bot.state['cycle']);self.assertFalse(self.broker.data['orders'])
                self.bar(120,'4000','4001');self.tick(2);self.assertEqual(len(self.broker.data['orders']),1)
    def test_sl_retains_recovery_math_and_initial_level_legacy(self):
        self.start();self.broker.fill('BUY');self.tick(3);self.broker.close('SL');self.tick()
        c=self.bot.state['cycle'];self.assertFalse(c['first']);self.assertEqual(c['branch'],'BRANCH1_RECOVERY')
        self.assertEqual(D(c['center'])+D(c['settings']['BRANCH1_RANGE']),D('4001'))
        self.assertEqual(D(c['recovery']),D('76.5'))
        self.tick(2);self.assertEqual(len(c['entries']),2)
    def test_fixed_sl_enters_existing_two_sided_recovery(self):
        self.start('fixed');self.broker.fill('BUY');self.tick(3);self.broker.close('SL');self.tick(2)
        c=self.bot.state['cycle'];self.assertFalse(c['first']);self.assertEqual(c['branch'],'FIXED_RECOVERY')
        self.assertEqual(D(c['fixed_recovery']['debt']),D('72'));self.assertEqual(len(self.broker.data['orders']),2)
    def test_stop_start_waits_new_close_without_reusing_signal(self):
        self.start();self.bot.command('/stop');self.tick(3);self.bot.command('/start auto');self.tick(3)
        self.assertFalse(self.broker.data['orders']);self.bar(120);self.tick(3);self.assertEqual(len(self.broker.data['orders']),1)
    def test_settings_migration_preserves_old_custom_values_and_cooldown(self):
        old=Settings().dump()
        for key in list(old):
            if key.startswith('INITIAL_') and key!='INITIAL_SIZE':old.pop(key)
        old.update(BRANCH1_RANGE='2.4',BRANCH1_SL_DISTANCE='5',BASE_TP_DISTANCE='.4',CYCLE_COOLDOWN_SECONDS='37')
        cfg=Settings.load(old);self.assertEqual((cfg.INITIAL_ENTRY_DISTANCE,cfg.INITIAL_SL_DISTANCE,cfg.INITIAL_TP_DISTANCE,cfg.CYCLE_COOLDOWN_SECONDS),(D('2.4'),D(5),D('.4'),D(37)))

    def test_custom_initial_settings_and_tick_constraints(self):
        cfg=Settings.load(dict(self.bot.state['settings'],INITIAL_ENTRY_DISTANCE='1.25',INITIAL_SL_DISTANCE='4.50',INITIAL_TP_DISTANCE='.30'))
        self.bot.command(cfg.template());self.start()
        order=self.broker.data['orders'][0]
        self.assertEqual((D(order['price']),D(order['sl']),D(order['tp'])),(D('4001.25'),D('3996.75'),D('4001.55')))
        with self.assertRaises(ValueError):self.bot.command(Settings.load(dict(cfg.dump(),INITIAL_ENTRY_DISTANCE='1.001')).template())
    def test_broker_stop_distance_rejects_without_rounding_or_market(self):
        self.broker.data['spec']['stops_level']='25'
        self.broker.quote('4000.80','4000.90');self.start()
        self.assertFalse(self.broker.commands);self.assertEqual(self.bot.state['phase'],'WAIT_SIGNAL')
    def test_missing_open_never_guesses_direction(self):
        self.bot.command('/start auto');self.bar();self.broker.data['candle'].pop('open');self.tick(3)
        self.assertFalse(self.broker.commands);self.assertIsNone(self.bot.state['cycle'])
    def test_manual_auto_trigger_edit_rejected_before_cancel(self):
        self.start();before=deepcopy(self.broker.data['orders'])
        with self.assertRaisesRegex(ValueError,'SEMI'):self.bot.triggers('prices',{'BUY':'4002'})
        self.assertEqual(self.broker.data['orders'],before)
    def test_explicit_delete_waits_for_start_and_new_close(self):
        self.start();order=self.broker.data['orders'][0]
        self.bot.delete_trigger(order['id'],order['price']);self.tick(3)
        self.assertEqual(self.bot.state['phase'],'PAUSED_CYCLE');self.bar(120);self.tick(3)
        self.assertFalse(self.broker.data['orders'])
        self.bot.command('/start auto');self.tick(2);self.assertFalse(self.broker.data['orders'])
        self.bar(180);self.tick(2);self.assertEqual(len(self.broker.data['orders']),1)

    def test_upgrade_preserves_old_active_symmetric_cycle_and_saved_cursor(self):
        from tradfi.engine import Controller
        old=Controller(self.store,self.broker);old.command('/start')
        self.bar();old.tick();old.tick()
        self.assertEqual(len(self.broker.data['orders']),2)
        for settings in [old.state['settings'],old.state['cycle']['settings']]:
            for key in ('INITIAL_ENTRY_DISTANCE','INITIAL_SL_DISTANCE','INITIAL_TP_DISTANCE'):settings.pop(key)
        old.state['telegram_offset']=17;old.state.pop('command_offset');old.save('old_version_fixture')
        before=deepcopy(self.broker.data['orders']);self.restart()
        self.bar(120,'4001','4000');self.tick(3)
        self.assertEqual(self.broker.data['orders'],before)
        self.assertEqual(self.bot.state['command_offset'],17)
        self.assertNotIn('telegram_offset',self.bot.state)
        self.assertFalse(self.bot.state['cycle'].get('candle_initial'))

# The same strategy through native obligation evidence and durable MT5 journal.
import test_tradfi_native_interactive as native
from unittest.mock import patch

class NativeCandleTest(unittest.TestCase):
    setUp=native.NativeInteractiveTest.setUp
    tearDown=native.NativeInteractiveTest.tearDown
    restart=native.NativeInteractiveTest.restart
    tick=native.NativeInteractiveTest.tick
    def bar(self,side='BUY'):
        self.api.now+=60
        self.api.tick=self.api.tick._replace(time_msc=self.api.now*1000)
        self.api.rates=[dict(time=int(self.api.now//60)*60-60,open=3999. if side=='BUY' else 4001.,close=4000.)]
    def start(self):
        self.bot.command('/start auto');self.bar();self.tick(2)
        self.assertEqual(len(self.api.orders),1)
    def test_native_flip_and_restart_anchor(self):
        self.start();self.bar('SELL');self.tick(4)
        self.assertEqual(len(self.api.orders),1);self.assertEqual(self.api.orders[0].type,5)
        price=self.api.orders[0].price_open;sent=len(self.api.sends);self.restart();self.tick(3)
        self.assertEqual(self.api.orders[0].price_open,price);self.assertEqual(len(self.api.sends),sent)
    def test_native_cancel_without_terminal_history_blocks_replacement(self):
        self.start();self.api.delay_cancel=True;self.bar('SELL');self.tick(4);self.restart();self.tick(3)
        self.assertFalse(self.api.orders)
        self.assertEqual(len([s for s in self.api.sends if s['action']==self.api.TRADE_ACTION_PENDING]),1)
        self.assertTrue(self.bot._unresolved())
    def test_native_fill_during_cancel_preserves_position(self):
        self.start();send=self.api.order_send
        def racing(req):
            if req['action']==self.api.TRADE_ACTION_REMOVE:
                self.api.sends.append(dict(req));self.api.fill();return None
            return send(req)
        with patch.object(self.api,'order_send',side_effect=racing):self.bar('SELL');self.tick(4)
        self.assertEqual(self.bot.state['phase'],'POSITION');self.assertEqual(len(self.api.positions),1)
        self.assertFalse(self.api.orders);self.assertEqual(len([s for s in self.api.sends if s['action']==self.api.TRADE_ACTION_PENDING]),1)
    def test_native_unknown_stop_does_not_replay(self):
        self.api.unknown=True;self.bot.command('/start auto');self.bar();self.tick(3);self.bar('SELL');self.tick(3);self.restart();self.tick(3)
        self.assertEqual(len(self.api.sends),1)
    def test_native_first_snapshot_contains_signal_before_send(self):
        self.bot.command('/start auto');self.bar();original=self.bot.save
        def crash(event):
            original(event)
            if event=='new_cycle_snapshot':raise RuntimeError('crash')
        self.bot.save=crash
        with self.assertRaises(RuntimeError):self.tick()
        self.assertEqual(self.store.load()['cycle']['candle_signal']['side'],'BUY')
        self.assertFalse(self.api.sends);self.restart();self.tick(2);self.assertEqual(len(self.api.orders),1)
