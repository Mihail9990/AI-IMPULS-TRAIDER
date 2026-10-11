"""Offline TradFi regressions: synthetic specification, no external transports."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest

from tradfi.broker import Spec, target_price, stepped_stop
from tradfi.engine import Controller
from tradfi.settings import Settings, parse_settings
from tradfi.simulator import Simulator
from tradfi.storage import Store, DurabilityError


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'state.sqlite3'); self.addCleanup(self.store.close)
        self.broker = Simulator(); self.bot = Controller(self.store, self.broker)

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

    def test_wait_new_closed_candle_and_exact_center_pair_with_protection(self):
        self.bot.command('/start'); self.tick(3)
        self.assertFalse(self.broker.commands)
        self.broker.candle(60, '4001'); self.tick()
        self.assertEqual({c['trigger'] for c in self.mutations('STOP')}, {'4004.00', '3998.00'})
        self.assertTrue(all(c['sl'] and c['tp'] for c in self.broker.commands))

    def test_normal_tp_target_money_excludes_fees_and_new_candle_after_cooldown(self):
        self.start(); self.fill(); self.broker.close('TP'); self.tick()
        self.assertEqual(self.bot.state['phase'], 'COOLDOWN')
        self.assertEqual(D(self.bot.state['completed'][0]['realized']), D('3.6'))
        self.assertEqual(self.bot.state['completed'][0]['forgiven_residual'], '0')
        self.broker.candle(119); self.tick(); self.assertEqual(len(self.mutations('STOP')), 2)
        self.broker.candle(120, '4100'); self.tick(); self.assertEqual(self.bot.state['phase'], 'WAIT_CANDLE')
        self.tick(); self.assertEqual(len(self.mutations('STOP')), 2)
        self.broker.candle(180, '4101'); self.broker.quote('4101', '4101.1'); self.tick()
        self.assertEqual(self.bot.state['cycle']['original_center'], '4101')
        self.assertEqual(len(self.mutations('STOP')), 4)

    def test_first_sl_repeats_original_trigger_then_next_sl_uses_actual_close(self):
        self.start(); original = [x['trigger'] for x in self.mutations('STOP')]
        self.fill(price='4003.10'); self.broker.close('SL', price='4001.05'); self.tick(2)
        self.assertEqual([x['trigger'] for x in self.mutations('STOP')][-2:], original)
        self.assertEqual(self.bot.state['cycle']['branch'], 'BRANCH1_RECOVERY')
        self.assertEqual(D(self.bot.state['cycle']['recovery']), D('40.50'))
        self.fill(); self.broker.close('SL', price='4000.90'); self.tick()
        self.assertEqual(self.bot.state['cycle']['branch'], 'AFTER_SL')
        self.assertEqual(self.bot.state['cycle']['center'], '4000.90')
        self.tick(); self.assertEqual(self.bot.state['cycle']['sl_distance'], '1.50')

    def test_actual_fill_reanchor_and_first_position_has_no_steps(self):
        self.start(); self.fill(price='4003.10')
        self.assertEqual(self.mutations('STOP')[0]['trigger'], '4003.00')
        p = self.broker.data['positions'][0]
        self.assertEqual(D(p['sl']), D('4001.10')); self.assertEqual(D(p['tp']), D('4003.30'))
        self.broker.quote('4003.9', '4004'); self.tick(3)
        self.assertFalse([x for x in self.mutations('MODIFY') if x['purpose'] == 'STEP'])

    def test_stepped_sl_positive_zero_negative_use_same_branch(self):
        for pnl in ('1.8', '0', '-1.8'):
            with self.subTest(pnl=pnl):
                self.setUp()
                self.recovery(); before = D(self.bot.state['cycle']['recovery'])
                entry = D(self.bot.state['cycle']['position']['entry'])
                self.broker.quote(entry + D('.8'), entry + D('.9')); self.tick(2)
                self.assertTrue(self.bot.state['cycle']['position']['stepped'])
                close = entry + D(pnl) / (D('.09') * D(self.broker.data['spec']['contract_size']))
                self.broker.close('SL', price=str(close), profit=pnl); self.tick()
                self.assertEqual(self.bot.state['cycle']['branch'], 'PARTIAL_TP')
                self.assertEqual(D(self.bot.state['cycle']['recovery']), before - D(pnl))
                self.assertEqual(D(self.bot.state['cycle']['center']), close)

    def test_tolerance_exact_one_above_one_and_ordinary_sl_not_eligible(self):
        for reason, stepped, remainder, complete in [('TP', False, '1', True),
            ('TP', False, '1.01', False), ('SL', True, '1', True), ('SL', False, '1', False)]:
            with self.subTest(reason=reason, stepped=stepped, remainder=remainder):
                self.setUp(); self.recovery()
                c = self.bot.state['cycle']; c['position']['stepped'] = stepped
                profit = D(c['recovery']) - D(remainder)
                self.broker.close(reason, profit=profit); self.tick()
                self.assertEqual(self.bot.state['cycle'] is None, complete)
                if complete: self.assertEqual(D(self.bot.state['completed'][0]['forgiven_residual']), D('1'))

    def test_tp_activation_below_equal_above_and_tp_equality_priority(self):
        for distance in (D('.69'), D('.70'), D('.71')):
            with self.subTest(distance=distance):
                self.setUp(); self.recovery()
                c = self.bot.state['cycle']; p = c['position']; entry = D(p['entry'])
                c['recovery'] = str(self.broker.profit('BUY', D(p['size']), entry, entry + distance))
                p['desired_tp'] = str(entry + distance)
                self.broker.data['positions'][0]['tp'] = str(entry + distance)
                self.broker.quote(entry + D('.70'), entry + D('.80')); self.tick()
                self.assertEqual(bool([x for x in self.mutations('MODIFY') if x['purpose'] == 'STEP']), distance > D('.70'))

    def test_sell_step_uses_ask_and_never_worsens(self):
        self.start(); self.fill('SELL'); self.broker.close('SL'); self.tick(2); self.fill('SELL')
        entry = D(self.bot.state['cycle']['position']['entry'])
        self.broker.quote(entry - D('.9'), entry - D('.7')); self.tick(2)
        self.assertEqual(D(self.broker.data['positions'][0]['sl']), entry)
        count = len(self.mutations('MODIFY'))
        self.broker.quote(entry - D('.7'), entry - D('.5')); self.tick(2)
        self.assertEqual(len(self.mutations('MODIFY')), count)

    def test_oco_race_safe_pause_keeps_protections_without_close(self):
        self.start(); self.broker.fill('BUY'); self.broker.fill('SELL'); self.tick()
        self.assertEqual(self.bot.state['phase'], 'SAFE_PAUSE')
        self.assertEqual(len(self.broker.data['positions']), 2)
        self.assertTrue(all(p['sl'] and p['tp'] for p in self.broker.data['positions']))
        count = len(self.broker.commands); self.tick(4); self.assertEqual(len(self.broker.commands), count)
        self.bot = Controller(self.store, self.broker)
        with self.assertRaises(ValueError): self.bot.command('/start')

    def test_crossed_stop_market_is_protected_and_not_replayed(self):
        self.bot.command('/start'); self.broker.candle(60); self.broker.quote('4004', '4004.1'); self.tick(4)
        self.assertEqual(len(self.mutations('MARKET')), 1)
        self.assertTrue(self.mutations('MARKET')[0]['sl'])
        self.assertEqual(len(self.broker.data['positions']), 1)
        self.bot = Controller(self.store, self.broker); self.tick(3)
        self.assertEqual(len(self.mutations('MARKET')), 1)

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

    def test_stop_open_position_finishes_logical_recovery_then_pauses(self):
        self.start(); self.fill(); self.bot.command('/stop'); self.broker.close('SL'); self.tick(2)
        self.assertEqual(self.bot.state['cycle']['branch'], 'BRANCH1_RECOVERY')
        self.fill(); self.broker.close('TP'); self.tick()
        self.assertEqual(self.bot.state['phase'], 'PAUSED')
        self.broker.candle(600); self.tick(3); self.assertEqual(len(self.mutations('STOP')), 4)

    def test_stop_between_branches_preserves_recovery_until_start(self):
        self.start(); self.fill(); self.broker.close('SL'); self.tick(); self.bot.command('/stop'); self.tick()
        self.assertEqual(self.bot.state['phase'], 'PAUSED_CYCLE')
        self.assertEqual(len(self.mutations('STOP')), 2)
        self.bot.command('/start'); self.tick(); self.assertEqual(len(self.mutations('STOP')), 4)

    def test_stop_cancel_fill_race_finishes_cycle_without_new_entry(self):
        self.start(); self.bot.command('/stop'); self.broker.cancel_race = 'BUY'; self.tick(3)
        self.assertEqual(self.bot.state['stop_mode'], 'FINISH')
        self.assertEqual(len(self.broker.data['positions']), 1)
        self.broker.close('TP'); self.tick(2)
        self.assertEqual(self.bot.state['phase'], 'PAUSED')

    def test_settings_atomic_snapshot_and_duplicate_telegram_update(self):
        self.start(); old = deepcopy(self.bot.state['cycle']['settings'])
        new = Settings().template().replace('BRANCH1_RANGE=3.00', 'BRANCH1_RANGE=4.00')
        self.bot.command(new, 10)
        self.assertEqual(self.bot.state['cycle']['settings'], old)
        self.assertEqual(self.bot.state['settings']['BRANCH1_RANGE'], '4.00')
        before = deepcopy(self.bot.state)
        with self.assertRaises(ValueError): self.bot.command(new + '\nINITIAL_SIZE=0.12', 11)
        self.assertEqual(self.bot.state, before)
        self.bot.command('/stop', 9); self.assertFalse(self.bot.state['stop'])

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

    def test_parser_rejects_nonfinite_unknown_missing_zero(self):
        base = Settings().template()
        for text in (base.replace('0.09', 'NaN'), base.replace('0.09', 'Infinity'),
                     base.replace('0.09', '0'), base + '\nUNKNOWN=1', '\n'.join(base.splitlines()[:-1])):
            with self.assertRaises(ValueError): parse_settings(text)
