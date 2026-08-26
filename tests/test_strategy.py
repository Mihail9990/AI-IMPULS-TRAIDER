from decimal import Decimal as D
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile
import requests

from pydroid_installer import copy_project, create_config, safe_extract
from trader.app import Bot
from trader.capital import CapitalClient, CapitalError
from trader.config import Settings
from trader.engine import Strategy
from trader.events import (
    find_close_event,
    find_trigger_open_event,
    find_working_order_execution,
    normalize_events,
)
from trader.execution import ExecutionPolicy, is_crossed_level_rejection, trigger_level_passed
from trader.model import CycleState, stop_slippage, trigger_slippage
from trader.reconcile import RemoteSnapshot
from trader.reporting import cycle_result_text, pnl_text
from trader.telegram import Telegram


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.state = CycleState()
        self.strategy = Strategy(Settings(), self.state)
        self.strategy.begin(D("4010.30"), D("4010.00"))

    def test_first_targets_are_beyond_opposite_stops(self):
        self.assertEqual(self.state.recovery, D("0.60"))
        self.assertEqual(self.state.long.stop, D("4009.30"))
        self.assertEqual(self.state.short.stop, D("4011.00"))
        self.assertEqual(self.state.long.take_profit, D("4011.60"))
        self.assertEqual(self.state.short.take_profit, D("4008.70"))

    def test_trigger_open_is_found_by_working_order_in_global_activity(self):
        activity = [{
            "dateUTC": "2026-08-25T13:34:09.000",
            "dealId": "new-buy",
            "source": "USER",
            "type": "POSITION",
            "status": "ACCEPTED",
            "details": {
                "workingOrderId": "trigger-buy",
                "direction": "BUY",
                "level": 4622.63,
            },
        }]
        event = find_trigger_open_event(activity, "trigger-buy", "BUY")
        self.assertIsNotNone(event)
        self.assertEqual(event.deal_id, "new-buy")
        self.assertEqual(event.level, D("4622.63"))

    def test_executed_trigger_is_detected_before_position_event_is_published(self):
        activity = [{
            "dateUTC": "2026-08-26T12:30:09.364",
            "dealId": "trigger-sell",
            "source": "USER",
            "type": "WORKING_ORDER",
            "status": "EXECUTED",
            "details": {"direction": "SELL", "level": 4620.63},
        }]
        event = find_working_order_execution(activity, "trigger-sell")
        self.assertIsNotNone(event)
        self.assertEqual(event.level, D("4620.63"))

    def test_actual_initial_fills_define_immutable_trigger_anchors(self):
        self.strategy.confirm_initial_fills(D("4010.35"), D("4010.00"))
        self.assertEqual(self.state.entry_spread, D("0.35"))
        self.assertEqual(self.state.recovery, D("0.65"))
        self.assertEqual(self.state.long.original_trigger_level, D("4010.35"))
        self.assertEqual(self.state.long.take_profit, D("4011.65"))

    def test_favorable_sequential_fill_gap_is_absolute_spread(self):
        self.strategy.confirm_initial_fills(D("4658.48"), D("4658.72"))
        self.assertEqual(self.state.entry_spread, D("0.24"))
        self.assertEqual(self.state.recovery, D("0.54"))
        self.assertEqual(self.state.long.stop, D("4657.48"))
        self.assertEqual(self.state.short.stop, D("4659.72"))
        self.assertEqual(self.state.long.take_profit, D("4660.26"))
        self.assertEqual(self.state.short.take_profit, D("4656.94"))

    def test_absolute_stop_slippage_is_added_in_both_directions(self):
        self.strategy.stopped("SELL", D("4011.10"), "stop-1")
        self.assertEqual(self.state.recovery, D("0.70"))
        self.assertEqual(self.state.realized_losses, D("1.10"))
        self.assertEqual(self.state.long.take_profit, D("4011.70"))

        second = CycleState()
        strategy = Strategy(Settings(), second)
        strategy.begin(D("4010.30"), D("4010.00"))
        strategy.stopped("SELL", D("4010.95"))
        self.assertEqual(second.recovery, D("0.65"))
        self.assertEqual(second.realized_losses, D("0.95"))

    def test_completion_calculates_gross_losses_and_net_result(self):
        self.strategy.stopped("SELL", D("4011.10"), "stop-1")
        self.strategy.complete("BUY", D("4011.70"))
        self.assertEqual(self.state.gross_take_profit, D("1.40"))
        self.assertEqual(self.state.realized_losses, D("1.10"))
        self.assertEqual(self.state.net_cycle_result, D("0.30"))
        self.assertEqual(self.state.completed_cycles, 1)

    def test_completed_cycle_counter_survives_reset_and_state_round_trip(self):
        self.strategy.complete("BUY", D("4011.60"))
        self.state.reset()
        with tempfile.NamedTemporaryFile() as file:
            self.state.save(file.name)
            restored = CycleState.load(file.name)
        self.assertEqual(restored.completed_cycles, 1)

    def test_reopen_uses_current_entry_for_stop_and_anchor_for_slippage(self):
        self.strategy.stopped("SELL", D("4011.10"))
        self.strategy.reopened("SELL", D("4009.90"), "short-2")
        self.assertEqual(self.state.scenario, 2)
        self.assertEqual(self.state.recovery, D("1.80"))
        self.assertEqual(self.state.short.original_trigger_level, D("4010.00"))
        self.assertEqual(self.state.short.current_entry, D("4009.90"))
        self.assertEqual(self.state.short.stop, D("4010.90"))
        self.assertEqual(self.state.long.take_profit, D("4012.70"))
        self.assertEqual(self.state.short.take_profit, D("4007.50"))

    def test_scenario_nine_enters_automatic_closing_phase(self):
        self.state.scenario = 8
        self.state.short.open = False
        self.strategy.reopened("SELL", D("4010"), "short-9")
        self.assertFalse(self.state.manual)
        self.assertFalse(self.state.paused)
        self.assertEqual(self.state.phase, "SCENARIO_9_CLOSING")

    def test_scenario_nine_result_uses_actual_close_gap_and_prior_losses(self):
        self.state.realized_losses = D("20")
        self.strategy.complete_scenario_nine(D("4001.5"), D("4000.3"))
        self.assertEqual(self.state.scenario_nine_prior_losses, D("20"))
        self.assertEqual(self.state.scenario_nine_close_gap, D("1.2"))
        self.assertEqual(self.state.scenario_nine_total_loss, D("21.2"))
        self.assertEqual(self.state.scenario_nine_long_fill, D("4001.5"))
        self.assertEqual(self.state.scenario_nine_short_fill, D("4000.3"))
        self.assertEqual(self.state.net_cycle_result, D("-21.2"))
        self.assertFalse(self.state.manual)
        self.assertFalse(self.state.active)

    def test_zero_profit_override_is_not_replaced_by_config_default(self):
        self.state.reset()
        self.state.profit_override = D("0")
        self.state.profit_override_remaining = 1
        self.strategy.begin(D("4010.3"), D("4010"))
        self.assertEqual(self.state.cycle_target_profit, D("0"))
        self.assertEqual(self.state.recovery, D("0.3"))

    def test_profit_override_applies_to_exactly_200_completed_cycles(self):
        self.state.reset()
        self.state.profit_override = D("0.4")
        self.state.profit_override_remaining = 200
        for index in range(200):
            self.strategy.begin(D("4010.3"), D("4010"))
            self.assertEqual(self.state.cycle_target_profit, D("0.4"))
            self.strategy.complete("BUY", self.state.long.take_profit)
            self.state.reset()
            self.assertEqual(self.state.profit_override_remaining, 199 - index)
        self.assertIsNone(self.state.profit_override)
        self.strategy.begin(D("4010.3"), D("4010"))
        self.assertEqual(self.state.cycle_target_profit, D("0.3"))

    def test_event_is_idempotent(self):
        self.strategy.stopped("SELL", D("4011.10"), "event")
        recovery = self.state.recovery
        self.strategy.stopped("SELL", D("4011.10"), "event")
        self.assertEqual(self.state.recovery, recovery)

    def test_state_round_trip(self):
        self.state.telegram_offset = 123
        with tempfile.NamedTemporaryFile() as file:
            self.state.save(file.name)
            restored = CycleState.load(file.name)
        self.assertEqual(restored.long.original_trigger_level, D("4010.30"))
        self.assertEqual(restored.long.current_entry, D("4010.30"))
        self.assertEqual(restored.recovery, D("0.60"))
        self.assertEqual(restored.telegram_offset, 123)

    def test_deal_ids_survive_reopen_reset_and_state_round_trip(self):
        self.state.long.deal_id = "long-1"
        self.state.long.deal_reference = "ref-1"
        self.state.remember_deal(self.state.long)
        self.strategy.stopped("SELL", D("4011.10"), "stop-short")
        self.strategy.reopened("SELL", D("4009.90"), "short-2", "reopen-short")
        self.state.remember_close("long-1", "TP", D("4012.70"))
        self.state.reset()
        with tempfile.NamedTemporaryFile() as file:
            self.state.save(file.name)
            restored = CycleState.load(file.name)
        by_id = {item["deal_id"]: item for item in restored.deal_history}
        self.assertIn("long-1", by_id)
        self.assertIn("short-2", by_id)
        self.assertEqual(by_id["long-1"]["close_source"], "TP")
        self.assertEqual(by_id["long-1"]["close_level"], "4012.70")

    def test_slippage_helpers_use_absolute_deviation(self):
        self.assertEqual(stop_slippage("BUY", D("10"), D("9.9")), D("0.1"))
        self.assertEqual(stop_slippage("BUY", D("10"), D("10.1")), D("0.1"))
        self.assertEqual(trigger_slippage("SELL", D("10"), D("9.9")), D("0.1"))
        self.assertEqual(trigger_slippage("SELL", D("10"), D("10.1")), D("0.1"))


