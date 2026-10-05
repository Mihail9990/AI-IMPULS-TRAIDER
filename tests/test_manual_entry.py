"""Manual-entry integration and Telegram payload tests. All transports are mocked."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import json
import unittest
from unittest.mock import Mock, patch

import test_initial_entry as fixture
from trader.capital import CapitalError
from trader.config import Settings
from trader.manual_entry import ManualInitialEntry, parse_manual_trigger, TRIGGER_TEMPLATE
from trader.model import CycleState
from trader.storage import StateStore, StorageFailure
from trader.telegram import Telegram


def command(level):
    return f'/TRIGGER:\nBUY: {level}\nSELL: {level}'


class ManualEntryTest(unittest.TestCase):
    def setUp(self):
        self.f = fixture.InitialEntryTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.cfg = replace(self.f.cfg, manual_initial_entry_enabled=True)
        self.f.bot = self.f.make_bot(CycleState())
        self.addCleanup(patch.stopall)
        patch('trader.manual_entry.begin_diagnostic_cycle').start()

    @property
    def bot(self):
        return self.f.bot

    @property
    def broker(self):
        return self.f.broker

    def start(self, level='4005'):
        self.bot.command('/start')
        self.bot.command(command(level))
        self.f.ticks()

    def test_flag_default_false_and_existing_automatic_path(self):
        self.assertFalse(Settings().manual_initial_entry_enabled)
        self.bot.cfg = replace(self.bot.cfg, manual_initial_entry_enabled=False)
        self.bot._tick_filter = Mock()
        self.bot.command('/start')
        self.bot.tick()
        self.bot._tick_filter.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, 'выключен'):
            self.bot.command(command('4005'))

    def test_start_waits_without_filter_or_orders_manual_priority(self):
        self.bot.command('/start')
        self.f.ticks(5)
        self.assertEqual(self.bot.state.phase, 'MANUAL_TRIGGER_WAIT')
        self.assertEqual(self.broker.candle_calls, 0)
        self.assertFalse(self.broker.posts)
        self.assertTrue(self.bot.state.armed)
        saved = CycleState.load(self.bot.cfg.state_file)
        self.assertTrue(saved.manual_initial_mode)
        self.assertTrue(saved.armed)

    def test_bare_trigger_only_queues_copy_form(self):
        before = deepcopy(self.bot.state)
        self.bot.command('/TRIGGER')
        self.bot.telegram.send_trigger_form.assert_called_once()
        self.assertEqual(self.bot.state, before)
        self.assertFalse(self.broker.posts)

    def test_filled_message_requires_start(self):
        with self.assertRaisesRegex(RuntimeError, '/start'):
            self.bot.command(command('4005'))
        self.assertFalse(self.broker.posts)
        self.assertFalse(self.bot.state.initial_entry)

    def test_incomplete_previous_cycle_cannot_be_erased_by_new_command(self):
        self.bot.command('/start')
        for field, value in [('pending_finalization', {'kind': 'NORMAL_TP'}),
                             ('pending_close_unknown_delete', True)]:
            with self.subTest(field=field):
                old = getattr(self.bot.state, field)
                setattr(self.bot.state, field, value)
                before = deepcopy(self.bot.state)
                with self.assertRaisesRegex(RuntimeError, 'заблокирована'):
                    self.bot.command(command('4005'))
                self.assertEqual(self.bot.state, before)
                setattr(self.bot.state, field, old)
        self.assertFalse(self.broker.posts)

    def test_command_before_startup_reconciliation_is_blocked(self):
        self.bot.command('/start')
        self.bot.reconciled = False
        with self.assertRaisesRegex(RuntimeError, 'startup'):
            self.bot.command(command('4005'))
        self.assertFalse(self.broker.posts)

    def test_exact_price_above_quote_no_filter(self):
        self.start('4005.00')
        self.assertEqual({(p[0], p[2], p[3]) for p in self.broker.posts},
                         {('BUY', D('4005'), 'STOP'), ('SELL', D('4005'), 'LIMIT')})
        self.assertEqual(self.broker.candle_calls, 0)
        self.assertFalse(self.f.protections)
        self.assertFalse(self.broker.markets)

    def test_exact_price_below_quote(self):
        self.start('3998.50')
        self.assertEqual({(p[0], p[2], p[3]) for p in self.broker.posts},
                         {('BUY', D('3998.50'), 'LIMIT'), ('SELL', D('3998.50'), 'STOP')})

    def test_invalid_messages_do_not_mutate_armed_state(self):
        self.bot.command('/start')
        bad = ['/TRIGGER:\nBUY: 4005\nSELL: 4006', '/TRIGGER:\nBUY: 4005',
               command('NaN'), command('Infinity'), command('-1'), command('0'),
               '/TRIGGER:\nBUY: 4005\nBUY: 4005', command('4,005'), command('1e3')]
        for text in bad:
            with self.subTest(text=text):
                before = deepcopy(self.bot.state)
                with self.assertRaises(ValueError):
                    self.bot.command(text)
                self.assertEqual(self.bot.state, before)
        self.assertFalse(self.broker.posts)

    def test_invalid_replacement_keeps_old_orders_unchanged(self):
        self.start()
        for level in ('4001.60', '4005.001'):
            before = deepcopy(self.bot.state)
            orders = deepcopy(self.broker.live_orders)
            with self.assertRaises(ValueError):
                self.bot.command(command(level))
            self.assertEqual(self.bot.state, before)
            self.assertEqual(self.broker.live_orders, orders)
        self.assertFalse(self.broker.cancels)

    def test_different_actual_entries_handoff_once(self):
        self.start()
        self.broker.fill('BUY', '4005.07')
        self.f.ticks(2)
        self.assertFalse(self.f.protections)
        self.broker.fill('SELL', '4004.95')
        self.f.ticks()
        self.assertEqual(self.bot.state.scenario, 1)
        self.assertEqual(self.bot.state.general_recovery, D('4.2'))
        self.assertEqual(len(self.f.protections), 2)
        self.assertFalse(self.broker.markets)
        self.assertFalse(self.bot.state.initial_entry)
        with self.assertRaisesRegex(RuntimeError, 'Цикл уже'):
            self.bot.command(command('4006'))

    def test_one_cycle_consumes_price_waits_for_new_command(self):
        self.start()
        self.broker.fill('BUY', '4005.07')
        self.broker.fill('SELL', '4004.95')
        self.f.ticks()
        self.broker.live_positions.clear()
        self.bot._complete_cycle('BUY', D('4008'))
        self.assertEqual(self.bot.state.phase, 'MANUAL_TRIGGER_WAIT')
        self.f.ticks(5)
        self.assertEqual(self.bot.state.phase, 'MANUAL_TRIGGER_WAIT')
        self.assertEqual(len(self.broker.posts), 2)
        self.assertEqual(self.broker.candle_calls, 0)
        self.bot.command(command('4007'))
        self.assertEqual(len(self.broker.posts), 4)

    def test_continuation_filter_not_intercepted(self):
        self.bot.state.continuation_managed = True
        self.bot.capital.candle_ranges = Mock(return_value=(D('4'), D('1')))
        starter = Mock()
        self.bot._tick_filter(starter)
        starter.assert_called_once()
        self.assertFalse(self.broker.posts)

    def test_replace_requires_two_positive_cancels_and_three_flat_reads(self):
        self.start()
        self.bot.command(command('4006'))
        self.assertEqual(len(self.broker.posts), 2)
        self.assertEqual(len(self.broker.cancels), 1)
        self.f.ticks()
        self.assertEqual(len(self.broker.cancels), 2)
        self.f.ticks(2)
        self.assertEqual(len(self.broker.posts), 2)
        self.f.ticks()
        self.assertEqual(len(self.broker.posts), 4)
        self.assertEqual({p[2] for p in self.broker.posts[-2:]}, {D('4006')})

    def test_latest_valid_replacement_wins_and_identical_command_noop(self):
        self.start()
        self.bot.command(command('4005.0'))
        self.assertFalse(self.broker.cancels)
        self.bot.command(command('4006'))
        self.bot.command(command('4007'))
        self.bot.command(command('4007.00'))
        self.f.ticks(5)
        self.assertEqual(len(self.broker.posts), 4)
        self.assertEqual({p[2] for p in self.broker.posts[-2:]}, {D('4007')})

    def test_replacement_restart_preserves_pending_price_and_owner(self):
        self.start()
        self.bot.command(command('4006'))
        self.f.restart()
        self.f.ticks(5)
        self.assertEqual(len(self.broker.posts), 4)
        self.assertEqual(self.bot.state.initial_entry['level'], '4006')

    def test_unknown_cancel_never_opens_replacement_even_empty_snapshot(self):
        self.start()
        self.broker.cancel_failure = CapitalError('transport timeout')
        self.bot.command(command('4006'))
        self.broker.empty_snapshot = True
        self.f.restart()
        self.f.ticks(8)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertTrue(self.bot.state.initial_entry)
        self.assertFalse(self.broker.markets)

    def test_fill_during_replacement_cancellation_discards_new_price(self):
        self.start()
        self.broker.cancel_race = True
        self.bot.command(command('4006'))
        self.f.ticks(2)
        self.assertEqual(self.bot.state.initial_entry['replacement_level'], '')
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertFalse(self.broker.markets)
        remaining = next(iter(self.broker.live_orders.values()))['direction']
        self.broker.fill(remaining, '4004.9')
        self.f.ticks()
        self.assertEqual(self.bot.state.scenario, 1)
        self.assertEqual(len(self.broker.posts), 2)

    def test_replacement_fill_after_other_cancel_only_missing_market(self):
        self.start()
        self.bot.command(command('4006'))
        self.broker.cancel_race = True
        self.f.ticks(4)
        self.assertEqual(len(self.broker.markets), 1)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertEqual(self.bot.state.scenario, 1)

    def test_command_after_first_fill_does_not_cancel_waiting_side(self):
        self.start()
        self.broker.fill('BUY')
        with self.assertRaisesRegex(RuntimeError, 'fill'):
            self.bot.command(command('4006'))
        self.assertFalse(self.broker.cancels)
        self.assertFalse(self.broker.markets)
        self.assertEqual(self.bot.state.initial_entry['level'], '4005')

    def test_explicit_reject_before_fills_waits_for_new_command(self):
        self.broker.post_failure['SELL'] = CapitalError('rejected', outcome='REJECTED')
        self.start()
        self.f.ticks(8)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertEqual(self.bot.state.phase, 'MANUAL_TRIGGER_WAIT')
        self.assertFalse(self.bot.state.initial_entry)
        self.broker.post_failure.clear()
        self.bot.command(command('4006'))
        self.assertEqual(len(self.broker.posts), 4)

    def test_rejection_with_fill_uses_only_missing_market(self):
        self.broker.post_failure['SELL'] = CapitalError('rejected', outcome='REJECTED')
        self.broker.cancel_race = True
        self.start()
        self.f.ticks(3)
        self.assertEqual([m[0] for m in self.broker.markets], ['SELL'])
        self.assertEqual(self.bot.state.scenario, 1)
        self.assertEqual(len(self.broker.posts), 2)

    def test_unknown_initial_post_survives_restart_no_duplicate(self):
        self.broker.post_failure['BUY'] = CapitalError('transport timeout')
        self.start()
        self.f.restart()
        self.f.ticks(5)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertFalse(self.broker.markets)
        self.assertTrue(self.bot.state.initial_entry)

    def test_stop_before_command_persists_and_requires_start(self):
        self.bot.command('/start')
        self.bot.command('/stop')
        self.f.restart()
        with self.assertRaisesRegex(RuntimeError, '/start'):
            self.bot.command(command('4005'))
        self.bot.command('/start')
        self.bot.command(command('4005'))
        self.assertEqual(len(self.broker.posts), 2)

    def test_stop_without_fills_cancels_both_and_discards_replacement(self):
        self.start()
        self.bot.command(command('4006'))
        self.bot.command('/stop')
        self.f.restart()
        self.f.ticks(5)
        self.assertEqual(self.bot.state.phase, 'PAUSED')
        self.assertFalse(self.bot.state.armed)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertFalse(self.bot.state.initial_entry)

    def test_stop_after_first_fill_waits_then_finishes_paused(self):
        self.start()
        self.broker.fill('BUY')
        self.bot.command('/stop')
        self.f.restart()
        self.assertFalse(self.broker.cancels)
        self.assertFalse(self.f.protections)
        self.broker.fill('SELL')
        self.f.ticks()
        self.assertTrue(self.bot.state.paused)
        self.assertEqual(self.bot.state.scenario, 1)
        self.broker.live_positions.clear()
        self.bot._complete_cycle('BUY', D('4008'))
        self.f.ticks(3)
        self.assertEqual(self.bot.state.phase, 'PAUSED')
        with self.assertRaisesRegex(RuntimeError, '/start'):
            self.bot.command(command('4006'))

    def test_stop_cancel_fill_race_keeps_old_pair(self):
        self.start()
        self.broker.cancel_race = True
        self.bot.command('/stop')
        self.f.ticks(4)
        self.assertEqual(len(self.broker.cancels), 1)
        self.assertFalse(self.broker.markets)
        self.assertTrue(self.bot.state.paused)

    def test_replacement_price_revalidated_after_cancellation(self):
        self.start()
        self.bot.command(command('4006'))
        self.broker.market = fixture.market('4005.9', '4006.1')
        self.f.ticks(6)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertEqual(self.bot.state.phase, 'MANUAL_TRIGGER_WAIT')
        self.assertFalse(self.bot.state.initial_entry)

    def test_manual_owner_survives_flag_disabled_on_restart(self):
        self.start()
        self.f.cfg = replace(self.f.cfg, manual_initial_entry_enabled=False)
        self.f.restart()
        self.broker.fill('BUY')
        self.broker.fill('SELL')
        self.f.ticks()
        self.assertEqual(self.bot.state.scenario, 1)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertTrue(self.bot.state.manual_initial_mode)

    def test_missing_first_position_never_permits_orphan_market(self):
        self.start()
        pid = self.broker.fill('BUY')
        self.f.ticks()
        self.broker.live_positions.pop(pid)
        self.broker.cancel_externally('SELL')
        self.f.restart()
        self.f.ticks(3)
        self.assertFalse(self.broker.markets)

    def test_partial_first_position_never_permits_oversize_market(self):
        self.start()
        self.broker.fill('BUY', size='5', keep_order=True)
        self.f.ticks()
        self.broker.cancel_externally('BUY')
        self.broker.cancel_externally('SELL')
        self.f.ticks(3)
        self.assertFalse(self.broker.markets)
        self.assertFalse(self.f.protections)

    def test_first_position_reduced_without_order_link_blocks_market(self):
        self.start()
        pid = self.broker.fill('BUY')
        self.f.ticks()
        self.broker.live_positions[pid].pop('workingOrderId')
        self.broker.live_positions[pid]['size'] = D('5')
        self.broker.cancel_externally('SELL')
        self.f.ticks(3)
        self.assertFalse(self.broker.markets)

    def test_scenario_nine_finalization_waits_for_fresh_command(self):
        self.start()
        self.broker.fill('BUY')
        self.broker.fill('SELL')
        self.f.ticks()
        self.broker.live_positions.clear()
        self.bot.state.active = False
        self.bot.state.scenario = 9
        self.bot.state.completed_cycles = 1
        self.bot.state.pending_finalization = {'kind': 'SCENARIO_9', 'attempt_id': 1,
            'long_fill': '3990', 'short_fill': '4010', 'paused': False}
        self.bot._resume_scenario_nine_finalization()
        self.assertEqual(self.bot.state.phase, 'MANUAL_TRIGGER_WAIT')
        self.f.restart()
        self.f.ticks(4)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertEqual(self.broker.candle_calls, 0)

    def test_late_residual_keeps_manual_owner_until_resolved(self):
        self.start()
        saved = next(dict(o) for o in self.broker.live_orders.values() if o['direction'] == 'BUY')
        self.broker.fill('BUY')
        self.f.ticks()
        self.broker.live_orders[saved['dealId']] = saved
        self.broker.fill('SELL')
        self.f.ticks()
        self.assertTrue(self.bot.state.initial_entry)
        self.assertFalse(self.f.protections)

    def test_partial_other_side_with_cancel_history_never_gets_extra_market(self):
        self.start()
        self.broker.fill('BUY')
        self.broker.fill('SELL', size='5', keep_order=True)
        oid = next(iter(self.broker.live_orders))
        self.broker.history.append({'dealId': oid, 'type': 'WORKING_ORDER',
                                   'status': 'CANCELLED', 'dateUTC': '2026-10-05T10:02:00Z'})
        self.f.ticks()
        self.f.restart()
        self.f.ticks(3)
        self.assertFalse(self.broker.markets)
        self.assertFalse(self.f.protections)
        self.assertTrue(self.bot.state.initial_entry)

    def test_unknown_fallback_survives_restart_without_retry(self):
        self.start()
        self.broker.fill('BUY')
        self.broker.cancel_externally('SELL')
        self.broker.market_failure = CapitalError('transport timeout')
        self.f.ticks()
        self.f.restart()
        self.f.ticks(4)
        self.assertEqual(len(self.broker.markets), 1)
        self.assertTrue(self.bot.state.initial_entry)
        self.assertFalse(self.f.protections)

    def test_stop_after_cancel_race_completes_missing_side_then_remains_paused(self):
        self.start()
        self.bot.command('/stop')
        self.f.ticks()
        self.broker.cancel_race = True
        self.f.ticks(4)
        self.assertEqual(len(self.broker.markets), 1)
        self.assertFalse(self.bot.state.initial_entry)
        self.assertTrue(self.bot.state.paused)
        self.assertEqual(self.bot.state.scenario, 1)

    def test_post_commit_failure_restores_intents_and_blocks_duplicates(self):
        self.bot.command('/start')
        original = StateStore.save
        def save(store, payload, **kwargs):
            fail = any(o.get('reference') for o in payload.get('initial_entry', {}).get('orders', {}).values())
            return original(store, payload, fault=lambda stage: (
                (_ for _ in ()).throw(RuntimeError('SQL failure')) if stage == 'before_commit' and fail else None
            ), **kwargs)
        with patch.object(StateStore, 'save', new=save):
            with self.assertRaises(StorageFailure):
                self.bot.command(command('4005'))
        self.f.restart()
        self.f.ticks(3)
        self.assertEqual(len(self.broker.posts), 2)
        self.assertTrue(self.bot.state.initial_entry)


class ManualTelegramTest(unittest.TestCase):
    def test_parser_case_whitespace_order_and_finite(self):
        self.assertEqual(parse_manual_trigger(' \n /tRiGgEr :\n Sell : 3998.500 \n BUY:3998.50\n'), D('3998.50'))
        self.assertEqual(TRIGGER_TEMPLATE, '/TRIGGER:\nBUY:\nSELL:')

    def test_copy_text_payload_and_instruction_without_network(self):
        telegram = Telegram('fake', '123')
        telegram.send_trigger_form()
        item = telegram._messages[0]
        with patch('trader.telegram.requests.post') as post:
            telegram._deliver_message(item)
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['reply_markup'], {'inline_keyboard': [[{
            'text': 'Скопировать шаблон', 'copy_text': {'text': TRIGGER_TEMPLATE}}]]})
        self.assertIn('вставь шаблон в поле сообщения', payload['text'])
        self.assertNotIn('switch_inline_query', json.dumps(payload))
        self.assertNotIn('t.me/', json.dumps(payload))

    def test_menu_registers_plain_trigger_keeps_existing_commands(self):
        telegram = Telegram('fake', '123')
        telegram.install_commands()
        with patch('trader.telegram.requests.post') as post:
            telegram._deliver_message(telegram._messages[0])
        commands = post.call_args.kwargs['json']['commands']
        self.assertTrue({'trigger', 'start', 'stop', 'status', 'sendlog'}.issubset({x['command'] for x in commands}))
        self.assertTrue(all('\n' not in x['command'] for x in commands))

    def test_foreign_chat_and_repeated_updates_never_replay_trigger(self):
        telegram = Telegram('fake', '123')
        telegram.offset = 10
        telegram._startup_discard = False
        telegram._stop = Mock()
        telegram._stop.wait.side_effect = [False, True]
        def update(i, chat, text):
            return {'update_id': i, 'message': {'chat': {'id': chat}, 'text': text}}
        first = update(12, 123, command('4005'))
        response = Mock()
        response.json.return_value = {'result': [update(9, 123, command('4006')),
            update(11, 999, command('4007')), first, first, update(13, 123, '/stop')]}
        with patch('trader.telegram.requests.get', return_value=response):
            telegram._poll_loop()
        self.assertEqual(telegram.commands(), [command('4005'), '/stop'])
        self.assertEqual(telegram.offset, 14)
