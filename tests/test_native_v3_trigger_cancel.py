"""V3 cancellation through injected native MT5 evidence; never a real broker."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import test_tradfi_basket as fixtures
from tradfi.recovery_v3 import ControlledRecoveryController


class NativeV3TriggerCancel(unittest.TestCase):
    def setUp(self):
        self.f = f = fixtures.NativeBasketTest(); f.setUp()
        self.addCleanup(f.doCleanups); self.addCleanup(f.tearDown)
        self.restart()
        f.bot.command('/start'); f.api.now += 60
        f.api.tick = f.api.tick._replace(time_msc=f.api.now * 1000)
        f.api.rates = [dict(time=int(f.api.now // 60) * 60 - 60, open=3999., close=4000.)]
        f.tick(2); f.api.fill(); f.tick(3)
        f.api.close_position(reason=f.api.DEAL_REASON_SL); f.tick(4)
        self.assertEqual(f.bot.evidence_contract, 'per_obligation_v1')
        self.assertEqual(f.bot.state['phase'], 'V3_WAIT_ENTRY')
        f.bot.action(self.action('recovery_profiles', selected_profile_id='native',
            profiles=[dict(id='native', name='Native', stages=[
                dict(volume='.01', tp_distance=d) for d in ('.20', '.40', '.60')])],
            recovery_sl_distance='1.50'))
        f.bot.action(self.action('v3_entry', order_type='STOP', timing='current', side='SELL'))
        target = f.bot.state['cycle']['v3_request']['bar_id']
        f.api.now = target + 60
        f.api.tick = f.api.tick._replace(time_msc=f.api.now * 1000, bid=3999.7, ask=3999.8)
        f.api.rates = [dict(time=target, open=4001., close=4000.)]
        f.tick(3)
        self.assertEqual(len(f.api.orders), 3)

    def restart(self):
        f = self.f
        f.bot = ControlledRecoveryController(f.store, f.broker,
            clock=lambda: f.api.now, monotonic=lambda: f.api.now)

    def action(self, kind='v3_cancel_entry', **kwargs):
        return dict(type=kind, cycle_id=self.f.bot.state['cycle']['id'], **kwargs)

    def test_cancel_pending_native_orders_preserves_debt_and_ledger(self):
        f = self.f; debt = f.bot.state['cycle']['recovery']
        ledger = f.store.db.execute('SELECT * FROM consumed_deals').fetchall()
        before = len(f.api.sends)
        f.bot.action(self.action()); f.tick(5)
        self.assertFalse(f.api.orders); self.assertFalse(f.api.positions)
        self.assertEqual(f.bot.state['phase'], 'V3_WAIT_ENTRY')
        self.assertEqual(f.bot.state['cycle']['recovery'], debt)
        self.assertEqual(f.store.db.execute('SELECT * FROM consumed_deals').fetchall(), ledger)
        self.assertEqual([r['action'] for r in f.api.sends[before:]], [f.api.TRADE_ACTION_REMOVE] * 3)
        sends = deepcopy(f.api.sends); self.restart(); f.tick(4)
        self.assertEqual(f.api.sends, sends)

    def test_filled_middle_stage_keeps_native_protection_across_restart(self):
        f = self.f; f.api.fill(index=1)
        position = deepcopy(f.api.positions[0]); before = len(f.api.sends)
        with patch.object(f.bot, '_cancel'):
            f.bot.action(self.action())
        self.restart(); f.tick(6)
        self.assertFalse(f.api.orders)
        self.assertEqual(f.api.positions, [position])
        self.assertEqual(f.bot.state['phase'], 'RECOVERY_POSITION')
        self.assertFalse(f.bot.state['cycle'].get('manual'))
        ticket = next(t for t in f.bot.state['cycle']['tickets'].values() if not t['closed'])
        self.assertEqual(ticket['index'], 1)
        self.assertEqual([r['action'] for r in f.api.sends[before:]], [f.api.TRADE_ACTION_REMOVE] * 2)

    def test_missing_native_cancel_history_blocks_reentry_without_retry(self):
        f = self.f; f.api.delay_cancel = True
        f.bot.action(self.action()); f.tick(4)
        sends = deepcopy(f.api.sends); self.restart(); f.tick(4)
        self.assertEqual(f.api.sends, sends)
        self.assertTrue(f.bot.state['cycle']['groups'][-1]['cancel_entry'])
        self.assertFalse(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_enter'])
        self.assertFalse(f.api.positions)

    def test_partial_volume_requires_manual_review_and_preserves_native_position(self):
        f = self.f; f.api.fill(index=1, volume=.005)
        position = deepcopy(f.api.positions[0]); before = len(f.api.sends)
        f.bot.action(self.action()); f.tick(5)
        self.assertEqual(f.bot.state['phase'], 'MANUAL')
        self.assertFalse(f.api.orders)
        self.assertEqual(f.api.positions, [position])
        self.assertTrue(all(r['action'] == f.api.TRADE_ACTION_REMOVE for r in f.api.sends[before:]))
        sends = deepcopy(f.api.sends); self.restart(); f.tick(4)
        self.assertEqual(f.api.sends, sends)
        self.assertEqual(f.api.positions, [position])