class EntryRetryTest(unittest.TestCase):
    def make_bot(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings(dry_run=False, api_key="key", identifier="id", password="password")
        bot.capital = Mock()
        bot.capital.positions.return_value = []
        bot.capital.working_orders.return_value = []
        bot.telegram = Mock()
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.execution_policy = ExecutionPolicy()
        bot._flat_checks = 0
        bot.strategy.begin(D("4010.30"), D("4010.00"))
        return bot

    def test_initial_leg_gets_three_retries(self):
        bot = self.make_bot()
        bot.capital.open_position.return_value = "reference"
        bot.capital.wait_position.return_value = {"dealId": "deal", "level": 4010.35}
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "REJECTED", "reason": "busy"},
            {"dealStatus": "REJECTED", "reason": "busy"},
            {"dealStatus": "REJECTED", "reason": "busy"},
            {"dealStatus": "ACCEPTED", "dealId": "deal", "level": 4010.35},
        ]
        error = bot._open_initial_leg(bot.state.long)
        self.assertIsNone(error)
        self.assertEqual(bot.capital.open_position.call_count, 4)
        self.assertEqual(bot.state.long.current_entry, D("4010.35"))
        kwargs = bot.capital.open_position.call_args.kwargs
        self.assertEqual(kwargs["stop_distance"], D("1"))
        self.assertNotIn("profit_distance", kwargs)

    def test_diagnostics_are_cleared_once_after_each_tenth_completed_cycle(self):
        bot = self.make_bot()
        bot.state.completed_cycles = 9
        bot.state.diagnostic_cleanup_cycle = 0
        with tempfile.NamedTemporaryFile() as state_file:
            bot.cfg = Settings(
                dry_run=False, api_key="key", identifier="id", password="password",
                state_file=state_file.name, diagnostic_log_file="diagnostic.log",
            )
            bot.strategy = Strategy(bot.cfg, bot.state)
            with patch("trader.app.clear_diagnostics") as clear:
                bot._complete_cycle("BUY", D("4011.60"))
                bot._complete_cycle("BUY", D("4011.60"))
        clear.assert_called_once_with("diagnostic.log")
        self.assertEqual(bot.state.completed_cycles, 11)
        self.assertEqual(bot.state.diagnostic_cleanup_cycle, 10)

    def test_scenario_nine_removes_protection_and_closes_both_sides(self):
        bot = self.make_bot()
        bot.state.scenario = 8
        bot.state.realized_losses = D("20")
        bot.state.short.open = False
        bot.strategy.reopened("SELL", D("4010"), "short-9")
        bot.state.long.deal_id = "long-9"
        bot.capital.positions.return_value = [
            {"position": {"dealId": "long-9", "direction": "BUY", "level": 4010.3},
             "market": {"epic": "GOLD"}},
            {"position": {"dealId": "short-9", "direction": "SELL", "level": 4010},
             "market": {"epic": "GOLD"}},
        ]
        bot.capital.update_position.side_effect = lambda deal_id, _sl, _tp: f"update-{deal_id}"
        bot.capital.close_position.side_effect = lambda deal_id: f"close-{deal_id}"

        def confirmation(reference):
            levels = {"close-long-9": 4001.5, "close-short-9": 4000.3}
            payload = {"dealStatus": "ACCEPTED"}
            if reference in levels:
                payload["level"] = levels[reference]
            return payload

        bot.capital.wait_confirmation.side_effect = confirmation
        with tempfile.NamedTemporaryFile() as state_file:
            bot.cfg = Settings(
                dry_run=False, api_key="key", identifier="id", password="password",
                state_file=state_file.name,
            )
            bot.strategy = Strategy(bot.cfg, bot.state)
            bot._enter_manual_nine()

        self.assertFalse(bot.state.active)
        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.scenario_nine_total_loss, D("21.2"))
        self.assertEqual(bot.capital.close_position.call_count, 2)
        bot.capital.update_position.assert_any_call("long-9", None, None)
        bot.capital.update_position.assert_any_call("short-9", None, None)

    def test_missed_diagnostic_cleanup_boundary_is_recovered(self):
        bot = self.make_bot()
        bot.state.completed_cycles = 12
        bot.state.diagnostic_cleanup_cycle = 0
        with tempfile.NamedTemporaryFile() as state_file:
            bot.cfg = Settings(
                dry_run=False, api_key="key", identifier="id", password="password",
                state_file=state_file.name, diagnostic_log_file="diagnostic.log",
            )
            with patch("trader.app.clear_diagnostics") as clear:
                bot._clear_diagnostics_if_due()
                bot._clear_diagnostics_if_due()
        clear.assert_called_once_with("diagnostic.log")
        self.assertEqual(bot.state.diagnostic_cleanup_cycle, 12)

    def test_exact_stop_is_confirmed_before_take_profit(self):
        bot = self.make_bot()
        leg = bot.state.long
        leg.deal_id = "long-1"
        bot.capital.update_position.return_value = "stop-ref"
        bot.capital.wait_confirmation.return_value = {"dealStatus": "ACCEPTED"}
        bot.capital.position.return_value = {
            "position": {"dealId": "long-1", "stopLevel": 4009.30, "profitLevel": None}
        }

        bot._apply_stop_only(leg)

        bot.capital.update_position.assert_called_once_with("long-1", D("4009.30"), None)
        bot.capital.position.assert_called_once_with("long-1")
        self.assertIn("TP пока не установлен", bot.telegram.send.call_args.args[0])

    def test_take_profit_is_read_back_after_both_stops(self):
        bot = self.make_bot()
        leg = bot.state.long
        leg.deal_id = "long-1"
        leg.take_profit = D("4011.60")
        bot.capital.update_position.return_value = "tp-ref"
        bot.capital.wait_confirmation.return_value = {"dealStatus": "ACCEPTED"}
        bot.capital.position.return_value = {
            "position": {
                "dealId": "long-1",
                "stopLevel": 4009.30,
                "profitLevel": 4011.60,
            }
        }

        bot._apply_take_profit_only(leg)

        bot.capital.update_position.assert_called_once_with(
            "long-1", D("4009.30"), D("4011.60")
        )
        bot.capital.position.assert_called_once_with("long-1")
        self.assertIn("Take Profit подтверждён", bot.telegram.send.call_args.args[0])

    def test_survivor_is_closed_when_target_was_crossed_before_tp_update(self):
        bot = self.make_bot()
        leg = bot.state.short
        bot.strategy.stopped("BUY", D("4009.25"), "stop-long")
        leg.deal_id = "short-1"
        bot.capital.quote.return_value = (D("4008.00"), D("4008.20"))
        bot.capital.close_position.return_value = "close-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "level": 4008.18,
        }

        protected = bot._apply_protection(leg)

        self.assertFalse(protected)
        bot.capital.update_position.assert_not_called()
        bot.capital.close_position.assert_called_once_with("short-1")
        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.net_cycle_result, D("0.77"))
        self.assertIn("Целевая цена достигнута", bot.telegram.send.call_args.args[0])

    def test_take_profit_maxvalue_race_falls_back_to_market_close(self):
        bot = self.make_bot()
        leg = bot.state.short
        bot.strategy.stopped("BUY", D("4009.25"), "stop-long")
        leg.deal_id = "short-1"
        bot.capital.quote.side_effect = [
            (D("4009.00"), D("4009.20")),
            (D("4008.00"), D("4008.20")),
        ]
        bot.capital.update_position.side_effect = CapitalError(
            'Capital API 400: {"errorCode":"error.invalid.takeprofit.maxvalue: 4008.20"}'
        )
        bot.capital.close_position.return_value = "close-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "level": 4008.18,
        }

        self.assertFalse(bot._apply_protection(leg))
        bot.capital.close_position.assert_called_once_with("short-1")
        self.assertFalse(bot.state.active)

    def test_take_profit_during_initial_protection_completes_cycle(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-1"
        bot.state.short.deal_id = "short-1"
        bot.capital.positions.side_effect = [
            [{"position": {"dealId": "short-1"}, "market": {"epic": bot.cfg.epic}}],
            *([[]] * 10),
        ]
        bot._closing_fill = Mock(
            side_effect=lambda leg, source="SL": {
                ("long-1", "TP"): D("4011.60"),
                ("short-1", "SL"): D("4011.05"),
            }.get((leg.deal_id, source))
        )

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.phase, "FILTER")
        self.assertEqual(bot.state.realized_losses, D("1.05"))
        self.assertEqual(bot.state.gross_take_profit, D("1.30"))
        self.assertEqual(bot.state.net_cycle_result, D("0.25"))
        self.assertIn("Итог завершённого цикла", bot.telegram.send.call_args.args[0])

    def test_accepted_unresolved_position_is_not_opened_twice(self):
        bot = self.make_bot()
        bot.capital.open_position.return_value = "accepted-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "confirmation-id",
        }
        bot.capital.wait_position.side_effect = CapitalError("positions synchronization timeout")
        bot.capital.activity.return_value = []

        with patch("trader.app.time.sleep"):
            error = bot._open_initial_leg(bot.state.long)

        self.assertIn("заявка принята", error)
        self.assertEqual(bot.state.long.deal_reference, "accepted-ref")
        bot.capital.open_position.assert_called_once()

    def test_accepted_position_closed_before_positions_sync_is_classified_from_activity(self):
        bot = self.make_bot()
        bot.capital.open_position.return_value = "accepted-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED",
            "dealId": "fast-close",
            "level": 4621.38,
        }
        bot.capital.wait_position.side_effect = CapitalError("positions synchronization timeout")
        bot.capital.activity.return_value = [{
            "dateUTC": "2026-08-25T02:51:38.000",
            "dealId": "fast-close",
            "source": "SL",
            "type": "POSITION",
            "status": "ACCEPTED",
            "details": {"level": 4620.31, "direction": "SELL"},
        }]

        error = bot._open_initial_leg(bot.state.long)

        self.assertIn("закрылась по SL", error)
        self.assertEqual(bot.state.long.deal_id, "fast-close")
        self.assertEqual(bot.state.long.current_entry, D("4621.38"))
        self.assertEqual(bot.state.long.original_trigger_level, D("4621.38"))
        self.assertEqual(bot.state.long.stop, D("4620.38"))
        self.assertFalse(bot.state.long.open)
        self.assertEqual(
            bot._initial_entry_close,
            (bot.state.long, "SL", D("4620.31")),
        )
        bot.capital.open_position.assert_called_once()

    def test_accepted_close_waits_for_delayed_deal_history(self):
        bot = self.make_bot()
        bot.capital.open_position.return_value = "accepted-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED",
            "dealId": "delayed-close",
            "level": 4621.38,
        }
        bot.capital.wait_position.side_effect = CapitalError("positions synchronization timeout")
        bot.capital.activity.side_effect = [[], [], [{
            "dateUTC": "2026-08-25T02:51:50.000",
            "dealId": "delayed-close",
            "source": "TP",
            "type": "POSITION",
            "status": "ACCEPTED",
            "details": {"level": 4622.70, "direction": "SELL"},
        }]]

        with patch("trader.app.time.sleep"):
            error = bot._open_initial_leg(bot.state.long)

        self.assertIn("закрылась по TP", error)
        self.assertEqual(bot.capital.activity.call_count, 3)
        self.assertEqual(
            bot._initial_entry_close,
            (bot.state.long, "TP", D("4622.70")),
        )

    def test_first_initial_leg_is_checked_before_second_entry(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-id"
        bot.capital.position.side_effect = CapitalError(
            'Capital API 404: {"errorCode":"error.not-found.dealId"}'
        )
        with patch("trader.app.time.sleep"):
            present = bot._position_still_open(bot.state.long)
        self.assertFalse(present)
        self.assertEqual(bot.capital.position.call_count, 3)

    def test_filter_waits_for_current_candle_then_enters_at_market(self):
        bot = self.make_bot()
        bot.state.reset()
        bot.state.armed = True
        bot.capital.candle_ranges.return_value = (D("2.5"), D("2.9"))
        bot._start_cycle = Mock()
        bot._tick_filter()
        self.assertTrue(bot.state.waiting_current_candle)
        bot.capital.candle_ranges.return_value = (D("2.5"), D("3.4"))
        bot._tick_filter()
        bot._start_cycle.assert_called_once_with("текущая свеча: 3.4")

    def test_pause_before_entry_disarms_filter_until_start(self):
        bot = self.make_bot()
        bot.state.reset()
        bot.state.armed = True
        bot.reconciled = True
        bot.command("/pause")
        self.assertTrue(bot.state.paused)
        self.assertFalse(bot.state.armed)
        self.assertEqual(bot.state.phase, "PAUSED")

        bot.command("/start")
        self.assertFalse(bot.state.paused)
        self.assertTrue(bot.state.armed)
        self.assertEqual(bot.state.phase, "FILTER")

    def test_early_start_command_reconciles_before_arming(self):
        bot = self.make_bot()
        bot.state.reset()
        bot.reconciled = False
        bot.capital.positions.return_value = []
        bot.capital.working_orders.return_value = []

        bot.command("/start")

        self.assertTrue(bot.reconciled)
        self.assertTrue(bot.state.armed)
        self.assertEqual(bot.state.phase, "FILTER")

    def test_start_cannot_override_manual_reconciliation_result(self):
        bot = self.make_bot()
        bot.state.reset()
        bot.state.manual = True
        bot.reconciled = True
        with self.assertRaisesRegex(RuntimeError, "ручном режиме"):
            bot.command("/start")

    def test_expected_command_error_is_reported_without_escaping_loop(self):
        bot = self.make_bot()
        bot.state.reset()
        bot.reconciled = True
        bot.cfg = Settings(dry_run=True)

        bot._process_commands(["/start"])

        message = bot.telegram.send.call_args.args[0]
        self.assertIn("BOT_DRY_RUN=true", message)

    def test_stop_is_pause_alias(self):
        bot = self.make_bot()
        bot.state.reset()
        bot.reconciled = True
        bot.command("/stop")
        self.assertTrue(bot.state.paused)
        self.assertEqual(bot.state.phase, "PAUSED")

    def test_pause_during_cycle_keeps_cycle_active(self):
        bot = self.make_bot()
        bot.reconciled = True
        bot.command("/pause")
        self.assertTrue(bot.state.active)
        self.assertTrue(bot.state.paused)
        self.assertEqual(bot.state.phase, "BOTH_OPEN")

    def test_paused_cycle_finishes_but_does_not_arm_next_cycle(self):
        bot = self.make_bot()
        bot.state.paused = True
        bot.state.short.open = False
        bot.state.short.trigger_id = "order"
        bot.state.phase = "LONG_ONLY"
        bot.capital.positions.return_value = []
        bot.capital.working_orders.return_value = []
        bot.capital.activity.return_value = [{
            "dealId": bot.state.long.deal_id, "source": "TP", "level": 4011.60,
        }]
        bot._tick_cycle()
        self.assertFalse(bot.state.active)
        self.assertFalse(bot.state.armed)
        self.assertEqual(bot.state.phase, "PAUSED")
        bot.capital.delete_working_order.assert_called_once_with("order")

    def test_completed_cycle_automatically_returns_to_filter_without_pause(self):
        bot = self.make_bot()
        bot.state.short.open = False
        bot.state.phase = "LONG_ONLY"
        bot.capital.positions.return_value = []
        bot.capital.activity.return_value = [{
            "dealId": bot.state.long.deal_id, "source": "TP", "level": 4011.60,
        }]
        bot._tick_cycle()
        self.assertFalse(bot.state.active)
        self.assertTrue(bot.state.armed)
        self.assertEqual(bot.state.phase, "FILTER")

    def test_transient_empty_positions_snapshot_does_not_enter_manual_mode(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-1"
        bot.state.short.deal_id = "short-1"
        both = [
            {"position": {"dealId": "long-1", "direction": "BUY"},
             "market": {"epic": "GOLD"}},
            {"position": {"dealId": "short-1", "direction": "SELL"},
             "market": {"epic": "GOLD"}},
        ]
        bot.capital.positions.side_effect = [[], both]
        bot._manual = Mock()
        with patch("trader.app.time.sleep"):
            bot._tick_cycle()
        bot._manual.assert_not_called()
        self.assertEqual(bot.state.phase, "BOTH_OPEN")

    def test_early_initial_stop_is_replayed_instead_of_manual_404(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-1"
        bot.state.short.deal_id = "short-1"
        bot.strategy.confirm_initial_fills(D("4645.81"), D("4644.88"))
        short_position = [{
            "position": {"dealId": "short-1", "direction": "SELL", "level": 4644.88},
            "market": {"epic": "GOLD"},
        }]
        bot.capital.positions.return_value = short_position
        bot.capital.activity.return_value = [{
            "dealId": "long-1", "source": "SL", "status": "ACCEPTED", "level": 4644.76,
        }]
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "trigger-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED", "dealId": "trigger-1"},
        ]

        handled = bot._continue_after_early_initial_close()

        self.assertTrue(handled)
        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.phase, "SHORT_ONLY")
        self.assertFalse(bot.state.long.open)
        self.assertEqual(bot.state.long.trigger_id, "trigger-1")
        self.assertEqual(bot.state.recovery, D("1.28"))
        bot.capital.update_position.assert_called_once()
        bot.capital.working_stop.assert_called_once()

    def test_second_initial_leg_stopped_before_position_sync_continues_scenario(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-1"
        bot.state.long.current_entry = D("4633.17")
        bot.state.long.original_trigger_level = D("4633.17")
        bot.state.short.deal_id = "short-fast-close"
        bot.state.short.current_entry = D("4633.06")
        bot.state.short.original_trigger_level = D("4633.06")
        bot.state.short.open = False
        bot.capital.positions.return_value = [{
            "position": {
                "dealId": "long-1", "direction": "BUY", "level": 4633.17,
            },
            "market": {"epic": "GOLD"},
        }]
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "trigger-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED", "dealId": "trigger-1"},
        ]

        with tempfile.NamedTemporaryFile() as state_file:
            bot.cfg = Settings(
                dry_run=False, api_key="key", identifier="id", password="password",
                state_file=state_file.name,
            )
            bot.strategy = Strategy(bot.cfg, bot.state)
            handled = bot._continue_after_second_initial_close(
                bot.state.short, "SL", D("4634.08")
            )

        self.assertTrue(handled)
        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.phase, "LONG_ONLY")
        self.assertFalse(bot.state.short.open)
        self.assertEqual(bot.state.entry_spread, D("0.11"))
        self.assertEqual(bot.state.recovery, D("0.43"))
        self.assertEqual(bot.state.realized_losses, D("1.02"))
        self.assertEqual(bot.state.long.take_profit, D("4634.49"))
        self.assertEqual(bot.state.short.trigger_id, "trigger-1")
        bot.capital.update_position.assert_called_once()
        bot.capital.working_stop.assert_called_once()
        self.assertIn("продолжает сценарий 1", bot.telegram.send.call_args.args[0])

    def test_second_initial_stop_tolerates_transient_missing_survivor(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-1"
        bot.state.long.current_entry = D("4633.17")
        bot.state.long.original_trigger_level = D("4633.17")
        bot.state.short.deal_id = "short-fast-close"
        bot.state.short.current_entry = D("4633.06")
        bot.state.short.original_trigger_level = D("4633.06")
        bot.state.short.open = False
        survivor = [{
            "position": {
                "dealId": "long-1", "direction": "BUY", "level": 4633.17,
            },
            "market": {"epic": "GOLD"},
        }]
        # The first snapshot is temporarily empty, then Capital publishes the surviving BUY.
        bot.capital.positions.side_effect = [[], survivor]
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "trigger-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED", "dealId": "trigger-1"},
        ]

        with tempfile.NamedTemporaryFile() as state_file, patch("trader.app.time.sleep"):
            bot.cfg = Settings(
                dry_run=False, api_key="key", identifier="id", password="password",
                state_file=state_file.name,
            )
            bot.strategy = Strategy(bot.cfg, bot.state)
            handled = bot._continue_after_second_initial_close(
                bot.state.short, "SL", D("4634.08")
            )

        self.assertTrue(handled)
        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.phase, "LONG_ONLY")
        self.assertEqual(bot.state.recovery, D("0.43"))
        self.assertEqual(bot.state.short.trigger_id, "trigger-1")
        self.assertEqual(bot.capital.positions.call_count, 2)

    def test_trigger_fill_followed_by_survivor_stop_is_replayed(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-old"
        bot.state.short.deal_id = "short-old"
        stopped_short = bot.strategy.stopped("SELL", D("4637.05"), "stop-short-old")
        stopped_short.trigger_id = "buy-trigger"
        stopped_short.trigger_reference = "trigger-ref"
        bot.capital.positions.return_value = [{
            "position": {
                "dealId": "short-new",
                "dealReference": "short-new-ref",
                "workingOrderId": "buy-trigger",
                "direction": "SELL",
                "level": 4635.97,
            },
            "market": {"epic": "GOLD"},
        }]
        bot.capital.activity.return_value = [{
            "dealId": "long-old", "source": "SL", "status": "ACCEPTED", "level": 4635.42,
        }]
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "next-trigger-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED", "dealId": "next-trigger"},
        ]

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.scenario, 2)
        self.assertEqual(bot.state.phase, "SHORT_ONLY")
        self.assertTrue(bot.state.short.open)
        self.assertEqual(bot.state.short.deal_id, "short-new")
        self.assertFalse(bot.state.long.open)
        self.assertEqual(bot.state.long.trigger_id, "next-trigger")
        bot.capital.update_position.assert_called_once()
        bot.capital.working_stop.assert_called_once()

    def test_trigger_fill_appearing_during_missing_position_retry_is_replayed(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-old"
        bot.state.short.deal_id = "short-old"
        stopped_short = bot.strategy.stopped("SELL", D("4637.05"), "stop-short-old")
        stopped_short.trigger_id = "sell-trigger"
        stopped_short.trigger_reference = "trigger-ref"
        trigger_position = [{
            "position": {
                "dealId": "short-new",
                "dealReference": "short-new-ref",
                "workingOrderId": "sell-trigger",
                "direction": "SELL",
                "level": 4635.97,
            },
            "market": {"epic": "GOLD"},
        }]
        # The first snapshot still contains neither side.  Capital.com exposes the position
        # created by the trigger only while _retry_missing_positions is already running.
        bot.capital.positions.side_effect = [[], *([trigger_position] * 10)]
        bot.capital.activity.return_value = [{
            "dealId": "long-old", "source": "SL", "status": "ACCEPTED", "level": 4635.42,
        }]
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "next-trigger-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED", "dealId": "next-trigger"},
        ]

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.scenario, 2)
        self.assertEqual(bot.state.phase, "SHORT_ONLY")
        self.assertEqual(bot.state.short.deal_id, "short-new")
        self.assertEqual(bot.state.long.trigger_id, "next-trigger")

    def test_passed_rejected_trigger_reopens_with_market(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"))
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "stop-ref"
        bot.capital.quote.return_value = (D("4009.70"), D("4009.90"))
        bot.capital.open_position.return_value = "market-ref"
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "REJECTED", "reason": "level already crossed"},
            {"dealStatus": "ACCEPTED", "dealId": "short-2", "level": 4009.68},
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED"},
        ]

        bot._create_trigger(stopped)

        self.assertEqual(bot.state.scenario, 2)
        self.assertTrue(bot.state.short.open)
        self.assertEqual(bot.state.short.current_entry, D("4009.68"))
        self.assertEqual(bot.state.short.original_trigger_level, D("4010.00"))
        self.assertEqual(bot.state.recovery, D("2.02"))
        bot.capital.open_position.assert_called_once()

    def test_startup_replays_unambiguous_stop_and_creates_trigger(self):
        bot = self.make_bot()
        bot.reconciled = False
        long_id = bot.state.long.deal_id = "long-1"
        short_id = bot.state.short.deal_id = "short-1"
        bot.capital.positions.return_value = [{
            "position": {"dealId": long_id, "direction": "BUY", "level": 4010.30},
            "market": {"epic": "GOLD"},
        }]
        bot.capital.working_orders.return_value = []
        bot.capital.activity.return_value = [{
            "dealId": short_id, "level": 4011.10, "source": "SL", "status": "ACCEPTED",
        }]
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.working_stop.return_value = "trigger-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED", "dealId": "trigger-1"},
        ]

        bot.reconcile_startup()

        self.assertTrue(bot.reconciled)
        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.phase, "LONG_ONLY")
        self.assertEqual(bot.state.recovery, D("0.70"))
        self.assertEqual(bot.state.short.trigger_id, "trigger-1")


