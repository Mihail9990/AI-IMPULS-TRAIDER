"""Known pre-effect input refusals release UI; uncertain effects retain UNKNOWN."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tradfi.control import ActionGateway,OwnerMailbox
from tradfi.interactive import InteractiveController
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.settings import Settings
from tradfi.storage import Store
from tradfi.strategies.fixed_recovery import RecoveryConfig

class ActionValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'s.db');self.addCleanup(self.store.close)
        self.broker=InteractiveSimulator();self.bot=InteractiveController(self.store,self.broker)
        self.gateway=ActionGateway(self.bot,execute=self.bot.action);self.index=0
    def apply(self,action):
        self.index+=1
        return self.gateway.apply(dict(id='validation-%04d'%self.index,revision=self.bot.state.get('control_revision',0),action=action))
    def command(self,text):return self.apply(dict(type='command',text=text))
    def test_bad_settings_then_corrected_settings_and_stop(self):
        settings=deepcopy(self.bot.state['settings'])
        for key,value in [('INITIAL_ENTRY_DISTANCE','bad'),('INITIAL_ENTRY_DISTANCE','NaN'),('INITIAL_ENTRY_DISTANCE','0'),('INITIAL_ENTRY_DISTANCE','-1'),('INITIAL_ENTRY_DISTANCE','1.001'),('CYCLE_COOLDOWN_SECONDS','-1'),('CYCLE_COOLDOWN_SECONDS','1.5')]:
            with self.subTest(key=key,value=value):
                text=Settings.load(settings).template().replace(key+'='+settings[key],key+'='+value)
                self.assertEqual(self.command(text)['status'],'REJECTED')
                self.assertEqual(self.bot.state['settings'],settings);self.assertEqual(self.bot.state['phase'],'IDLE');self.assertFalse(self.broker.commands)
        good=Settings.load(dict(settings,INITIAL_ENTRY_DISTANCE='1.25')).template()
        self.assertEqual(self.command(good)['status'],'APPLIED')
        self.assertEqual(self.bot.state['settings']['INITIAL_ENTRY_DISTANCE'],'1.25')
        self.assertEqual(self.command('/stop')['status'],'APPLIED');self.assertTrue(self.bot.state['stop'])
    def test_bad_settings_keys_and_missing_keys_are_rejected(self):
        text=Settings.load(self.bot.state['settings']).template()
        for bad in [text+'\nEXTRA=1',text.replace('INITIAL_SIZE=0.09\n',''),text+'\nINITIAL_SIZE=1','/invalid']:
            self.assertEqual(self.command(bad)['status'],'REJECTED')
        self.assertFalse(self.broker.commands)
    def test_mode_module_and_manual_input_validation_before_guard(self):
        actions=[dict(type='start',mode='invalid'),dict(type='start',mode=[]),
            dict(type='settings',mode='semi',engine=[],recovery=RecoveryConfig().dump()),
            dict(type='modules',engine='fixed',recovery={'parts':'x'}),
            dict(type='triggers',representation='prices',levels={'BUY':'bad'}),
            dict(type='triggers',representation='ranges',levels={'BUY':'1'}),
            dict(type='triggers',representation='prices',levels={'BAD':'1'}),
            dict(type='delete_trigger',order_id='o-test',old_price='bad'),
            dict(type='close_position',position_id='p-test',expected_size='bad',confirmed=True),
            dict(type='protection',position_id='p-test',old={'sl':'1','tp':'2'},new={'sl':'bad','tp':'2'})]
        with patch.object(self.bot,'_interactive_guard',side_effect=AssertionError('must validate before guard')):
            for action in actions:
                with self.subTest(action=action):self.assertEqual(self.apply(action)['status'],'REJECTED')
        self.assertFalse(self.broker.commands);self.assertEqual(self.bot.state['phase'],'IDLE')
    def test_invalid_reset_and_unavailable_resume_allow_next_stop(self):
        for text in ('/reset_manual','/resume_time'):
            self.assertEqual(self.command(text)['status'],'REJECTED')
            self.assertIsNone(self.bot.state['cycle']);self.assertFalse(self.broker.commands)
        self.assertEqual(self.command('/stop')['status'],'APPLIED')
    def test_late_value_error_after_save_remains_unknown_and_deduplicated(self):
        request=dict(id='after-effect-01',revision=0,action=dict(type='command',text='/stop'))
        original=self.bot.command
        def late(text):original(text);raise ValueError('after saved effect')
        with patch.object(self.bot,'command',side_effect=late) as command:
            self.assertEqual(self.gateway.apply(request)['status'],'UNKNOWN')
            self.assertEqual(self.gateway.apply(request)['status'],'UNKNOWN');self.assertEqual(command.call_count,1)
        self.assertTrue(self.store.load()['stop'])
    def test_default_gateway_also_preserves_late_value_error(self):
        request=dict(id='default-effect-01',revision=0,action=dict(type='command',text='/stop'))
        original=self.bot.command
        def late(text):original(text);raise ValueError('after saved effect')
        with patch.object(self.bot,'command',side_effect=late):
            self.assertEqual(ActionGateway(self.bot).apply(request)['status'],'UNKNOWN')
        self.assertTrue(self.store.load()['stop'])
    def test_rejection_survives_restart_and_mailbox_allows_stop(self):
        box=OwnerMailbox();bad=dict(id='bad-input-001',revision=0,action=dict(type='command',text='/SETTINGS:\nINITIAL_ENTRY_DISTANCE=bad'))
        box.submit(bad);box.drain(self.gateway,lambda:dict(action_results=self.bot.state['control_receipts']))
        self.assertEqual(box.view()['action_results'][bad['id']]['status'],'REJECTED')
        self.bot=InteractiveController(self.store,self.broker);self.gateway=ActionGateway(self.bot,execute=self.bot.action)
        self.assertEqual(self.gateway.apply(bad)['status'],'REJECTED')
        self.assertEqual(self.command('/stop')['status'],'APPLIED')
