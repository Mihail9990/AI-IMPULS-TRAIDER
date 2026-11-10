"""Offline TradFi regressions: synthetic specification, no external transports."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from tradfi.broker import Spec, target_price
from tradfi.engine import Controller
from tradfi.settings import Settings
from tests.support.synthetic_broker import SyntheticBroker
from tradfi.storage import Store, DurabilityError


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.sqlite3'); self.addCleanup(self.store.close)
        self.broker = SyntheticBroker(); self.bot = Controller(self.store, self.broker)

    def tick(self, n=1):
        for _ in range(n): self.bot.tick()

    def start(self):
        self.bot.command('/start'); self.broker.candle(60); self.tick()

    def fill(self, side='BUY', price=None):
        self.broker.fill(side, price)
        actual = self.broker.data['positions'][0]['entry']
        self.broker.quote(D(actual) - D('.05'), D(actual) + D('.05'))
        self.tick(4)

    def recovery(self):
        self.start(); self.fill(); self.broker.close('SL'); self.tick(2)
        self.fill()

    def mutations(self, kind):
        return [c for c in self.broker.commands if c['kind'] == kind]


    def test_oco_race_safe_pause_keeps_protections_without_close(self):
        self.start(); self.broker.fill('BUY'); self.broker.fill('SELL'); self.tick()
        self.assertEqual(self.bot.state['phase'], 'SAFE_PAUSE')
        self.assertEqual(len(self.broker.data['positions']), 2)
        self.assertTrue(all(p['sl'] and p['tp'] for p in self.broker.data['positions']))
        count = len(self.broker.commands); self.tick(4); self.assertEqual(len(self.broker.commands), count)
        self.bot = Controller(self.store, self.broker)
        with self.assertRaises(ValueError): self.bot.command('/start')


    def test_unknown_request_no_fallback_duplicate_or_new_cycle(self):
        self.broker.fail_before.add('STOP'); self.start(); self.tick()
        self.bot = Controller(self.store, self.broker); self.tick(5)
        self.assertEqual(len(self.mutations('STOP')), 2); self.assertFalse(self.mutations('MARKET'))
        self.assertEqual(self.bot.state['phase'], 'PAIR')

    def test_unknown_response_reconciles_by_order_identity(self):
        self.broker.fail_after.add('STOP'); self.start()
        self.bot = Controller(self.store, self.broker); self.tick(2)
        self.assertEqual(len(self.mutations('STOP')), 2)
        self.fill(); self.assertEqual(self.bot.state['phase'], 'POSITION')

    def test_duplicate_deal_snapshot_does_not_double_recovery(self):
        self.start(); self.fill(); self.broker.close('SL'); self.tick()
        remaining = self.bot.state['cycle']['recovery']
        self.bot = Controller(self.store, self.broker); self.tick(4)
        self.assertEqual(self.bot.state['cycle']['recovery'], remaining)
        self.assertEqual(len(self.mutations('STOP')), 4)

    def test_stop_pending_cancels_both_and_restart_does_not_rearm(self):
        self.start(); self.bot.command('/stop'); self.tick(4)
        self.assertEqual(len(self.mutations('CANCEL')), 2)
        self.assertEqual(self.bot.state['phase'], 'PAUSED_CYCLE')
        self.bot = Controller(self.store, self.broker); self.tick(3)
        self.assertEqual(len(self.mutations('STOP')), 2)
        self.bot.command('/start'); self.tick(); self.assertEqual(len(self.mutations('STOP')), 4)


    def test_stop_cancel_fill_race_finishes_cycle_without_new_entry(self):
        self.start(); self.bot.command('/stop'); self.broker.cancel_race = 'BUY'; self.tick(3)
        self.assertEqual(self.bot.state['stop_mode'], 'FINISH')
        self.assertEqual(len(self.broker.data['positions']), 1)
        self.broker.close('TP'); self.tick(2)
        self.assertEqual(self.bot.state['phase'], 'PAUSED')


    def test_commit_failure_before_intent_sends_nothing(self):
        self.bot.command('/start'); self.broker.candle(60)
        def fault(event):
            if event == 'pair_intents_atomic': raise OSError('disk full')
        self.store.fault = fault
        with self.assertRaises(DurabilityError): self.tick()
        self.assertFalse(self.broker.commands)
        self.store.fault = None; self.bot = Controller(self.store, self.broker); self.tick()
        self.assertEqual(len(self.mutations('STOP')), 2)

    def test_unknown_modify_readback_confirmed_on_restart_without_duplicate(self):
        self.broker.fail_after.add('MODIFY'); self.start(); self.fill(price='4003.10')
        count = len(self.mutations('MODIFY')); self.bot = Controller(self.store, self.broker); self.tick(3)
        self.assertTrue(self.bot.state['cycle']['position']['reanchored'])
        self.assertEqual(len(self.mutations('MODIFY')), count)

    def test_partial_fill_pauses_without_refill(self):
        self.start(); self.broker.fill('BUY', size='.03'); self.tick()
        self.assertEqual(self.bot.state['phase'], 'SAFE_PAUSE'); self.assertFalse(self.mutations('MARKET'))

    def test_real_spec_validation_no_assumed_lot_step_currency_or_rounding(self):
        spec = Spec.load(self.broker.data['spec']); spec.validate(Settings())
        for key, value in [('INITIAL_SIZE', '.10'), ('BRANCH1_RANGE', '3.001')]:
            data = Settings().dump(); data[key] = value
            with self.assertRaises(ValueError): spec.validate(Settings.load(data))
        altered = deepcopy(self.broker.data['spec']); altered['currency'] = 'EUR'
        with self.assertRaises(ValueError): Spec.load(altered)
        altered = deepcopy(self.broker.data['spec']); altered['tick_size'] = '.05'
        spec = Spec.load(altered)
        tp = target_price(self.broker, spec, 'BUY', D('.09'), D('4000'), D('1'))
        self.assertEqual(tp, D('4000.10'))