class CapitalClientTest(unittest.TestCase):
    def test_delete_missing_working_order_is_idempotent_but_other_errors_raise(self):
        client = CapitalClient.__new__(CapitalClient)
        client.request = Mock(side_effect=CapitalError(
            'Capital API 404: {"errorCode":"error.not-found.dealId"}'
        ))
        self.assertFalse(client.delete_working_order("already-executed"))

        client.request = Mock(side_effect=CapitalError("Capital API 500: unavailable"))
        with self.assertRaises(CapitalError):
            client.delete_working_order("unknown")

    def test_get_retries_transient_transport_failure_without_retrying_mutation(self):
        settings = Settings(api_key="key", identifier="id", password="password")
        client = CapitalClient(settings)
        client.last_login = 10**20
        response = Mock(status_code=200, ok=True, content=b"{}")
        response.json.return_value = {}
        client.http.request = Mock(side_effect=[requests.ReadTimeout("temporary"), response])
        with patch("trader.capital.time.sleep"):
            self.assertEqual(client.request("GET", "/positions"), {})
        self.assertEqual(client.http.request.call_count, 2)

        client.http.request = Mock(side_effect=requests.ReadTimeout("unknown mutation result"))
        with self.assertRaises(CapitalError):
            client.request("POST", "/positions", json={})
        self.assertEqual(client.http.request.call_count, 1)

    def test_initial_position_can_use_fill_relative_protection_distances(self):
        client = CapitalClient.__new__(CapitalClient)
        client.request = Mock(return_value={"dealReference": "ref"})

        reference = client.open_position(
            "GOLD", "BUY", D("0.1"),
            stop_distance=D("1"), profit_distance=D("1.3"),
        )

        self.assertEqual(reference, "ref")
        body = client.request.call_args.kwargs["json"]
        self.assertEqual(body["stopDistance"], 1.0)
        self.assertEqual(body["profitDistance"], 1.3)
        self.assertNotIn("stopLevel", body)
        self.assertNotIn("profitLevel", body)

    def test_three_minute_candles_are_aggregated_from_minute_bars(self):
        client = CapitalClient.__new__(CapitalClient)
        client.request = Mock(return_value={"prices": [
            self.candle("2026-08-21T10:00:00", "10", "9"),
            self.candle("2026-08-21T10:01:00", "12", "10"),
            self.candle("2026-08-21T10:02:00", "11", "8"),
            self.candle("2026-08-21T10:03:00", "14", "12"),
            self.candle("2026-08-21T10:04:00", "15", "13"),
        ]})
        closed, current = client.candle_ranges("GOLD", 3)
        self.assertEqual(closed, D("4"))
        self.assertEqual(current, D("3"))
        self.assertEqual(client.request.call_args.kwargs["params"]["resolution"], "MINUTE")

    def test_wait_position_resolves_permanent_id_from_affected_deal(self):
        client = CapitalClient.__new__(CapitalClient)
        client.positions = Mock(return_value=[{
            "position": {"dealId": "permanent", "dealReference": "ref", "direction": "BUY", "level": 10}
        }])
        position = client.wait_position("permanent", "ref", "BUY")
        self.assertEqual(position["dealId"], "permanent")

    def test_wait_position_does_not_bind_stale_same_direction_position(self):
        client = CapitalClient.__new__(CapitalClient)
        client.positions = Mock(side_effect=[
            [{"position": {"dealId": "old", "direction": "BUY", "level": 9}}],
            [{"position": {"dealId": "old", "direction": "BUY", "level": 9}},
             {"position": {"dealId": "new", "direction": "BUY", "level": 10}}],
        ])
        with patch("trader.capital.time.sleep"):
            position = client.wait_position("confirmation-id", "new-ref", "BUY",
                                            excluded_ids={"old"})
        self.assertEqual(position["dealId"], "new")

    def test_wait_position_direction_fallback_is_scoped_to_epic(self):
        client = CapitalClient.__new__(CapitalClient)
        client.positions = Mock(return_value=[
            {"position": {"dealId": "oil", "direction": "BUY", "level": 80},
             "market": {"epic": "OIL"}},
            {"position": {"dealId": "gold", "direction": "BUY", "level": 4620},
             "market": {"epic": "GOLD"}},
        ])
        position = client.wait_position("pending", "ref", "BUY", epic="GOLD")
        self.assertEqual(position["dealId"], "gold")

    def test_update_position_retries_eventual_not_found(self):
        client = CapitalClient.__new__(CapitalClient)
        client.request = Mock(side_effect=[
            CapitalError('Capital API 404: {"errorCode":"error.not-found.dealId"}'),
            {"dealReference": "updated"},
        ])
        with patch("trader.capital.time.sleep"):
            reference = client.update_position("deal", D("9"), D("11"))
        self.assertEqual(reference, "updated")
        self.assertEqual(client.request.call_count, 2)

    @staticmethod
    def candle(timestamp, high, low):
        return {"snapshotTimeUTC": timestamp, "highPrice": {"bid": high}, "lowPrice": {"bid": low}}


