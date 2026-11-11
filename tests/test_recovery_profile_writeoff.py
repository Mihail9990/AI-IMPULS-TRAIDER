"""Flat profile writeoff and restart contracts on offline broker facts only."""
from copy import deepcopy
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_tradfi_basket as fixtures
from tradfi.control import ActionGateway
from tradfi.recovery_v3 import ControlledRecoveryController


class ProfileWriteoff(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.BasketTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        f.bot = ControlledRecoveryController(f.store, f.b)
        self.assertEqual(f.bot.state['tp_distances'], [None] * 20)
        f.start(); f.b.fill('BUY'); f.tick(3)
        f.b.close('SL'); f.tick(4)
        f.bot.command('/stop'); f.tick(3)
        self.assertEqual(f.bot.state['phase'], 'PAUSED_CYCLE')

    def action(self):
        s = self.f.bot.state; c = s.get('cycle') or {}
        return dict(type='writeoff_recovery', cycle_id=c.get('id'),
                    expected_debt=c.get('recovery', s['basket_carry']), confirmed=True)

    def apply(self, action=None, request_id='profile-writeoff'):
        bot = self.f.bot
        return ActionGateway(bot, execute=bot.action).apply(dict(
            id=request_id, revision=bot.state.get('control_revision', 0),
            action=self.action() if action is None else action))

    def assert_refused(self, action=None, reason=None):
        f = self.f; before = deepcopy(f.bot.state); calls = deepcopy(f.b.commands)
        deals = deepcopy(f.b.data['deals'])
        result = self.apply(action)
        self.assertEqual(result['status'], 'REJECTED', result)
        self.assertTrue(result.get('reason'), result)
        if reason: self.assertIn(reason, result['reason'])
        self.assertEqual(f.b.commands, calls)
        self.assertEqual(f.b.data['deals'], deals)
        for key in ('completed', 'basket_carry', 'recovery_writeoffs'):
            self.assertEqual(f.bot.state.get(key), before.get(key))
        self.assertEqual(f.bot.state['cycle']['recovery'], before['cycle']['recovery'])
        self.assertEqual(f.bot.state['cycle']['realized'], before['cycle']['realized'])
        self.assertTrue(f.bot.state['stop'])
        return result

    def test_default_null_legacy_tp_writeoff_restart_and_next_cycle(self):
        f = self.f; bot = f.bot; old = deepcopy(bot.state['cycle'])
        calls = deepcopy(f.b.commands); deals = deepcopy(f.b.data['deals'])
        profiles = deepcopy(bot.state['recovery_profiles']); action = self.action()
        request = dict(id='profile-writeoff', revision=bot.state.get('control_revision', 0), action=action)
        gate = ActionGateway(bot, execute=bot.action)
        self.assertEqual(gate.apply(request)['status'], 'APPLIED')
        self.assertEqual(gate.apply(request)['status'], 'APPLIED')
        self.assertEqual(f.b.commands, calls); self.assertEqual(f.b.data['deals'], deals)
        self.assertEqual(bot.state['completed'][-1]['realized'], old['realized'])
        self.assertEqual(bot.state['completed'][-1]['written_off_recovery'], old['recovery'])
        self.assertEqual(bot.state['recovery_writeoffs'][-1]['amount'], old['recovery'])
        self.assertEqual(bot.state['basket_carry'], '0')
        self.assertEqual(bot.state['phase'], 'WAIT_CANDLE'); self.assertFalse(bot.state['stop'])
        self.assertIsNone(bot.state['cycle']); self.assertEqual(bot.state['recovery_profiles'], profiles)
        f.tick(3); self.assertEqual(f.b.commands, calls)
        f.bot = ControlledRecoveryController(f.store, f.b); f.tick(3)
        self.assertEqual(ActionGateway(f.bot, execute=f.bot.action).apply(request)['status'], 'APPLIED')
        self.assertEqual(len(f.bot.state['recovery_writeoffs']), 1)
        self.assertEqual(f.b.commands, calls)
        boundary = f.bot.state['last_candle']; f.bar(boundary + 60); f.tick(3)
        self.assertEqual(len(f.b.data['orders']), 1)
        self.assertNotEqual(f.bot.state['cycle']['id'], old['id'])
        self.assertEqual(D(f.bot.state['cycle']['recovery']), 0)
        self.assertEqual(f.bot.state['recovery_profiles'], profiles)
        f.b.fill('BUY'); f.tick(3); f.b.close('SL'); f.tick(4)
        f.bot.action(dict(type='v3_entry', cycle_id=f.bot.state['cycle']['id'],
                         order_type='MARKET', timing='now', side='SELL'))
        f.tick(5)
        self.assertEqual(len(f.b.data['positions']), 6)
        self.assertEqual(f.bot.state['cycle']['groups'][-1]['recovery_profile']['id'], 'six')

    def test_archived_recovery_settings_do_not_override_profile_validation(self):
        cfg = self.f.bot.state['basket_settings']
        cfg.update(TICKET_SIZE='.005', EXPANSION_DISTANCE='.005', RECOVERY_SL_DISTANCE='.005')
        self.assertEqual(self.apply()['status'], 'APPLIED')

    def test_closed_profile_snapshot_stays_historical_and_next_profile_is_validated(self):
        f = self.f; bot = f.bot; cid = bot.state['cycle']['id']
        bot.command('/start'); f.tick(2)
        bot.action(dict(type='recovery_profile_select', cycle_id=cid, profile_id='mixed'))
        bot.action(dict(type='v3_entry', cycle_id=cid, order_type='MARKET', timing='now', side='SELL'))
        f.tick(5)
        group = bot.state['cycle']['groups'][-1]
        profile = deepcopy(group['recovery_profile'])
        for p in list(f.b.data['positions']): f.b.close('SL', position_id=p['id'])
        f.tick(5)
        bot.action(dict(type='recovery_profile_select', cycle_id=cid, profile_id='single'))
        bot.command('/stop'); f.tick(3)
        # Broker changes affect future orders, not already reconciled facts.
        f.b.data['spec'].update(volume_min='.10', volume_step='.10')
        bot.state['basket_settings']['INITIAL_SIZE'] = '.10'
        self.assertEqual(self.apply()['status'], 'APPLIED')
        self.assertEqual(group['recovery_profile'], profile)
        self.assertEqual(bot.state['recovery_profiles']['selected_profile_id'], 'single')

    def test_invalid_selected_profile_keeps_debt_with_safe_reason(self):
        self.f.bot.state['recovery_profiles']['profiles'][0]['stages'][0]['volume'] = '.005'
        self.assert_refused(reason='профиль RECOVERY')

    def test_missing_selected_profile_keeps_debt_with_safe_reason(self):
        self.f.bot.state['recovery_profiles']['selected_profile_id'] = 'missing'
        self.assert_refused(reason='профиль RECOVERY')

    def test_invalid_initial_settings_keep_debt(self):
        self.f.bot.state['basket_settings']['INITIAL_SIZE'] = '.005'
        self.assert_refused(reason='начального входа')

    def test_legacy_controller_still_requires_its_tp_ladder(self):
        import test_recovery_writeoff as legacy
        case = legacy.Writeoff(); case.setUp(); self.addCleanup(case.doCleanups)
        f = case.f; bot = f.bot; bot.state['tp_distances'] = [None] * 20
        debt = bot.state['cycle']['recovery']; calls = deepcopy(f.b.commands)
        result = ActionGateway(bot, execute=bot.action).apply(dict(
            id='legacy-invalid-ladder', revision=bot.state.get('control_revision', 0), action=case.action()))
        self.assertEqual(result['status'], 'REJECTED')
        self.assertIn('сетка TP', result['reason'])
        self.assertEqual(bot.state['cycle']['recovery'], debt)
        self.assertNotIn('recovery_writeoffs', bot.state)
        self.assertEqual(f.b.commands, calls)

    def test_pending_v3_work_is_not_discarded(self):
        for key in ('edit', 'manual_close', 'initial_cancel', 'v3_edit', 'v3_request',
                    'v3_link', 'v3_rearm', 'v3_relink'):
            with self.subTest(key=key):
                x = ProfileWriteoff(); x.setUp(); self.addCleanup(x.doCleanups)
                x.f.bot.state['cycle'][key] = {'pending': True}
                x.assert_refused(reason='операция')
                self.assertEqual(x.f.bot.state['cycle'][key], {'pending': True})

    def test_pending_protection_is_not_discarded(self):
        for key, status in (('actual_ladder_tx', 'PENDING'), ('entry_sl_once', 'PREPARED'),
                            ('entry_sl_once', 'PENDING')):
            with self.subTest(key=key, status=status):
                x = ProfileWriteoff(); x.setUp(); self.addCleanup(x.doCleanups)
                x.f.bot.state['cycle']['groups'][0][key] = {'status': status}
                x.assert_refused(reason='защит')

    def test_active_safety_guards_cannot_be_cleared_by_writeoff(self):
        for flag in ('MANUAL', 'SAFE_PAUSE', 'manual', 'migration', 'time', 'readonly', 'unknown'):
            with self.subTest(flag=flag):
                x = ProfileWriteoff(); x.setUp(); self.addCleanup(x.doCleanups)
                s = x.f.bot.state
                if flag in ('MANUAL', 'SAFE_PAUSE'): s['phase'] = flag
                elif flag == 'manual': s['cycle']['manual'] = True
                elif flag == 'migration': s['migration_block'] = 'needs review'
                elif flag == 'time': s['time_block'] = {'reason': 'needs review'}
                elif flag == 'readonly': x.f.b.read_only = True
                else: next(iter(s['commands'].values()))['status'] = 'UNKNOWN'
                x.assert_refused()

    def test_deadline_crossing_runs_normal_guard_before_writeoff(self):
        bot = self.f.bot; now = bot.state['cycle']['deadline_utc'] + 1
        with patch.object(bot, 'clock', return_value=now):
            self.assert_refused(reason='защитная блокировка')
        self.assertTrue(bot.state['cycle']['manual'])
        self.assertEqual(bot.state['cycle']['manual_reason'], 'CYCLE_24H_LIMIT')

    def test_deadline_crossing_during_readback_is_latched_before_writeoff(self):
        bot = self.f.bot; deadline = bot.state['cycle']['deadline_utc']; now = [deadline - 1]
        snapshot = bot.snapshot
        def crossing():
            snap = snapshot(); now[0] = deadline + 1
            return snap
        with patch.object(bot, 'clock', side_effect=lambda: now[0]), patch.object(bot, 'snapshot', side_effect=crossing):
            self.assert_refused(reason='защитная блокировка')
        self.assertTrue(bot.state['cycle']['manual'])
        self.assertEqual(bot.state['cycle']['manual_reason'], 'CYCLE_24H_LIMIT')
        self.assertTrue(self.f.store.load()['cycle']['manual'])

    def test_time_safety_change_during_readback_is_latched_before_writeoff(self):
        bot = self.f.bot; reason = [None]; snapshot = bot.snapshot
        self.f.b.time_safety = SimpleNamespace(check=lambda: reason[0])
        def crossing():
            snap = snapshot(); reason[0] = 'UTC_TEST_DISCONTINUITY'
            return snap
        with patch.object(bot, 'snapshot', side_effect=crossing):
            # Gateway admission now also runs the clock guard, so a second check
            # may refuse earlier while keeping the same durable safety guarantee.
            self.assert_refused(reason='время')
        self.assertEqual(bot.state['time_block']['reason'], 'UTC_TEST_DISCONTINUITY')
        self.assertEqual(self.f.store.load()['time_block'], bot.state['time_block'])

    def test_readonly_change_during_readback_keeps_debt(self):
        bot = self.f.bot; snapshot = bot.snapshot
        def crossing():
            snap = snapshot(); self.f.b.read_only = True
            return snap
        with patch.object(bot, 'snapshot', side_effect=crossing):
            self.assert_refused(reason='READ_ONLY')

    def test_live_positions_or_orders_are_never_closed_or_cancelled(self):
        for kind in ('positions', 'orders'):
            with self.subTest(kind=kind):
                x = ProfileWriteoff(); x.setUp(); self.addCleanup(x.doCleanups)
                x.f.b.data[kind].append(dict(id='unexpected', owned=True))
                x.assert_refused(reason='позиции или ордера')

    def test_changed_or_missing_close_facts_keep_debt(self):
        for kind in ('missing', 'id', 'volume', 'money', 'ledger'):
            with self.subTest(kind=kind):
                x = ProfileWriteoff(); x.setUp(); self.addCleanup(x.doCleanups)
                deal = next(d for d in x.f.b.data['deals'] if d['entry'] == 'OUT')
                if kind == 'missing': x.f.b.data['deals'].remove(deal)
                elif kind == 'id': deal['id'] = 'changed'
                elif kind == 'volume': deal['volume'] = '.005'
                elif kind == 'money': deal['profit'] = '-999'
                else: x.f.bot.state['cycle']['realized'] = '-999'
                x.assert_refused(reason='закрытий')

    def test_snapshot_failure_has_public_reason_without_internal_details(self):
        with patch.object(self.f.b, 'snapshot', side_effect=OSError('private-api-diagnostic')):
            result = self.assert_refused(reason='свежие')
        self.assertNotIn('private-api-diagnostic', result['reason'])

    def test_stale_confirmation_is_not_applied(self):
        self.assert_refused(dict(self.action(), expected_debt='999'), reason='изменился')

    def test_retained_carry_with_default_profile_is_written_off_once(self):
        bot = self.f.bot; completed = deepcopy(bot.state['completed'])
        bot.state.update(cycle=None, basket_carry='.30', phase='WAIT_START', stop=True)
        bot.save('profile_carry_fixture')
        self.assertEqual(self.apply()['status'], 'APPLIED')
        self.assertEqual(bot.state['completed'], completed)
        self.assertEqual(bot.state['recovery_writeoffs'][-1]['amount'], '0.30')
        self.assertEqual(bot.state['phase'], 'WAIT_CANDLE')
