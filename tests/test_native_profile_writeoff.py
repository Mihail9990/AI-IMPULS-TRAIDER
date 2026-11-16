"""Native writeoff evidence on injected MT5 bindings only, never a broker."""
from copy import deepcopy
import unittest
from unittest.mock import patch
import test_tradfi_basket as fixtures
from tradfi.control import ActionGateway
from tradfi.recovery_v3 import ControlledRecoveryController


class NativeProfileWriteoff(unittest.TestCase):
    def setUp(self):
        self.f = f = fixtures.NativeBasketTest(); f.setUp()
        self.addCleanup(f.doCleanups); self.addCleanup(f.tearDown)
        f.bot = ControlledRecoveryController(f.store, f.broker, clock=lambda: f.api.now, monotonic=lambda: f.api.now)
        f.bot.command('/start'); f.api.now += 60
        f.api.tick = f.api.tick._replace(time_msc=f.api.now*1000)
        f.api.rates = [dict(time=int(f.api.now//60)*60-60, open=3999., close=4000.)]
        f.tick(2); f.api.fill(); f.tick(3)
        f.api.close_position(reason=f.api.DEAL_REASON_SL); f.tick(4)
        f.bot.command('/stop'); f.tick(3)
        self.assertEqual(f.bot.state['phase'], 'PAUSED_CYCLE')

    def action(self):
        c = self.f.bot.state['cycle']
        return dict(type='writeoff_recovery', cycle_id=c['id'], expected_debt=c['recovery'], confirmed=True)

    def test_native_obligation_evidence_and_ledger_survive_profile_writeoff(self):
        f = self.f; cycle = deepcopy(f.bot.state['cycle']); sends = deepcopy(f.api.sends)
        ledger = f.store.db.execute('SELECT * FROM consumed_deals').fetchall()
        f.bot.action(self.action())
        self.assertEqual(f.api.sends, sends)
        self.assertEqual(f.store.db.execute('SELECT * FROM consumed_deals').fetchall(), ledger)
        self.assertEqual(f.bot.state['completed'][-1]['realized'], cycle['realized'])
        self.assertEqual(f.bot.state['phase'], 'WAIT_CANDLE')
        f.bot = ControlledRecoveryController(f.store, f.broker, clock=lambda: f.api.now, monotonic=lambda: f.api.now)
        f.tick(3)
        self.assertEqual(f.api.sends, sends)
        self.assertEqual(len(f.bot.state['recovery_writeoffs']), 1)

    def test_missing_or_stale_native_evidence_keeps_debt(self):
        f = self.f; snapshot = f.broker.snapshot
        for kind in ('quote', 'obligation'):
            with self.subTest(kind=kind):
                def altered():
                    snap = snapshot()
                    if kind == 'quote': snap['quote']['utc_time'] = None
                    else:
                        cid = f.bot.state['cycle']['entries'][0]
                        snap['obligations'][cid]['status'] = 'UNKNOWN'
                    return snap
                before = deepcopy(f.bot.state['cycle']); sends = deepcopy(f.api.sends)
                with patch.object(f.broker, 'snapshot', side_effect=altered):
                    result = ActionGateway(f.bot, execute=f.bot.action).apply(dict(
                        id='native-writeoff-'+kind, revision=f.bot.state.get('control_revision', 0), action=self.action()))
                self.assertEqual(result['status'], 'REJECTED')
                self.assertTrue(result.get('reason'))
                self.assertEqual(f.api.sends, sends)
                self.assertEqual(f.bot.state['cycle']['realized'], before['realized'])
                self.assertEqual(f.bot.state['cycle']['recovery'], before['recovery'])
                self.assertNotIn('recovery_writeoffs', f.bot.state)
                self.assertTrue(f.bot.state['stop'])