class BrokerInfrastructureTest(unittest.TestCase):
    def test_transport_error_on_protection_is_resolved_by_readback(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings()
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4010.30"), D("4010.00"))
        leg = bot.state.long
        leg.deal_id = "long"
        bot.capital = Mock()
        bot.capital.update_position.side_effect = CapitalError(
            "Capital transport error PUT /positions/long: timeout"
        )
        bot._cycle_positions = Mock(return_value={
            "long": {"dealId": "long", "stopLevel": leg.stop,
                     "profitLevel": leg.take_profit},
        })
        bot.telegram = Mock()

        self.assertTrue(bot._apply_protection(leg))
        self.assertIn("повторным чтением", bot.telegram.send.call_args.args[0])

    def test_rejected_close_activity_is_not_treated_as_fill(self):
        activity = [{
            "dateUTC": "2026-08-25T14:00:00", "dealId": "deal",
            "source": "SL", "type": "POSITION", "status": "REJECTED",
            "details": {"level": 4009.2},
        }]
        self.assertIsNone(find_close_event(activity, "deal", "SL"))

    def test_missing_deal_during_protection_is_deferred_to_event_replay(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings()
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4010.30"), D("4010.00"))
        bot.state.long.deal_id = "closed-between-snapshot-and-put"
        bot.capital = Mock()
        bot.capital.update_position.side_effect = CapitalError(
            'Capital API 404: {"errorCode":"error.not-found.dealId"}'
        )
        bot.telegram = Mock()

        self.assertFalse(bot._apply_protection(bot.state.long))
        bot.telegram.send.assert_not_called()

    def test_closed_trigger_position_is_recovered_from_global_activity(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings()
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4622.63"), D("4622.02"))
        stopped = bot.strategy.stopped("BUY", D("4621.61"), "initial-buy-stop")
        stopped.trigger_id = "trigger-buy"
        survivor = bot.state.short
        activity = [
            {"dateUTC": "2026-08-25T13:34:09", "dealId": "new-buy",
             "source": "USER", "type": "POSITION", "status": "ACCEPTED",
             "details": {"workingOrderId": "trigger-buy", "direction": "BUY",
                         "level": 4622.63}},
            {"dateUTC": "2026-08-25T13:34:10", "dealId": survivor.deal_id,
             "source": "SL", "type": "POSITION", "status": "ACCEPTED",
             "details": {"direction": "BUY", "level": 4623.07}},
            {"dateUTC": "2026-08-25T13:34:12", "dealId": "new-buy",
             "source": "TP", "type": "POSITION", "status": "ACCEPTED",
             "details": {"direction": "SELL", "level": 4624.95}},
        ]
        bot.capital = Mock()
        bot.capital.activity.return_value = activity
        bot.telegram = Mock()
        bot._complete_cycle = Mock()

        self.assertTrue(bot._recover_trigger_round_trip_from_activity(survivor, stopped))
        self.assertEqual(bot.state.scenario, 2)
        bot._complete_cycle.assert_called_once_with("BUY", D("4624.95"))
        bot.capital.activity.assert_called_once_with()

    def test_two_fast_stops_create_trigger_for_the_first_stopped_survivor(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings()
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4622.63"), D("4622.02"))
        original_stopped = bot.strategy.stopped("BUY", D("4621.61"), "initial-buy-stop")
        original_stopped.trigger_id = "trigger-buy"
        old_survivor = bot.state.short
        old_survivor.deal_id = "old-sell"
        bot.capital = Mock()
        bot.capital.activity.return_value = [
            {"dateUTC": "2026-08-25T13:34:09", "dealId": "reopened-buy",
             "source": "USER", "type": "POSITION", "status": "ACCEPTED",
             "details": {"workingOrderId": "trigger-buy", "direction": "BUY",
                         "level": 4622.67}},
            {"dateUTC": "2026-08-25T13:34:10", "dealId": "old-sell",
             "source": "SL", "type": "POSITION", "status": "ACCEPTED",
             "details": {"direction": "BUY", "level": 4623.07}},
            {"dateUTC": "2026-08-25T13:34:12", "dealId": "reopened-buy",
             "source": "SL", "type": "POSITION", "status": "ACCEPTED",
             "details": {"direction": "SELL", "level": 4621.62}},
        ]
        bot.telegram = Mock()
        bot._create_trigger = Mock()

        self.assertTrue(
            bot._recover_trigger_round_trip_from_activity(old_survivor, original_stopped)
        )
        self.assertEqual(bot.state.phase, "LONG_ONLY")
        self.assertTrue(bot.state.long.open)
        self.assertFalse(bot.state.short.open)
        bot._create_trigger.assert_called_once_with(old_survivor)

    def test_nested_activity_is_normalized_and_sorted(self):
        items = [{
            "dateUTC": "2026-08-22T10:01:00Z", "source": "SL", "status": "ACCEPTED",
            "details": {"dealId": "deal-1", "closeLevel": "4009.20", "direction": "BUY"},
        }, {
            "dateUTC": "2026-08-22T10:00:00Z", "source": "USER", "status": "ACCEPTED",
            "details": {"dealId": "deal-1", "level": "4010.30", "direction": "BUY"},
        }]
        events = normalize_events(items)
        self.assertEqual(events[0].source, "USER")
        close = find_close_event(items, "deal-1", "SL")
        self.assertEqual(close.level, D("4009.20"))
        self.assertTrue(close.is_stop)

    def test_execution_helpers_are_directional(self):
        self.assertTrue(trigger_level_passed("SELL", D("10"), D("9.9"), D("10.1")))
        self.assertFalse(trigger_level_passed("BUY", D("10.2"), D("9.9"), D("10.1")))
        self.assertTrue(is_crossed_level_rejection("error.invalid.level: already crossed"))
        self.assertFalse(is_crossed_level_rejection("insufficient funds"))

    def test_remote_snapshot_accepts_position_linked_to_trigger(self):
        snapshot = RemoteSnapshot({
            "new-deal": {"dealId": "new-deal", "workingOrderId": "trigger-1"},
            "foreign": {"dealId": "foreign"},
        }, {})
        self.assertEqual(
            snapshot.unknown_position_ids(set(), {"trigger-1"}), {"foreign"}
        )

    def test_pnl_report_uses_broker_values(self):
        text = pnl_text(
            CycleState(scenario=3, recovery=D("3.00")),
            [{"position": {"upl": "2.50", "currency": "USD"}}],
            [{"profitAndLoss": "-1.20", "currency": "USD"}],
        )
        self.assertIn("Закрытый P&L за период истории: -1.20 USD", text)
        self.assertIn("Суммарно: 1.30 USD", text)

    def test_pnl_report_accepts_capital_transaction_size_as_money(self):
        text = pnl_text(
            CycleState(), [],
            [{"transactionType": "TRADE", "size": "-0.09", "currency": "USD"},
             {"transactionType": "TRADE", "size": "0.14", "currency": "USD"}],
        )
        self.assertIn("Закрытый P&L за период истории: 0.05 USD", text)

    def test_completed_cycle_report_includes_losses_gross_and_net(self):
        state = CycleState(
            scenario=2, realized_losses=D("2.05"), gross_take_profit=D("2.35"),
            net_cycle_result=D("0.30"),
        )
        text = cycle_result_text(state, "SELL", D("4668.99"), D("0.1"))
        self.assertIn("Валовая прибыль TP: 2.35 пункта", text)
        self.assertIn("Общие убытки закрытых сторон: 2.05 пункта", text)
        self.assertIn("Итог цикла: 0.30 пункта", text)
        self.assertIn("итог 0.030", text)

    def test_manual_trigger_command_creates_working_stop(self):
        bot = EntryRetryTest().make_bot()
        bot.state.manual = True
        bot.capital.working_stop.return_value = "manual-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "manual-order",
        }
        bot.command("/settrigger long 4020.50")
        self.assertEqual(bot.state.long.trigger_id, "manual-order")
        bot.capital.working_stop.assert_called_once_with(
            "GOLD", "BUY", D("0.1"), D("4020.50")
        )

    def test_recover_rebinds_permanent_ids_and_reapplies_protection(self):
        bot = EntryRetryTest().make_bot()
        bot.state.manual = True
        bot.state.paused = True
        bot.capital.positions.return_value = [
            {"position": {"dealId": "permanent-buy", "direction": "BUY", "level": 4010.30},
             "market": {"epic": "GOLD"}},
            {"position": {"dealId": "permanent-sell", "direction": "SELL", "level": 4010.00},
             "market": {"epic": "GOLD"}},
        ]
        bot.capital.update_position.side_effect = ["update-buy", "update-sell"]
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED"}, {"dealStatus": "ACCEPTED"},
        ]

        bot.command("/recover")

        self.assertFalse(bot.state.manual)
        self.assertTrue(bot.state.paused)
        self.assertEqual(bot.state.long.deal_id, "permanent-buy")
        self.assertEqual(bot.state.short.deal_id, "permanent-sell")
        self.assertEqual(bot.capital.update_position.call_count, 2)

    def test_automode_clears_stale_manual_cycle_when_broker_is_empty(self):
        bot = EntryRetryTest().make_bot()
        bot.state.manual = True
        bot.state.paused = True
        bot.capital.positions.return_value = []
        bot.capital.working_orders.return_value = []

        bot.command("/automode")

        self.assertFalse(bot.state.manual)
        self.assertFalse(bot.state.active)
        self.assertTrue(bot.state.paused)
        self.assertEqual(bot.state.phase, "PAUSED")

    def test_automode_refuses_to_ignore_existing_position(self):
        bot = EntryRetryTest().make_bot()
        bot.state.manual = True
        bot.capital.positions.return_value = [{
            "position": {"dealId": "buy", "direction": "BUY"}, "market": {"epic": "GOLD"},
        }]
        bot.capital.working_orders.return_value = []
        with self.assertRaisesRegex(RuntimeError, "Нельзя выйти"):
            bot.command("/automode")

    def test_startup_clears_manual_state_when_broker_has_nothing(self):
        bot = EntryRetryTest().make_bot()
        bot.state.manual = True
        bot.reconciled = False
        bot.capital.positions.return_value = []
        bot.capital.working_orders.return_value = []

        bot.reconcile_startup()

        self.assertTrue(bot.reconciled)
        self.assertFalse(bot.state.manual)
        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.phase, "PAUSED")


class PydroidConfigTest(unittest.TestCase):
    def test_invalid_safety_boolean_is_rejected(self):
        with patch.dict(os.environ, {"CAPITAL_DEMO": "treu"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_nonpositive_entry_range_is_rejected(self):
        with patch.dict(os.environ, {"ENTRY_CANDLE_RANGE": "0"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_json_config_is_loaded_without_environment_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bot_config.json"
            config.write_text(json.dumps({
                "CAPITAL_DEMO": True,
                "BOT_DRY_RUN": False,
                "CAPITAL_API_KEY": "demo-key",
                "CAPITAL_IDENTIFIER": "demo-user",
                "CAPITAL_PASSWORD": "demo-password",
                "POSITION_SIZE": "0.2",
                "ENTRY_CANDLE_MINUTES": 3,
            }), encoding="utf-8")
            with patch.dict(os.environ, {"BOT_CONFIG_FILE": str(config)}, clear=True):
                settings = Settings.from_env()
        self.assertTrue(settings.demo)
        self.assertFalse(settings.dry_run)
        self.assertEqual(settings.size, D("0.2"))
        self.assertEqual(settings.candle_minutes, 3)

    def test_environment_overrides_json_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bot_config.json"
            config.write_text(json.dumps({"POSITION_SIZE": "0.2"}), encoding="utf-8")
            with patch.dict(os.environ, {
                "BOT_CONFIG_FILE": str(config), "POSITION_SIZE": "0.3",
            }, clear=True):
                settings = Settings.from_env()
        self.assertEqual(settings.size, D("0.3"))

    def test_standalone_installer_extracts_and_preserves_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "project.zip"
            with ZipFile(archive, "w") as bundle:
                bundle.writestr("AI-IMPULS-TRAIDER-work/main.py", "print('new')")
                bundle.writestr("AI-IMPULS-TRAIDER-work/bot_config.example.json", "{}")
                bundle.writestr("AI-IMPULS-TRAIDER-work/requirements.txt", "requests\n")
                bundle.writestr("AI-IMPULS-TRAIDER-work/trader/app.py", "NEW = True\n")
            source = safe_extract(archive, root / "unpacked")
            target = root / "installed"
            target.mkdir()
            (target / "bot_config.json").write_text('{"secret":"keep"}', encoding="utf-8")
            (target / "bot_state.json").write_text('{"scenario":4}', encoding="utf-8")

            copy_project(source, target)
            created = create_config(target)

            self.assertFalse(created)
            self.assertEqual((target / "bot_config.json").read_text(), '{"secret":"keep"}')
            self.assertEqual((target / "bot_state.json").read_text(), '{"scenario":4}')
            self.assertEqual((target / "trader/app.py").read_text(), "NEW = True\n")

    def test_telegram_discards_commands_pending_before_process_start(self):
        telegram = Telegram("token", "123")
        response = Mock()
        response.json.return_value = {"result": [{"update_id": 77}]}
        with patch("trader.telegram.requests.get", return_value=response) as get:
            telegram.discard_pending()
        self.assertEqual(telegram.offset, 78)
        self.assertEqual(get.call_args.kwargs["params"]["offset"], -1)

    def test_telegram_installs_command_menu_and_keyboard(self):
        telegram = Telegram("token", "123")
        response = Mock()
        with patch("trader.telegram.requests.post", return_value=response) as post:
            telegram.install_commands()
            telegram.send("ready", show_menu=True)
            telegram.flush_pending()
        self.assertTrue(any(call.args[0].endswith("/setMyCommands") for call in post.call_args_list))
        send_payload = post.call_args_list[-1].kwargs["json"]
        self.assertIn("reply_markup", send_payload)
        self.assertEqual(send_payload["reply_markup"]["keyboard"][0][0]["text"], "/status")
        self.assertFalse(send_payload["reply_markup"]["is_persistent"])

    def test_telegram_keyboard_can_be_removed_and_restored(self):
        telegram = Telegram("token", "123")
        response = Mock()
        with patch("trader.telegram.requests.post", return_value=response) as post:
            telegram.send("hide", hide_menu=True)
            telegram.send("show", show_menu=True)
            telegram.flush_pending()
        hide_payload = post.call_args_list[-2].kwargs["json"]
        show_payload = post.call_args_list[-1].kwargs["json"]
        self.assertEqual(hide_payload["reply_markup"], {"remove_keyboard": True})
        self.assertIn("keyboard", show_payload["reply_markup"])

    def test_telegram_timeout_does_not_interrupt_trading_loop(self):
        telegram = Telegram("token", "123")
        with patch("trader.telegram.requests.get",
                   side_effect=requests.ReadTimeout("temporary network timeout")):
            self.assertEqual(telegram.commands(), [])
        telegram.send_unavailable_until = 0
        with patch("trader.telegram.requests.post",
                   side_effect=requests.ReadTimeout("temporary network timeout")):
            self.assertTrue(telegram.send("important report"))
            self.assertEqual(telegram.flush_pending(), 0)
        self.assertEqual(telegram.pending_reports, 1)

    def test_poll_timeout_does_not_suppress_reports(self):
        telegram = Telegram("token", "123")
        with patch("trader.telegram.requests.get",
                   side_effect=requests.ReadTimeout("poll timeout")):
            self.assertEqual(telegram.commands(), [])
        response = Mock()
        with patch("trader.telegram.requests.post", return_value=response) as post:
            self.assertTrue(telegram.send("cycle report"))
            self.assertEqual(telegram.flush_pending(), 1)
        post.assert_called_once()
        self.assertEqual(telegram.pending_reports, 0)

    def test_failed_report_is_retried_in_order(self):
        telegram = Telegram("token", "123")
        telegram.send("first report")
        with patch("trader.telegram.requests.post",
                   side_effect=requests.ReadTimeout("temporary network timeout")):
            self.assertEqual(telegram.flush_pending(), 0)
        telegram.send("second report")
        self.assertEqual(telegram.pending_reports, 2)
        telegram.send_unavailable_until = 0
        response = Mock()
        with patch("trader.telegram.requests.post", return_value=response) as post:
            self.assertEqual(telegram.flush_pending(), 2)
        self.assertEqual(
            [call.kwargs["json"]["text"] for call in post.call_args_list],
            ["first report", "second report"],
        )


if __name__ == "__main__":
    unittest.main()
