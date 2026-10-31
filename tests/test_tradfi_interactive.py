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


    def test_manual_close_unknown_then_sl_stops_after_confirmed_actual_loss(self):
        self.recovery();p=self.broker.data['positions'][0];self.broker.fail_before.add('CLOSE')
        self.bot.close_position(p['id'],p['size'])
        self.broker.close('SL');self.tick(3)
        self.assertFalse(self.broker.data['orders'])
        self.assertEqual(self.bot.state['phase'],'WAIT_TRIGGER')
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),108)


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


    def test_web_action_crash_after_execution_does_not_replay(self):
        from tradfi.storage import DurabilityError
        from tradfi.control import ActionGateway
        self.recovery();p=self.broker.data['positions'][0]
        request=dict(id='close-test-44',revision=0,action=dict(type='close_position',position_id=p['id'],expected_size=p['size'],confirmed=True))
        def crash(event):
            if event=='control_action_completed':raise RuntimeError('power loss')
        self.store.fault=crash
        with self.assertRaises(DurabilityError):ActionGateway(self.bot,execute=self.bot.action).apply(request)
        self.store.fault=None;self.bot=InteractiveController(self.store,self.broker)
        result=ActionGateway(self.bot,execute=self.bot.action).apply(request);self.tick(3)
        self.assertEqual(result['status'],'UNKNOWN')
        self.assertEqual(len([c for c in self.broker.commands if c['kind']=='CLOSE']),1)


    def test_actual_cost_residual_one_cent_is_not_forgiven(self):
        self.recovery();self.broker.close('TP',profit='54',commission='-0.01');self.tick(2)
        self.assertEqual(D(self.bot.state['cycle']['fixed_recovery']['debt']),D('.01'))
        self.assertFalse(self.bot.state['completed'])
        self.fill('BUY');self.broker.close('TP');self.tick(2)
        self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.bot.state['completed'][-1]['forgiven_residual'],'0')


    def test_time_latch_retained_across_active_protection_and_restart(self):
        self.recovery();self.bot.state['time_block']={'reason':'EXTERNAL_UTC_UNVERIFIED'};self.bot.save('test_latch')
        p=self.broker.data['positions'][0]
        self.bot.protection(p['id'],{k:p[k] for k in ('sl','tp')},dict(sl='4136.5',tp='4140.5'));self.tick(2)
        self.bot=InteractiveController(self.store,self.broker);self.tick(2)
        self.assertEqual(D(p['sl']),D('4136.5'))
        self.assertIn('time_block',self.bot.state)
        with self.assertRaises(ValueError):self.bot.command('/start semi')
