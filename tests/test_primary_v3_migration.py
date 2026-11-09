from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_tradfi_basket as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.control import ActionRejected

class PrimaryMigration(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BasketTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.f.bot.state['tp_distances']=['.20']+[None]*19
    def load(self):
        self.f.bot=ControlledRecoveryController(self.f.store,self.f.b)
        return self.f.bot
    def test_idle_v1_upgrades_retaining_carry_and_settings(self):
        f=self.f;f.bot.state.update(recovery_mode='V1',basket_carry='12.34');before=deepcopy(f.bot.state['basket_settings']);f.bot.save('fixture')
        b=self.load();self.assertEqual(b.state['recovery_mode'],'V3');self.assertEqual(b.state['basket_carry'],'12.34');self.assertEqual(b.state['basket_settings'],before)
        self.assertFalse(b.state.get('migration_block'));self.assertFalse(b.state.get('cycle'))
    def test_active_legacy_cycle_blocks_without_mutating_obligations(self):
        for stage in ('pending','position','recovery'):
            with self.subTest(stage=stage):
                x=PrimaryMigration();x.setUp();self.addCleanup(x.doCleanups);f=x.f;f.start()
                if stage!='pending':f.b.fill('BUY');f.tick(3)
                if stage=='recovery':f.b.close('SL');f.tick(4)
                old_cycle=deepcopy(f.bot.state['cycle']);old_commands=deepcopy(f.bot.state['commands']);orders=deepcopy(f.b.data['orders']);positions=deepcopy(f.b.data['positions']);sent=len(f.b.commands)
                b=x.load();self.assertEqual(b.state['cycle'],old_cycle);self.assertEqual(b.state['commands'],old_commands)
                self.assertEqual(b.state['phase'],'MIGRATION_BLOCK');self.assertTrue(b.state['stop'])
                b.tick();b.dispatch();b.dispatch({'CANCEL'});self.assertFalse(b._mutation_allowed('MARKET'))
                for action in (dict(type='recovery_mode',mode='V3',cycle_id=old_cycle['id']),dict(type='manual_close')):
                    with self.assertRaises(ActionRejected):b.action(action)
                with self.assertRaises(ActionRejected):b.command('/start')
                self.assertEqual(len(f.b.commands),sent);self.assertEqual(f.b.data['orders'],orders);self.assertEqual(f.b.data['positions'],positions)
                self.assertEqual(b.state['cycle']['recovery'],old_cycle['recovery'])
                x.load().tick();self.assertEqual(len(f.b.commands),sent)
    def test_orphan_ready_never_dispatches(self):
        f=self.f;f.bot.state['commands']['old']=dict(id='old',kind='STOP',status='READY',side='BUY',size='.01',price='4001',sl='3998',tp='4002');f.bot.save('fixture')
        b=self.load();b.dispatch();b.tick();self.assertEqual(b.state['commands']['old']['status'],'READY');self.assertFalse(f.b.commands);self.assertTrue(b.state.get('migration_block'))
    def test_no_legacy_manual_close_direct_or_api(self):
        b=self.load()
        for fn in (lambda:b.action({'type':'manual_close'}),lambda:b.request_manual_close({})):
            with self.assertRaises(ActionRejected):fn()
        for mode in ('V1','V3','V3.3'):
            with self.assertRaises(ActionRejected):b.action(dict(type='recovery_mode',mode=mode,cycle_id=None))

    def test_paper_legacy_restart_blocks_special_mutations(self):
        from test_tradfi_paper import PaperTest
        f=PaperTest();f.setUp();self.addCleanup(f.doCleanups);self.addCleanup(f.tearDown)
        f.initial();cycle=deepcopy(f.session.controller.state['cycle']);positions=deepcopy(f.p['positions'])
        f.session.close();f.primary_v3=True;f.create();f.step()
        self.assertTrue(f.session.controller.state.get('migration_block'))
        self.assertEqual(f.session.controller.state['cycle'],cycle);self.assertEqual(f.p['positions'],positions)
        for a in ({'type':'paper_flatten','confirmed':True},{'type':'paper_resume'},{'type':'command','text':'/reset_manual'}):
            with self.assertRaises(ActionRejected):f.action(a)
        self.assertEqual(f.p['positions'],positions)

    def test_cli_simulator_uses_primary_v3(self):
        import io,json
        from contextlib import redirect_stdout
        from tradfi.runtime import main
        out=io.StringIO()
        with redirect_stdout(out):main(['--mode','simulate'])
        result=json.loads(out.getvalue())
        self.assertEqual(result['recovery_mode'],'V3');self.assertTrue(result['completed'])
