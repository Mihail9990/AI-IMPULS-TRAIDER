from decimal import Decimal as D
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile
import requests

from pydroid_installer import copy_project, create_config, safe_extract
from trader.app import Bot
from trader.capital import CapitalClient, CapitalError
from trader.config import Settings
from trader.cycle_continuation import CycleContinuation
from trader.diagnostics import CycleFileHandler
from trader.engine import Strategy
from trader.events import (
    find_close_event,
    find_trigger_open_event,
    find_working_order_cancellation,
    find_working_order_execution,
    normalize_events,
)
from trader.execution import ExecutionPolicy, is_crossed_level_rejection, trigger_level_passed
from trader.model import CycleState, Leg, stop_slippage, target_for, trigger_slippage
from trader.notifications import (
    NotificationHistoryWorker, migrate_notification_jobs, split_report,
)
from trader.reconcile import RemoteSnapshot
from trader.reporting import cycle_result_text, leg_details, pnl_text, recovery_change_text, recovery_snapshot
from trader.streaming import PriceWatch, QuoteStream
from trader.telegram import Telegram


_RUNTIME_STATE_PATH = Path(__file__).resolve().parents[1] / "bot_state.json"
_RUNTIME_STATE_EXISTED = _RUNTIME_STATE_PATH.exists()
_RUNTIME_STATE_BYTES = _RUNTIME_STATE_PATH.read_bytes() if _RUNTIME_STATE_EXISTED else None


def tearDownModule():
    """Never leave test state in the project or overwrite a user's pre-existing state."""
    if _RUNTIME_STATE_EXISTED:
        _RUNTIME_STATE_PATH.write_bytes(_RUNTIME_STATE_BYTES)
    else:
        _RUNTIME_STATE_PATH.unlink(missing_ok=True)


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.state = CycleState()
        self.strategy = Strategy(Settings(), self.state)
        self.strategy.begin(D("4010.30"), D("4010.00"))
        self.strategy.confirm_initial_fills(D("4010.30"), D("4010.00"))

    def test_first_targets_use_each_leg_actual_entry(self):
        self.assertEqual(self.state.general_recovery, D("0.060"))
        self.assertEqual(self.state.long.stop, D("4009.30"))
        self.assertEqual(self.state.short.stop, D("4011.00"))
        self.assertEqual(self.state.long.take_profit, D("4011.90"))
        self.assertEqual(self.state.short.take_profit, D("4008.40"))

    def test_detailed_report_uses_persisted_recovery_components(self):
        before = recovery_snapshot(self.state)
        self.strategy.stopped("BUY", D("4009.20"), "report-sl")
        text = recovery_change_text(self.state, before, event="SL", direction="BUY")
        self.assertIn("GENERAL_RECOVERY", text)
        self.assertIn("pending D", text)
        self.assertNotIn("temporary:", text)

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

    def test_working_order_cancellation_requires_explicit_cancelled_event(self):
        quiet = [{
            "dealId": "another-order", "type": "WORKING_ORDER", "status": "CANCELLED",
        }]
        self.assertIsNone(find_working_order_cancellation(quiet, "trigger-sell"))
        cancelled = quiet + [{
            "dealId": "trigger-sell", "type": "WORKING_ORDER", "status": "CANCELLED",
        }]
        self.assertIsNotNone(find_working_order_cancellation(cancelled, "trigger-sell"))

    def test_actual_initial_fills_define_immutable_trigger_anchors(self):
        state = CycleState(); strategy = Strategy(Settings(), state)
        strategy.begin(D("4010.30"), D("4010.00"))
        strategy.confirm_initial_fills(D("4010.35"), D("4010.00"))
        self.assertEqual(state.general_recovery, D("0.065"))
        self.assertEqual(state.long.original_trigger_level, D("4010.35"))
        strategy.stopped("BUY", state.long.stop, "anchor-stop")
        strategy.reopened("BUY", D("4010.45"), "buy-2", "anchor-open")
        self.assertEqual(state.long.original_trigger_level, D("4010.35"))
        self.assertEqual(state.long.current_entry, D("4010.45"))

    def test_favorable_sequential_fill_gap_is_absolute_spread(self):
        state = CycleState(); strategy = Strategy(Settings(), state)
        strategy.begin(D("4658.50"), D("4658.70"))
        strategy.confirm_initial_fills(D("4658.48"), D("4658.72"))
        self.assertEqual(state.entry_spread, D("0.24"))
        self.assertEqual(state.general_recovery, D("0.054"))
        self.assertEqual(state.long.take_profit, D("4660.02"))
        self.assertEqual(state.short.take_profit, D("4657.18"))

    def test_absolute_stop_slippage_is_added_in_both_directions(self):
        self.strategy.stopped("SELL", D("4011.10"), "stop-1")
        self.assertEqual(self.state.general_recovery, D("0.070"))
        second = CycleState(); strategy = Strategy(Settings(), second)
        strategy.begin(D("4010.30"), D("4010.00")); strategy.confirm_initial_fills(D("4010.30"), D("4010.00"))
        strategy.stopped("BUY", D("4009.20"), "stop-2")
        self.assertEqual(second.general_recovery, D("0.070"))

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
        self.strategy.stopped("SELL", D("4011.10"), "sell-stop")
        self.strategy.reopened("SELL", D("4009.90"), "short-2", "sell-open")
        self.assertEqual(self.state.general_recovery, D("0.180"))
        self.assertEqual(self.state.short.original_trigger_level, D("4010.00"))
        self.assertEqual(self.state.short.current_entry, D("4009.90"))

    def test_target_helper_is_symmetric_and_independent_of_opposite_stop(self):
        self.assertEqual(target_for("BUY", D("4011.05"), D("1"), D("0.65")), D("4012.70"))
        self.assertEqual(target_for("SELL", D("4010.70"), D("1"), D("0.65")), D("4009.05"))
        before = self.state.long.take_profit
        self.state.short.stop = D("9999")
        self.strategy._targets_from_entries()
        self.assertEqual(self.state.long.take_profit, before)

    def test_recovery_table_scenarios_one_through_nine(self):
        previous = self.state.general_recovery
        for index, direction in enumerate(("SELL", "BUY", "SELL", "BUY", "SELL", "BUY", "SELL", "BUY"), 2):
            leg = self.state.short if direction == "SELL" else self.state.long
            self.strategy.stopped(direction, leg.stop, f"stop-{index}")
            self.strategy.reopened(direction, leg.original_trigger_level, f"deal-{index}", f"open-{index}")
            self.assertGreater(self.state.general_recovery, previous)
            previous = self.state.general_recovery
        self.assertEqual(self.state.scenario, 9)
        self.assertEqual(self.state.phase, "SCENARIO_9_CLOSING")

    def test_refresh_targets_migrates_old_tp_without_changing_recovery(self):
        recovery = self.state.recovery
        self.state.long.take_profit = D("999")
        self.state.short.take_profit = D("-999")
        self.strategy.refresh_targets()
        self.assertEqual(self.state.recovery, recovery)
        self.assertEqual(self.state.long.take_profit, D("4011.90"))
        self.assertEqual(self.state.short.take_profit, D("4008.40"))

    def test_scenario_nine_enters_automatic_closing_phase(self):
        self.state.scenario = 8
        self.strategy.stopped("SELL", self.state.short.stop, "s8-stop")
        self.strategy.reopened("SELL", self.state.short.original_trigger_level, "short-9", "s9-open")
        self.assertEqual(self.state.phase, "SCENARIO_9_CLOSING")

    def test_scenario_nine_result_uses_actual_close_gap_and_prior_losses(self):
        self.state.realized_losses = D("20")
        self.strategy.complete_scenario_nine(D("4001.5"), D("4000.3"), D("0.2"))
        self.assertEqual(self.state.scenario_nine_prior_losses, D("20"))
        self.assertEqual(self.state.scenario_nine_close_gap, D("1.2"))
        self.assertEqual(self.state.scenario_nine_extra_loss, D("0.2"))
        self.assertEqual(self.state.scenario_nine_total_loss, D("21.4"))
        self.assertEqual(self.state.scenario_nine_long_fill, D("4001.5"))
        self.assertEqual(self.state.scenario_nine_short_fill, D("4000.3"))
        self.assertEqual(self.state.net_cycle_result, D("-21.4"))
        self.assertFalse(self.state.manual)
        self.assertFalse(self.state.active)

    def test_zero_profit_override_is_not_replaced_by_config_default(self):
        self.state.reset(); self.state.profit_override = D("0"); self.state.profit_override_remaining = 1
        self.strategy.begin(D("4010.3"), D("4010")); self.strategy.confirm_initial_fills(D("4010.3"), D("4010"))
        self.assertEqual(self.state.target_value, D("0.0"))
        self.assertEqual(self.state.general_recovery, D("0.03"))

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
        with tempfile.NamedTemporaryFile() as file:
            self.state.save(file.name); restored = CycleState.load(file.name)
        self.assertEqual(restored.general_recovery, self.state.general_recovery)
        self.assertEqual(restored.target_value, self.state.target_value)
        self.assertEqual(restored.recovery_events, self.state.recovery_events)

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


class ScenarioParameterStrategyTest(unittest.TestCase):
    SIZES = tuple(map(D, ("10", "10", "10", "10", "20", "20", "20", "20", "20")))
    STOPS = tuple(map(D, ("1", "1", "3", "4", "4", "4", "4", "4", "4")))

    def make_strategy(self, sizes=None):
        state = CycleState()
        cfg = Settings(size=D("10"), stop_distance=D("1"), target_profit=D("0.30"),
                       scenario_sizes=sizes or self.SIZES,
                       scenario_stop_distances=self.STOPS)
        strategy = Strategy(cfg, state)
        strategy.begin(D("4010.50"), D("4010.00"))
        strategy.confirm_initial_fills(D("4010.50"), D("4010.00"))
        return strategy, state

    @staticmethod
    def reopen_exact(strategy, state, direction):
        leg = state.long if direction == "BUY" else state.short
        strategy.stopped(direction, leg.stop, f"stop-{state.scenario}-{direction}")
        strategy.reopened(direction, leg.original_trigger_level,
                          f"deal-{state.scenario + 1}-{direction}")

    def test_full_control_example_without_slippage(self):
        strategy, state = self.make_strategy()
        for direction in ("BUY", "SELL", "BUY", "SELL", "BUY", "SELL", "BUY", "SELL"):
            self.reopen_exact(strategy, state, direction)
        self.assertEqual(state.scenario, 9)
        self.assertEqual(state.phase, "SCENARIO_9_CLOSING")
        self.assertTrue(all(leg.temporary_recovery == 0 for leg in (state.long, state.short)))

    def test_first_increase_slippage_is_divided_after_old_slippage_but_not_trigger_slippage(self):
        strategy, state = self.make_strategy()
        for direction in ("BUY", "SELL", "BUY"): self.reopen_exact(strategy, state, direction)
        before = state.general_recovery
        strategy.stopped("SELL", state.short.stop + D("0.10"), "sell-slip")
        self.assertEqual(state.general_recovery, before + D("1"))
        strategy.reopened("SELL", state.short.original_trigger_level - D("0.10"), "sell-5", "sell-open")
        self.assertEqual(state.short.size, D("20"))

    def test_first_buy_increase_is_mirror_of_sell_increase(self):
        strategy, state = self.make_strategy()
        for direction in ("SELL", "BUY", "SELL"): self.reopen_exact(strategy, state, direction)
        strategy.stopped("BUY", state.long.stop - D("0.10"), "buy-slip")
        strategy.reopened("BUY", state.long.original_trigger_level + D("0.10"), "buy-5", "buy-open")
        self.assertEqual(state.long.size, D("20"))
        self.assertEqual(state.short.size, D("10"))

    def test_second_increase_removes_only_temporary_parts_and_synchronizes_recovery(self):
        strategy, state = self.make_strategy()
        for direction in ("BUY", "SELL", "BUY", "SELL", "BUY"): self.reopen_exact(strategy, state, direction)
        self.assertEqual((state.long.size, state.short.size), (D("20"), D("20")))
        self.assertEqual(state.general_recovery / state.long.size, state.general_recovery / state.short.size)
        self.assertEqual((state.long.temporary_recovery, state.short.temporary_recovery), (D("0"), D("0")))

    def test_same_large_side_can_reopen_twice_without_another_division(self):
        strategy, state = self.make_strategy()
        for direction in ("BUY", "SELL", "BUY", "SELL", "SELL"): self.reopen_exact(strategy, state, direction)
        self.assertEqual(state.short.size, D("20")); self.assertEqual(state.long.size, D("10"))
        self.assertEqual(state.scenario, 6)

    def test_increase_may_begin_at_scenario_three_or_seven(self):
        for start in (3, 7):
            sizes = tuple(D("10") if scenario < start else D("20")
                          for scenario in range(1, 10))
            strategy, state = self.make_strategy(sizes)
            direction = "BUY"
            while state.scenario < start:
                self.reopen_exact(strategy, state, direction)
                direction = "SELL" if direction == "BUY" else "BUY"
            increased = state.long if state.long.size == 20 else state.short
            unchanged = state.short if increased is state.long else state.long
            self.assertEqual(increased.size, D("20"))
            self.assertEqual(unchanged.size, D("10"))

    def test_state_round_trip_preserves_scaled_recovery_and_temporary_parts(self):
        strategy, state = self.make_strategy(); self.reopen_exact(strategy, state, "BUY")
        with tempfile.NamedTemporaryFile() as file:
            state.save(file.name); restored = CycleState.load(file.name)
        self.assertEqual(restored.general_recovery, state.general_recovery)
        self.assertEqual(restored.pending_recovery, state.pending_recovery)

    def test_continuation_rebuilds_equal_pair_from_money_without_temporary_parts(self):
        strategy, state = self.make_strategy(); state.scenario = 6; state.cycle_attempt = 2
        state.general_recovery = D("100")
        strategy.begin_continuation(D("4020.50"), D("4020.00"))
        self.assertEqual(state.general_recovery, D("100"))
        strategy.confirm_continuation_fills(D("4020.50"), D("4020.00"))
        self.assertEqual(state.general_recovery, D("110"))

    def test_invalid_size_sequences_are_rejected(self):
        for sizes in ((D("10"),) * 8,
                      (D("10"), D("20"), D("10")) + (D("10"),) * 6,
                      (D("10"), D("30")) + (D("30"),) * 7):
            with self.assertRaises(ValueError):
                Settings(scenario_sizes=sizes,
                         scenario_stop_distances=(D("1"),) * 9).validate()


class EntryRetryTest(unittest.TestCase):
    def make_bot(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings(dry_run=False, api_key="key", identifier="id", password="password")
        bot.capital = Mock()
        bot.capital.positions.return_value = []
        def position(deal_id):
            leg = next((candidate for candidate in (bot.state.long, bot.state.short)
                        if candidate and candidate.deal_id == deal_id), None)
            if leg:
                return {"position": {"dealId": deal_id, "stopLevel": leg.stop,
                                     "profitLevel": leg.take_profit}}
            return {"position": {}}
        bot.capital.position.side_effect = position
        bot.capital.working_orders.return_value = []
        bot.telegram = Mock()
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.execution_policy = ExecutionPolicy()
        bot._flat_checks = 0
        bot.strategy.begin(D("4010.30"), D("4010.00"))
        bot.strategy.confirm_initial_fills(D("4010.30"), D("4010.00"))
        return bot

    def test_double_initial_stop_report_is_separate_and_idempotent(self):
        bot = self.make_bot()
        bot.state.cycle_id = 270
        bot.state.cycle_attempt = 1
        bot.state.attempt_result_total = D("17.25")
        buy, sell = bot.state.long, bot.state.short
        buy.size, buy.current_entry, buy.stop, buy.deal_id = D("10"), D("4286.07"), D("4285.07"), "buy"
        sell.size, sell.current_entry, sell.stop, sell.deal_id = D("10"), D("4285.12"), D("4286.12"), "sell"

        bot._report_initial_pair_closures(sell, "SL", D("4286.14"), buy, ("SL", D("4285.04")))
        text = bot.telegram.send.call_args.args[0]
        self.assertIn("Цикл №270; попытка 1", text)
        self.assertIn("-10.20", text)
        self.assertIn("-10.30", text)
        self.assertIn("-20.50", text)
        self.assertIn("НЕ добавлен", text)
        self.assertEqual(bot.state.attempt_result_total, D("17.25"))
        bot._report_initial_pair_closures(sell, "SL", D("4286.14"), buy, ("SL", D("4285.04")))
        self.assertEqual(bot.telegram.send.call_count, 1)

    def test_initial_stop_report_does_not_treat_absence_as_close(self):
        bot = self.make_bot()
        buy, sell = bot.state.long, bot.state.short
        buy.deal_id, sell.deal_id = "buy", "sell"
        bot._report_initial_pair_closures(buy, "SL", D("4009.30"), sell, None)
        text = bot.telegram.send.call_args.args[0]
        self.assertIn("не доказательство закрытия", text)
        self.assertIn("итог пары пока не объявлен", text)

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
        with patch("trader.app.time.sleep"):
            error = bot._open_initial_leg(bot.state.long)
        self.assertIsNone(error)
        self.assertEqual(bot.capital.open_position.call_count, 4)
        self.assertEqual(bot.state.long.current_entry, D("4010.35"))
        kwargs = bot.capital.open_position.call_args.kwargs
        self.assertEqual(kwargs["stop_distance"], D("1"))
        self.assertNotIn("profit_distance", kwargs)

    def test_trigger_and_market_fallback_use_next_scenario_parameters(self):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            scenario_sizes=(D("10"), D("20")) + (D("20"),) * 7,
            scenario_stop_distances=(D("1"), D("3")) + (D("3"),) * 7,
        )
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4010.50"), D("4010.00"))
        bot.strategy.confirm_initial_fills(D("4010.50"), D("4010.00"))
        bot.strategy.stopped("BUY", D("4009.50"), "buy-stop")
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "buy-trigger"
        }
        bot.capital.working_stop.return_value = "trigger-ref"

        bot._create_trigger(bot.state.long)

        args = bot.capital.working_stop.call_args.args
        self.assertEqual(args[2], D("20"))
        self.assertEqual(args[3:], (D("4010.50"), D("4007.50"), D("4014.40")))

        bot.capital.working_stop.reset_mock()
        bot.capital.open_position.return_value = "market-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "buy-2", "level": 4010.4,
            "affectedDeals": [{"dealId": "buy-2", "status": "OPENED"}],
        }
        bot.capital.wait_position.return_value = {
            "dealId": "buy-2", "direction": "BUY", "level": 4010.4,
        }
        bot.state.long.trigger_id = ""
        bot._open_passed_trigger_at_market(bot.state.long, D("4015.40"))
        market_args = bot.capital.open_position.call_args.args
        self.assertEqual(market_args[2], D("20"))
        self.assertEqual(market_args[3], D("4007.50"))

    def test_trigger_transition_notification_precedes_protection_reports(self):
        bot = self.make_bot()
        bot.strategy.stopped("SELL", D("4011.00"), "stop-short")
        bot.state.short.trigger_id = "trigger-short"
        bot.telegram.reset_mock()
        bot._apply_protection = Mock(return_value=True)
        positions = {
            "short-2": {
                "dealId": "short-2", "direction": "SELL", "level": 4010.00,
                "workingOrderId": "trigger-short",
            }
        }

        bot._detect_trigger_fill(positions)

        first_report = bot.telegram.send.call_args_list[0].args[0]
        self.assertIn("Trigger исполнен — переход в сценарий 2", first_report)
        self.assertEqual(bot._apply_protection.call_count, 2)

    def test_scenario_nine_removes_protection_and_closes_both_sides(self):
        bot = self.make_bot()
        bot.state.scenario = 8
        bot.state.realized_losses = D("20")
        bot.strategy.stopped("SELL", bot.state.short.stop, "sell-sl-8")
        bot.strategy.reopened("SELL", D("4010"), "short-9", "sell-open-9")
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
                payload["dealId"] = reference.removeprefix("close-")
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
        self.assertEqual(bot.state.scenario_nine_total_loss, D("22.20"))
        self.assertTrue(bot.state.scenario_nine_triggers_verified)
        self.assertEqual(bot.state.scenario_nine_extra_loss, D("0"))
        self.assertEqual(bot.capital.close_position.call_count, 2)
        bot.capital.update_position.assert_any_call("long-9", None, None)
        bot.capital.update_position.assert_any_call("short-9", None, None)

    def test_inactive_completed_cycle_never_recreates_trigger(self):
        bot = self.make_bot()
        bot.state.short.open = False
        bot.state.short.trigger_id = ""
        bot.state.short.stop = None
        bot.state.active = False
        bot.state.phase = "FILTER"

        bot._ensure_expected_trigger()

        bot.capital.working_stop.assert_not_called()

    def test_scenario_nine_ignores_foreign_close_confirmation_price(self):
        bot = self.make_bot()
        bot.state.scenario = 8
        bot.state.realized_losses = D("0")
        bot.state.long.deal_id = "00000000-618c-b0bf"
        bot.state.long.current_entry = D("4406.78")
        bot.state.short.deal_id = "00000000-618c-d4ed"
        bot.state.short.current_entry = D("4406.18")
        bot.capital.positions.return_value = [
            {"position": {"dealId": bot.state.long.deal_id, "direction": "BUY",
                          "level": 4406.78}, "market": {"epic": bot.cfg.epic}},
            {"position": {"dealId": bot.state.short.deal_id, "direction": "SELL",
                          "level": 4406.18}, "market": {"epic": bot.cfg.epic}},
        ]
        bot.capital.update_position.side_effect = lambda deal_id, stop, tp: f"update-{deal_id}"
        bot.capital.close_position.side_effect = lambda deal_id: f"close-{deal_id}"

        def confirmation(reference):
            if reference.startswith("update-"):
                return {"dealStatus": "ACCEPTED"}
            if reference.endswith("b0bf"):
                return {"dealStatus": "ACCEPTED", "dealId": "00000000-618c-b0bf",
                        "affectedDeals": [{"dealId": "00000000-618c-b0bf",
                                           "status": "CLOSED"}], "level": 4406.02}
            # Reproduce the journal defect: SELL confirmation carries the BUY id and price.
            return {"dealStatus": "ACCEPTED", "dealId": "00000000-618c-b0bf",
                    "affectedDeals": [{"dealId": "00000000-618c-b0bf",
                                       "status": "CLOSED"}], "level": 4406.02}

        bot.capital.wait_confirmation.side_effect = confirmation
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: ([{
            "dealId": "00000000-618c-d4ed", "source": "USER", "type": "POSITION",
            "status": "ACCEPTED", "details": {"level": 4406.50, "direction": "BUY"},
        }] if deal_id == "00000000-618c-d4ed" else [])
        with patch("trader.app.time.sleep"):
            bot._enter_manual_nine()

        self.assertEqual(bot.state.scenario_nine_long_fill, D("4406.02"))
        self.assertEqual(bot.state.scenario_nine_short_fill, D("4406.50"))
        self.assertEqual(bot.state.scenario_nine_close_gap, D("0.48"))

    def test_scenario_nine_actual_deal_result_updates_attempt_total_once(self):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            size=D("10"), state_file=bot.cfg.state_file,
        )
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.state.scenario = 8
        bot.state.attempt_counter = bot.state.active_attempt_id = 9
        bot.state.diagnostic_cycle_number = 9
        bot.state.attempt_result_total = D("20")
        bot.state.long.deal_id, bot.state.long.current_entry = "long-9", D("100")
        bot.state.short.deal_id, bot.state.short.current_entry = "short-9", D("100")
        bot.state.attempt_deal_ids = ["long-9", "short-9"]
        bot.state.remember_deal(bot.state.long)
        bot.state.remember_deal(bot.state.short)
        bot.capital.positions.return_value = [
            {"position": {"dealId": "long-9", "direction": "BUY", "level": 100},
             "market": {"epic": bot.cfg.epic}},
            {"position": {"dealId": "short-9", "direction": "SELL", "level": 100},
             "market": {"epic": bot.cfg.epic}},
        ]
        bot.capital.update_position.side_effect = lambda deal_id, stop, tp: f"update-{deal_id}"
        bot.capital.close_position.side_effect = lambda deal_id: f"close-{deal_id}"
        bot.capital.wait_confirmation.side_effect = lambda reference: (
            {"dealStatus": "ACCEPTED"} if reference.startswith("update-") else
            {"dealStatus": "ACCEPTED", "dealId": reference.removeprefix("close-"),
             "level": 94 if reference.endswith("long-9") else 107}
        )

        with patch("trader.app.time.sleep"):
            bot._enter_manual_nine()
            bot._refresh_actual_attempt_result()

        attempt = next(item for item in bot.state.attempt_history if item["attempt_id"] == 9)
        self.assertEqual(attempt["actual_result"], "-130")
        self.assertEqual(attempt["actual_result_status"], "CONFIRMED")
        self.assertEqual(bot.state.attempt_result_total, D("-110"))
        self.assertEqual(bot.state.pending_actual_attempt_id, 0)

    def test_actual_attempt_result_uses_each_deal_own_size(self):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            scenario_sizes=(D("10"), D("20")) + (D("20"),) * 7,
            scenario_stop_distances=(D("1"),) * 9,
            state_file=bot.cfg.state_file,
        )
        bot.state.pending_actual_attempt_id = 31
        bot.state.pending_actual_deal_ids = ["small", "large"]
        bot.state.attempt_history = [{"attempt_id": 31, "status": "PENDING"}]
        bot.state.deal_history = [
            {"deal_id": "small", "direction": "BUY", "entry": "100",
             "close_level": "99", "size": "10"},
            {"deal_id": "large", "direction": "SELL", "entry": "100",
             "close_level": "102", "size": "20"},
        ]

        self.assertTrue(bot._refresh_actual_attempt_result())
        self.assertEqual(bot.state.attempt_history[0]["actual_result"], "-50")
        self.assertEqual(bot.state.attempt_result_total, D("-50"))

    def test_pending_scenario_nine_actual_result_survives_reload_and_is_idempotent(self):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            size=D("10"), state_file=bot.cfg.state_file,
        )
        bot.state.attempt_history = [{
            "attempt_id": 12, "status": "COMPLETED_SCENARIO_9",
            "result": "-99", "actual_result_status": "PENDING",
        }]
        bot.state.deal_history = [{
            "deal_id": "scenario9-deal", "direction": "BUY", "entry": "100",
            "close_level": None,
        }]
        bot.state.pending_actual_attempt_id = 12
        bot.state.pending_actual_deal_ids = ["scenario9-deal"]
        bot.capital.activity.return_value = []
        self.assertFalse(bot._refresh_actual_attempt_result())
        restored = CycleState.load(bot.cfg.state_file)
        self.assertEqual(restored.pending_actual_attempt_id, 12)

        bot.state = restored
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.capital.activity.return_value = [{
            "dealId": "scenario9-deal", "source": "USER", "type": "POSITION",
            "status": "ACCEPTED", "details": {"direction": "SELL", "level": 97},
        }]
        self.assertTrue(bot._refresh_actual_attempt_result())
        self.assertTrue(bot._refresh_actual_attempt_result())
        self.assertEqual(bot.state.attempt_result_total, D("-30"))
        self.assertEqual(bot.state.attempt_history[0]["actual_result_status"], "CONFIRMED")

    def test_scenario_nine_closes_trigger_that_executes_during_cancellation(self):
        bot = self.make_bot()
        bot.capital.activity.return_value = []
        bot.capital.positions.return_value = [{
            "position": {
                "dealId": "extra-sell", "workingOrderId": "trigger-sell",
                "direction": "SELL", "level": 4000,
            },
            "market": {"epic": "GOLD"},
        }]
        bot.capital.close_position.return_value = "close-extra"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "extra-sell", "level": 4001,
        }

        with patch("trader.app.time.sleep"):
            loss = bot._close_scenario_nine_trigger_races(
                {"trigger-sell"}, {"long-9", "short-9"}
            )

        self.assertEqual(loss, D("1"))
        bot.capital.close_position.assert_called_once_with("extra-sell")
        self.assertIn("исполнился во время отмены", bot.telegram.send.call_args.args[0])

    def test_scenario_nine_verifies_trigger_cancellation_with_three_empty_reads(self):
        bot = self.make_bot()
        order = {
            "workingOrderData": {"dealId": "trigger-buy"},
            "marketData": {"epic": "GOLD"},
        }
        bot.state.cycle_trigger_ids = ["trigger-buy"]
        bot.capital.working_orders.side_effect = [[order], [], [], []]
        bot.capital.delete_working_order.return_value = True

        with patch("trader.app.time.sleep"):
            uncertain = bot._cancel_and_verify_scenario_nine_triggers()

        self.assertEqual(uncertain, set())
        self.assertTrue(bot.state.scenario_nine_triggers_verified)
        bot.capital.delete_working_order.assert_called_once_with("trigger-buy")

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
            "dealStatus": "ACCEPTED", "dealId": "short-1", "level": 4008.18,
        }

        protected = bot._apply_protection(leg)

        self.assertFalse(protected)
        bot.capital.update_position.assert_not_called()
        bot.capital.close_position.assert_called_once_with("short-1")
        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.net_cycle_result, D("0.77"))
        self.assertIn("Целевая цена достигнута", bot.telegram.send.call_args.args[0])

    def test_market_take_profit_cancels_pending_trigger_before_completion(self):
        bot = self.make_bot()
        winner = bot.state.short
        bot.strategy.stopped("BUY", D("4009.25"), "stop-long")
        winner.deal_id = "short-1"
        bot.state.long.trigger_id = "buy-trigger"
        bot.capital.quote.return_value = (D("4008.00"), D("4008.20"))
        bot.capital.close_position.return_value = "close-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "short-1", "level": 4008.18,
        }
        bot.capital.delete_working_order.return_value = True

        self.assertFalse(bot._apply_protection(winner))

        bot.capital.delete_working_order.assert_called_once_with("buy-trigger")
        self.assertEqual(bot.state.long.trigger_id, "")
        self.assertFalse(bot.state.active)

    def test_market_take_profit_accounts_for_trigger_race_before_completion(self):
        bot = self.make_bot()
        winner = bot.state.short
        bot.strategy.stopped("BUY", D("4009.25"), "stop-long")
        winner.deal_id = "short-1"
        bot.state.long.trigger_id = "buy-trigger"
        bot.capital.quote.return_value = (D("4008.00"), D("4008.20"))
        bot.capital.close_position.return_value = "close-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "short-1", "level": 4008.18,
        }
        bot.capital.delete_working_order.return_value = False
        bot._close_trigger_that_raced_with_tp = Mock(return_value=D("0.20"))

        self.assertFalse(bot._apply_protection(winner))

        self.assertEqual(bot.state.realized_losses, D("1.25"))
        self.assertEqual(bot.state.net_cycle_result, D("0.57"))
        bot._close_trigger_that_raced_with_tp.assert_called_once_with(bot.state.long)

    def test_market_take_profit_waits_across_ticks_for_uncertain_trigger(self):
        bot = self.make_bot()
        winner = bot.state.short
        bot.strategy.stopped("BUY", D("4009.25"), "stop-long")
        winner.deal_id = "short-1"
        bot.state.long.trigger_id = "buy-trigger"
        bot.capital.quote.return_value = (D("4008.00"), D("4008.20"))
        bot.capital.close_position.return_value = "close-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "short-1", "level": 4008.18,
        }
        bot.capital.delete_working_order.return_value = False
        bot._close_trigger_that_raced_with_tp = Mock(side_effect=[None, D("0.20")])

        self.assertFalse(bot._apply_protection(winner))
        self.assertTrue(bot.state.active)
        self.assertEqual(bot.state.pending_tp_direction, "SELL")
        self.assertEqual(bot.state.pending_tp_fill, D("4008.18"))

        bot._tick_cycle()

        bot.capital.close_position.assert_called_once_with("short-1")
        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.pending_tp_direction, "")
        self.assertIsNone(bot.state.pending_tp_fill)
        self.assertEqual(bot.state.net_cycle_result, D("0.57"))

    def test_identifierless_close_waits_for_history_without_second_delete(self):
        bot = self.make_bot()
        winner = bot.state.long
        winner.deal_id = "buy-close-id"
        winner.take_profit = D("4011.60")
        bot.state.phase = "LONG_ONLY"
        bot.state.short.open = False
        bot.capital.quote.return_value = D("4011.70"), D("4011.90")
        bot.capital.close_position.return_value = "close-ref"
        # ACCEPTED level is deliberately untrusted because both dealId and affectedDeals are absent.
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "level": 4011.72,
        }
        close_visible = False
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: ([{
            "dealId": "buy-close-id", "source": "TP", "type": "POSITION",
            "status": "ACCEPTED", "details": {"level": 4011.71, "direction": "SELL"},
        }] if close_visible else [])

        with patch("trader.app.time.sleep"):
            self.assertFalse(bot._apply_protection(winner))
        self.assertEqual(bot.state.pending_close_reference, "close-ref")
        bot.capital.close_position.assert_called_once()

        close_visible = True
        with patch("trader.app.time.sleep"):
            bot._tick_cycle()
        bot.capital.close_position.assert_called_once()
        self.assertEqual(bot.state.pending_close_reference, "")
        self.assertFalse(bot.state.active)

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
            "dealStatus": "ACCEPTED", "dealId": "short-1", "level": 4008.18,
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

    def test_initial_transport_timeout_reconciles_reference_without_second_market_order(self):
        bot = self.make_bot()
        bot.capital.open_position.side_effect = CapitalError("transport timeout")
        bot.capital.wait_confirmation.side_effect = CapitalError("confirmation unavailable")

        with patch("trader.app.time.sleep"):
            error = bot._open_initial_leg(bot.state.long)

        self.assertIn("повторное открытие заблокировано", error)
        bot.capital.open_position.assert_called_once()
        bot.capital.wait_confirmation.assert_not_called()
        self.assertFalse(bot.state.manual)
        self.assertTrue(bot.state.active)
        self.assertEqual(bot.state.long.pending_market_kind, "INITIAL")

    def test_initial_confirmation_timeout_persists_reference_and_blocks_filter_reset(self):
        bot = self.make_bot()
        bot.capital.open_position.return_value = "initial-ref"
        bot.capital.wait_confirmation.side_effect = CapitalError("confirmation unavailable")
        bot.capital.positions.return_value = []

        with patch("trader.app.time.sleep"):
            error = bot._open_initial_leg(bot.state.long)

        self.assertIn("повторное открытие заблокировано", error)
        bot.capital.open_position.assert_called_once()
        self.assertFalse(bot.state.manual)
        self.assertTrue(bot.state.active)
        self.assertFalse(bot.state.armed)
        self.assertEqual(bot.state.long.pending_market_reference, "initial-ref")

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

    def test_initial_empty_affected_deals_links_open_and_sl_then_pauses(self):
        bot = self.make_bot()
        bot.state.reset()
        bot._flat_checks = 2
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            size=D("10"), state_file=bot.cfg.state_file,
        )
        bot.strategy = Strategy(bot.cfg, bot.state)
        execution_id = "00000000-618f-7312"
        position_id = "00000000-618f-7315"
        bot.capital.positions.return_value = []
        bot.capital.working_orders.return_value = []
        bot.capital.quote.return_value = D("4405.10"), D("4405.60")
        bot.capital.open_position.return_value = "initial-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": execution_id,
            "affectedDeals": [], "level": 4405.60, "direction": "BUY",
        }
        bot.capital.wait_position.side_effect = CapitalError("position already absent")
        opening = {
            "dealId": position_id, "source": "USER", "type": "POSITION",
            "status": "ACCEPTED", "details": {
                "workingOrderId": execution_id, "direction": "BUY", "level": 4405.60,
            },
        }
        closing = {
            "dealId": position_id, "source": "SL", "type": "POSITION",
            "status": "ACCEPTED", "details": {
                "direction": "SELL", "level": 4404.08, "openPrice": 4405.60,
            },
        }
        global_reads = 0

        def activity(deal_id="", last_period=86400):
            nonlocal global_reads
            if deal_id:
                return [{"dealId": execution_id, "source": "USER",
                         "type": "WORKING_ORDER", "status": "EXECUTED"}]
            global_reads += 1
            return [] if global_reads == 1 else [opening, closing]

        bot.capital.activity.side_effect = activity
        with patch("trader.app.time.sleep"):
            bot._start_cycle("demo candle")

        self.assertTrue(bot.state.paused)
        self.assertEqual(bot.state.phase, "PAUSED")
        self.assertFalse(bot.state.armed)
        self.assertEqual(bot.state.attempt_counter, 1)
        self.assertEqual(bot.state.attempt_result_total, D("-15.20"))
        self.assertEqual(bot.state.attempt_history[-1]["status"], "INITIAL_PAIR_NOT_FORMED")
        self.assertEqual(bot.state.deal_history[-1]["deal_id"], position_id)
        self.assertEqual(bot.state.deal_history[-1]["close_source"], "SL")
        self.assertEqual(bot.capital.open_position.call_count, 1)
        report = bot.telegram.send.call_args.args[0]
        self.assertIn("следующий вход только после /start", report)
        self.assertIn("-15.20", report)

    def test_failed_initial_attempt_ids_are_unique_and_do_not_enter_recovery(self):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            size=D("10"), state_file=bot.cfg.state_file,
        )
        results = [(D("4405.60"), D("4404.05")),
                   (D("4406.00"), D("4404.43")),
                   (D("4405.60"), D("4404.08"))]
        for attempt_id, (entry, close) in enumerate(results, 1):
            bot.state.attempt_counter = attempt_id
            bot.state.active_attempt_id = attempt_id
            bot.state.diagnostic_cycle_number = attempt_id
            leg = Leg("BUY", entry, entry, deal_id=f"buy-{attempt_id}")
            bot.state.long = leg
            bot._finish_failed_initial_attempt(leg, "SL", close, opposite_sent=False)

        self.assertEqual([item["attempt_id"] for item in bot.state.attempt_history], [1, 2, 3])
        self.assertEqual(bot.state.attempt_result_total, D("-46.40"))
        self.assertEqual(bot.state.recovery, D("0"))
        self.assertEqual(bot.state.completed_cycles, 0)

    def test_historical_opening_stays_pending_until_linked_close_arrives(self):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            size=D("10"), state_file=bot.cfg.state_file,
        )
        bot.strategy = Strategy(bot.cfg, bot.state)
        leg = bot.state.long
        execution_id, position_id = "execution-7312", "position-7315"
        leg.pending_market_kind = "INITIAL"
        leg.pending_market_reference = "initial-ref"
        leg.pending_market_preexisting_ids = []
        bot.state.active_attempt_id = bot.state.attempt_counter = 17
        bot.state.diagnostic_cycle_number = 17
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": execution_id,
            "affectedDeals": [], "level": 4405.60, "direction": "BUY",
        }
        bot.capital.wait_position.side_effect = CapitalError("already closed")
        bot.capital.positions.return_value = []
        opening = {
            "dealId": position_id, "source": "USER", "type": "POSITION",
            "status": "ACCEPTED", "details": {
                "workingOrderId": execution_id, "direction": "BUY", "level": 4405.60,
            },
        }
        closing = {
            "dealId": position_id, "source": "SL", "type": "POSITION",
            "status": "ACCEPTED", "details": {"direction": "SELL", "level": 4404.08},
        }
        close_visible = False

        def activity(deal_id="", last_period=86400):
            if deal_id == position_id:
                return [closing] if close_visible else []
            return [opening] + ([closing] if close_visible else [])

        bot.capital.activity.side_effect = activity
        with patch("trader.app.time.sleep"):
            self.assertTrue(bot._resume_pending_market())
        self.assertEqual(leg.pending_market_kind, "INITIAL")
        self.assertFalse(bot.state.paused)

        close_visible = True
        with patch("trader.app.time.sleep"):
            self.assertTrue(bot._resume_pending_market())
        self.assertTrue(bot.state.paused)
        self.assertEqual(bot.state.attempt_history[-1]["attempt_id"], 17)
        self.assertEqual(bot.state.attempt_result_total, D("-15.20"))

    def _pending_second_initial_bot(self, both_closed=False):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            size=D("10"), stop_distance=D("1.5"), target_profit=D("0.5"),
            state_file=bot.cfg.state_file,
        )
        bot.state.reset()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("100"), D("99.5"))
        bot.state.attempt_counter = bot.state.active_attempt_id = 21
        bot.state.diagnostic_cycle_number = 21
        bot.state.initial_submitted_directions = ["BUY", "SELL"]
        bot.state.long.deal_id = "buy-position"
        bot.state.long.deal_reference = "buy-ref"
        bot.state.short.pending_market_kind = "INITIAL"
        bot.state.short.pending_market_reference = "sell-ref"
        bot.state.short.pending_market_preexisting_ids = ["buy-position"]
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "sell-execution",
            "affectedDeals": [{"dealId": "sell-position", "status": "OPENED"}],
            "level": 99.5, "direction": "SELL",
        }
        bot.capital.wait_position.return_value = {
            "dealId": "sell-position", "direction": "SELL", "level": 99.5,
        }
        sell_position = {"position": {
            "dealId": "sell-position", "direction": "SELL", "level": 99.5,
            "stopLevel": 101.0, "profitLevel": 97.5,
        }, "market": {"epic": bot.cfg.epic}}
        bot.capital.positions.return_value = [] if both_closed else [sell_position]
        events = [{
            "dealId": "buy-position", "source": "SL", "type": "POSITION",
            "status": "ACCEPTED", "details": {"level": 98.5, "direction": "SELL"},
        }]
        if both_closed:
            events.append({
                "dealId": "sell-position", "source": "TP", "type": "POSITION",
                "status": "ACCEPTED", "details": {"level": 97.5, "direction": "BUY"},
            })
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: (
            [item for item in events if not deal_id or item["dealId"] == deal_id]
        )
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "buy-trigger-ref"
        bot.capital.wait_confirmation.side_effect = lambda reference: (
            {"dealStatus": "ACCEPTED", "dealId": "buy-trigger"}
            if reference == "buy-trigger-ref" else {
                "dealStatus": "ACCEPTED", "dealId": "sell-execution",
                "affectedDeals": [{"dealId": "sell-position", "status": "OPENED"}],
                "level": 99.5, "direction": "SELL",
            }
        )
        return bot

    def test_delayed_second_initial_with_opposite_sl_continues_formed_cycle(self):
        bot = self._pending_second_initial_bot()
        with patch("trader.app.time.sleep"):
            bot._tick_cycle()
        bot.capital.open_position.assert_not_called()
        self.assertEqual(bot.state.phase, "SHORT_ONLY")
        self.assertEqual(bot.state.realized_losses, D("1.5"))
        self.assertEqual(bot.state.long.trigger_id, "buy-trigger")
        self.assertFalse(bot.state.paused)

    def test_delayed_second_initial_both_closed_accounts_sl_before_tp_once(self):
        bot = self._pending_second_initial_bot(both_closed=True)
        with patch("trader.app.time.sleep"):
            bot._tick_cycle()
            self.assertFalse(bot._resume_pending_market())
        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.realized_losses, D("1.5"))
        self.assertEqual(bot.state.gross_take_profit, D("2.0"))
        self.assertEqual(bot.state.net_cycle_result, D("0.5"))
        self.assertEqual(bot.state.attempt_result_total, D("5.0"))
        self.assertEqual(len(bot.state.attempt_history), 1)

    def test_tick_reconciles_two_stops_and_nonblocking_pause_then_filter(self):
        bot = self.make_bot()
        object.__setattr__(bot.cfg, "size", D("10"))
        bot.state.active_attempt_id = bot.state.attempt_counter = bot.state.cycle_id = 224
        bot.state.cycle_attempt = 1
        bot.state.long.deal_id = "buy-224"
        bot.state.short.deal_id = "sell-224"
        bot.state.long.current_entry = D("4372.74")
        bot.state.short.current_entry = D("4371.16")
        bot.state.realized_losses = D("10.82")
        bot.state.realized_loss_money = D("108.20")
        bot.state.long.size = bot.state.short.size = D("10")
        bot.capital.positions.return_value = []
        events = [
            {"dealId": "buy-224", "source": "SL", "status": "ACCEPTED",
             "type": "POSITION", "details": {"level": 4371.22}},
            {"dealId": "sell-224", "source": "SL", "status": "ACCEPTED",
             "type": "POSITION", "details": {"level": 4373.53}},
        ]
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: events
        with tempfile.TemporaryDirectory() as directory, \
                patch("trader.app.time.sleep"), patch("trader.app.time.time", return_value=1000):
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            object.__setattr__(bot.cfg, "diagnostic_log_file", str(Path(directory) / "log"))
            bot._tick_cycle()
            self.assertEqual(bot.state.phase, "DOUBLE_SL_PAUSE")
            self.assertTrue(bot.state.continuation_managed)
            self.assertEqual(bot.state.continuation_stage, "PAUSE")
            self.assertEqual(bot.state.continuation_pause_until, 1300)
            self.assertEqual(bot.state.realized_losses, D("14.71"))
            self.assertEqual(bot.state.attempt_result_total, D("-147.10"))
            with patch("trader.app.time.time", return_value=1299):
                bot.tick()
            self.assertEqual(bot.state.phase, "DOUBLE_SL_PAUSE")
            with patch("trader.app.time.time", return_value=1301):
                bot.tick()
            self.assertEqual(bot.state.phase, "CONTINUATION_FILTER")
            self.assertEqual(bot.state.continuation_stage, "FILTER")

    def test_continuation_controller_owns_pending_pair_reconciliation(self):
        bot = self._pending_second_initial_bot(both_closed=False)
        bot.state.cycle_id = 254
        bot.state.cycle_attempt = 2
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "FORMING_PAIR"
        bot.continuation = CycleContinuation(bot)
        bot.command("/stop")
        with patch("trader.app.time.sleep"):
            bot.tick()
            bot.tick()
        bot.capital.open_position.assert_not_called()
        self.assertTrue(bot.state.continuation_managed)
        self.assertTrue(bot.state.continuation_stopped_by_user)
        self.assertIn(bot.state.continuation_stage, {"FORMING_PAIR", "ACTIVE"})

    def test_continuation_active_dispatch_uses_second_increase_recovery(self):
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            scenario_sizes=(D("10"),) * 4 + (D("20"),) * 5,
            scenario_stop_distances=(D("1"), D("1"), D("3")) + (D("4"),) * 6,
        )
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4010.50"), D("4010.00"))
        bot.strategy.confirm_initial_fills(D("4010.50"), D("4010.00"))
        for direction in ("BUY", "SELL", "BUY"):
            leg = bot.state.long if direction == "BUY" else bot.state.short
            bot.strategy.stopped(direction, leg.stop, f"stop-{bot.state.scenario}-{direction}")
            bot.strategy.reopened(direction, leg.original_trigger_level,
                                  f"deal-{bot.state.scenario + 1}-{direction}")
        bot.strategy.stopped("SELL", bot.state.short.stop, "stop-sell-4")
        bot.strategy.reopened("SELL", bot.state.short.original_trigger_level,
                              "sell-5", "fill-sell-5")
        bot.strategy.stopped("BUY", bot.state.long.stop, "stop-buy-5")
        bot.state.long.trigger_id = "buy-trigger-6"
        bot.state.short.deal_id = "sell-5"
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "ACTIVE"
        bot.continuation = CycleContinuation(bot)
        bot._apply_protection = Mock(return_value=True)
        bot._ensure_expected_trigger = Mock()
        bot.capital.positions.return_value = [
            {"position": {"dealId": "sell-5", "direction": "SELL", "level": 4010,
                          "size": 20}, "market": {"epic": "GOLD"}},
            {"position": {"dealId": "buy-6", "direction": "BUY", "level": 4010.50,
                          "size": 20, "workingOrderId": "buy-trigger-6"},
             "market": {"epic": "GOLD"}},
        ]

        bot.continuation.handle_active_scenario()

        self.assertEqual(bot.state.scenario, 6)
        self.assertEqual(bot.state.general_recovery / bot.state.long.size, D("6.90"))
        self.assertEqual(bot.state.general_recovery / bot.state.short.size, D("6.90"))
        self.assertEqual(bot.state.long.temporary_recovery, D("0"))
        self.assertEqual(bot.state.short.temporary_recovery, D("0"))

    def test_continuation_preflight_progresses_across_ticks(self):
        bot = self.make_bot()
        bot.state.continuation_managed = True
        controller = bot.continuation = CycleContinuation(bot)
        bot.capital.positions.return_value = []
        bot.capital.working_orders.return_value = []
        bot._start_pair_common = Mock()
        controller.start_pair("closed candle")
        controller.tick()
        controller.tick()
        bot._start_pair_common.assert_not_called()
        controller.tick()
        bot._start_pair_common.assert_called_once_with(
            "closed candle", continuation=True, preflight_done=True
        )
        self.assertEqual(bot.state.continuation_flat_checks, 3)
        self.assertEqual(bot.state.continuation_stage, "FORMING_PAIR")

    def test_fast_continuation_stop_uses_current_scenario_and_carried_recovery(self):
        bot = self.make_bot()
        bot.state.scenario = 5
        bot.state.realized_losses = D("14.71")
        bot.state.cycle_target_profit = D("0.40")
        bot.state.cycle_attempt = 2
        bot.state.continuation_managed = True
        controller = bot.continuation = CycleContinuation(bot)
        bot.strategy.begin_continuation(D("100.50"), D("100.00"))
        bot.state.long.deal_id = "continued-buy"
        bot.state.short.deal_id = "continued-sell"
        bot._apply_protection = Mock(return_value=True)
        bot._create_trigger = Mock()
        controller.handle_fast_second_close(bot.state.short, "SL", D("101.05"))
        self.assertEqual(bot.state.scenario, 5)
        self.assertEqual(bot.state.general_recovery, D("0.115"))
        self.assertEqual(bot.state.long.original_trigger_level, D("100.50"))
        self.assertEqual(bot.state.short.original_trigger_level, D("100.00"))
        bot._create_trigger.assert_called_once_with(bot.state.short)
        self.assertEqual(bot.state.continuation_stage, "ACTIVE")

    def _continuation_tp_bot(self):
        bot = self.make_bot()
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "ACTIVE"
        bot.state.cycle_id = 254
        bot.state.long.deal_id = "buy-winner"
        stopped = bot.strategy.stopped("SELL", D("4011.10"), "sell-stop")
        stopped.trigger_id = "sell-trigger"
        bot.capital.positions.return_value = []
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: [{
            "dealId": "buy-winner", "source": "TP", "type": "POSITION",
            "status": "ACCEPTED", "details": {"level": 4012.0},
        }]
        bot.continuation = CycleContinuation(bot)
        return bot

    def test_continuation_tp_cancels_trigger_then_returns_to_normal_filter(self):
        bot = self._continuation_tp_bot()
        bot.capital.delete_working_order.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            object.__setattr__(bot.cfg, "diagnostic_log_file", str(Path(directory) / "log"))
            bot.continuation.handle_active_scenario()
        bot.capital.delete_working_order.assert_called_once_with("sell-trigger")
        self.assertFalse(bot.state.continuation_managed)
        self.assertFalse(bot.state.active)
        self.assertTrue(bot.state.armed)
        self.assertEqual(bot.state.phase, "FILTER")
        self.assertTrue(any("завершён по TP" in call.args[0]
                            for call in bot.telegram.send.call_args_list))

    def test_continuation_tp_waits_when_trigger_outcome_is_unknown(self):
        bot = self._continuation_tp_bot()
        bot.capital.delete_working_order.return_value = False
        bot._close_trigger_that_raced_with_tp = Mock(return_value=None)
        bot.continuation.handle_active_scenario()
        self.assertTrue(bot.state.continuation_managed)
        self.assertTrue(bot.state.active)
        self.assertEqual(bot.state.pending_tp_direction, "BUY")
        bot._complete_cycle = Mock()
        bot.continuation.handle_active_scenario()
        bot._complete_cycle.assert_not_called()

    def test_nested_pending_market_scenario_nine_completion_is_not_repeated(self):
        """Reproduce attempt 255: nested fallback completion must stop the outer dispatcher."""
        bot = self.make_bot()
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "ACTIVE"
        bot.state.scenario = 9
        bot.continuation = CycleContinuation(bot)
        completions = []

        def resolve_and_complete():
            # The shared pending-MARKET resolver may dispatch synchronously to the owner.  Model
            # the observable result of that real inner path instead of replacing the outer stage.
            completions.append(bot.state.active_attempt_id)
            bot.state.active = False
            bot.continuation.release()
            return False

        bot._resume_pending_market = Mock(side_effect=resolve_and_complete)
        bot._enter_manual_nine = Mock()

        bot.continuation.handle_active_scenario()

        self.assertEqual(len(completions), 1)
        bot._enter_manual_nine.assert_not_called()
        bot.capital.positions.assert_not_called()

    def test_scenario_nine_completion_is_idempotent_after_cycle_is_inactive(self):
        bot = self.make_bot()
        bot.state.scenario = 9
        bot.state.active = False
        completed = bot.state.completed_cycles

        bot._enter_manual_nine()

        self.assertEqual(bot.state.completed_cycles, completed)
        bot.capital.working_orders.assert_not_called()
        bot.capital.close_position.assert_not_called()

    def test_releasing_continuation_clears_only_transient_owner_metadata(self):
        bot = self.make_bot()
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "ACTIVE"
        bot.state.continuation_pause_until = 12345.0
        bot.state.continuation_flat_checks = 2
        bot.state.continuation_filter_reason = "old filter"
        bot.state.continuation_stopped_by_user = True
        bot.state.paused = True

        bot._get_continuation().release()

        self.assertFalse(bot.state.continuation_managed)
        self.assertEqual(bot.state.continuation_stage, "")
        self.assertEqual(bot.state.continuation_pause_until, 0.0)
        self.assertEqual(bot.state.continuation_flat_checks, 0)
        self.assertEqual(bot.state.continuation_filter_reason, "")
        self.assertFalse(bot.state.continuation_stopped_by_user)
        # /stop is represented by the common pause and must survive owner release.
        self.assertTrue(bot.state.paused)

    def test_preflight_stop_blocks_pair_until_start(self):
        bot = self.make_bot()
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "PREFLIGHT"
        bot.state.continuation_stopped_by_user = True
        bot.continuation = CycleContinuation(bot)
        bot.reconciled = True
        bot._start_pair_common = Mock()
        for _ in range(5):
            bot.continuation.tick()
        bot._start_pair_common.assert_not_called()
        bot.arm_cycle()
        for _ in range(3):
            bot.continuation.tick()
        bot._start_pair_common.assert_called_once()

    def test_forming_pair_retries_pre_submission_preparation_error(self):
        bot = self.make_bot()
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "FORMING_PAIR"
        bot.state.continuation_filter_reason = "filter"
        bot.continuation = CycleContinuation(bot)
        bot._start_pair_common = Mock(side_effect=[CapitalError("quote timeout"), None])
        bot.continuation.tick()
        self.assertEqual(bot.state.continuation_stage, "FORMING_PAIR")
        self.assertFalse(bot.state.initial_submitted_directions)
        bot.continuation.tick()
        self.assertEqual(bot._start_pair_common.call_count, 2)
        for call in bot._start_pair_common.call_args_list:
            self.assertEqual(call.kwargs, {"continuation": True, "preflight_done": True})

    def test_reconciling_trigger_fill_returns_controller_to_active(self):
        bot = self.make_bot()
        bot.state.continuation_managed = True
        bot.state.continuation_stage = "RECONCILING"
        bot.state.phase = "DOUBLE_SL_RECONCILING"
        bot.continuation = CycleContinuation(bot)
        bot._tick_double_sl_reconciling = Mock(
            side_effect=lambda: setattr(bot.state, "phase", "BOTH_OPEN")
        )
        bot.continuation.tick()
        self.assertEqual(bot.state.continuation_stage, "ACTIVE")
        self.assertTrue(bot.state.continuation_managed)

    def test_market_fallback_follow_up_respects_continuation_owner(self):
        bot = self.make_bot()
        bot.state.continuation_managed = True
        bot.continuation = CycleContinuation(bot)
        bot.continuation.handle_active_scenario = Mock()
        bot._tick_cycle = Mock(side_effect=AssertionError("ordinary owner called"))
        bot._dispatch_owned_cycle()
        bot.continuation.handle_active_scenario.assert_called_once_with()
        bot._tick_cycle.assert_not_called()

    def test_continuation_recovery_uses_losses_target_and_new_spread_once(self):
        bot = self.make_bot()
        bot.state.realized_losses = D("14.71")
        bot.state.cycle_target_profit = D("0.40")
        bot.state.scenario = 8
        bot.state.cycle_attempt = 2
        bot.strategy.begin_continuation(D("4375.20"), D("4375.00"))
        self.assertEqual(bot.state.general_recovery, D("0.060"))
        bot.strategy.confirm_continuation_fills(D("4375.20"), D("4375.00"))
        self.assertEqual(bot.state.general_recovery, D("0.080"))
        self.assertEqual(bot.state.long.original_trigger_level, D("4375.20"))
        self.assertEqual(bot.state.short.original_trigger_level, D("4375.00"))
        self.assertEqual(bot.state.long.take_profit, D("4377.00"))
        self.assertEqual(bot.state.short.take_profit, D("4373.20"))
        self.assertEqual(bot.state.scenario, 8)

    def test_projected_trigger_target_uses_trigger_entry_not_opposite_stop(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"), "sell-stop")
        bot.capital.working_stop.return_value = "trigger-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "trigger-id",
        }
        before = bot.state.recovery
        bot._create_trigger(stopped)
        bot.capital.working_stop.assert_called_once_with(
            "GOLD", "SELL", D("0.1"), D("4010.00"), D("4011.00"), D("4007.30")
        )
        self.assertEqual(bot.state.recovery, before)

    def test_stop_blocks_expired_continuation_pause(self):
        bot = self.make_bot()
        bot.state.phase = "DOUBLE_SL_PAUSE"
        bot.state.continuation_pause_until = 1
        bot.command("/stop")
        with patch("trader.app.time.time", return_value=1000):
            bot.tick()
        self.assertEqual(bot.state.phase, "DOUBLE_SL_PAUSE")
        self.assertTrue(bot.state.continuation_stopped_by_user)
        bot.capital.open_position.assert_not_called()

    def test_delayed_second_initial_rejection_accounts_first_close_and_pauses(self):
        bot = self._pending_second_initial_bot()
        bot.capital.positions.return_value = []
        bot.capital.wait_confirmation.side_effect = None
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "REJECTED", "dealId": "sell-execution", "reason": "rejected",
        }
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: ([{
            "dealId": "buy-position", "source": "SL", "type": "POSITION",
            "status": "ACCEPTED", "details": {"level": 98.5, "direction": "SELL"},
        }] if not deal_id or deal_id == "buy-position" else [])
        with patch("trader.app.time.sleep"):
            bot._tick_cycle()
            bot.tick()
        self.assertTrue(bot.state.paused)
        self.assertEqual(bot.state.attempt_result_total, D("-15.0"))
        self.assertEqual(bot.state.attempt_history[-1]["opposite_order_sent"], True)
        bot.capital.open_position.assert_not_called()

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

    def test_normal_tp_waits_across_ticks_when_trigger_404_is_not_yet_resolved(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-winner"
        bot.state.short.open = False
        bot.state.short.trigger_id = "short-trigger"
        bot.state.phase = "LONG_ONLY"
        bot.capital.positions.return_value = []
        bot.capital.activity.return_value = [{
            "dealId": "long-winner", "source": "TP", "type": "POSITION",
            "status": "ACCEPTED", "details": {"level": 4011.60},
        }]
        bot.capital.delete_working_order.return_value = False
        bot._close_trigger_that_raced_with_tp = Mock(side_effect=[None, D("0.20")])

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()
        self.assertTrue(bot.state.active)
        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.short.trigger_id, "short-trigger")

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()
        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.realized_losses, D("0.20"))
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

    def test_both_closes_are_recovered_from_global_activity_index(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-1"
        bot.state.short.deal_id = "short-1"
        bot.capital.positions.return_value = []
        global_events = [
            {"dealId": "long-1", "source": "SL", "type": "POSITION",
             "status": "ACCEPTED", "details": {"level": 4611.23}},
            {"dealId": "short-1", "source": "TP", "type": "POSITION",
             "status": "ACCEPTED", "details": {"level": 4610.49}},
        ]
        bot.capital.activity.side_effect = lambda deal_id="", **_: (
            global_events if not deal_id else []
        )

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        self.assertFalse(bot.state.active)
        self.assertFalse(bot.state.manual)
        closes = {
            item["deal_id"]: item.get("close_source") for item in bot.state.deal_history
        }
        self.assertEqual(closes["long-1"], "SL")
        self.assertEqual(closes["short-1"], "TP")

    def test_confirmed_stop_waits_beyond_old_sixty_second_cutoff(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "long-1"
        bot.state.short.deal_id = "short-1"
        bot.capital.positions.return_value = []
        global_events = [
            {"dealId": "long-1", "source": "SL", "type": "POSITION",
             "status": "ACCEPTED", "details": {"level": 4611.23}},
        ]
        bot.capital.activity.side_effect = lambda deal_id="", **_: (
            global_events if not deal_id else []
        )
        bot._missing_exit_since = time.monotonic() - 120
        bot._manual = Mock()

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        bot._manual.assert_not_called()
        self.assertTrue(bot.state.active)
        self.assertEqual(bot.state.phase, "BOTH_OPEN")

    def test_early_initial_stop_is_replayed_instead_of_manual_404(self):
        bot = self.make_bot()
        bot.state.general_recovery = D("0"); bot.state.recovery_events.clear()
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
        self.assertEqual(bot.state.general_recovery, D("0.128"))
        bot.capital.update_position.assert_called_once()
        bot.capital.working_stop.assert_called_once()

    def test_second_initial_leg_stopped_before_position_sync_continues_scenario(self):
        bot = self.make_bot()
        bot.state.general_recovery = D("0"); bot.state.recovery_events.clear()
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
        self.assertEqual(bot.state.general_recovery, D("0.043"))
        self.assertEqual(bot.state.realized_losses, D("1.02"))
        self.assertEqual(bot.state.long.take_profit, D("4634.60"))
        self.assertEqual(bot.state.short.trigger_id, "trigger-1")
        bot.capital.update_position.assert_called_once()
        bot.capital.working_stop.assert_called_once()
        self.assertIn("продолжает сценарий 1", bot.telegram.send.call_args.args[0])

    def test_second_initial_stop_tolerates_transient_missing_survivor(self):
        bot = self.make_bot()
        bot.state.general_recovery = D("0"); bot.state.recovery_events.clear()
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
        self.assertEqual(bot.state.general_recovery, D("0.043"))
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
        bot.capital.wait_position.return_value = {
            "dealId": "short-2", "direction": "SELL", "level": 4009.68,
        }
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
        self.assertEqual(bot.state.general_recovery, D("0.202"))
        bot.capital.open_position.assert_called_once()

    def test_capital_stop_price_rejection_reopens_passed_trigger_with_market(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"))
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.side_effect = CapitalError(
            'Capital API 400: {"errorCode":"error.validation.stop.price"}'
        )
        bot.capital.quote.return_value = (D("4009.70"), D("4009.90"))
        bot.capital.open_position.return_value = "market-ref"
        bot.capital.wait_position.return_value = {
            "dealId": "short-2", "direction": "SELL", "level": 4009.68,
        }
        bot.capital.update_position.return_value = "update-ref"
        bot.capital.wait_confirmation.side_effect = [
            {"dealStatus": "ACCEPTED", "dealId": "short-2", "level": 4009.68},
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED"},
        ]

        bot._create_trigger(stopped)

        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.scenario, 2)
        self.assertEqual(bot.state.short.current_entry, D("4009.68"))
        bot.capital.working_stop.assert_called_once()
        bot.capital.open_position.assert_called_once()

    def test_demo_market_fallback_uses_opened_affected_deal_and_actual_position(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "00000000-6135-ee9f"
        stopped = bot.strategy.stopped("SELL", D("4412.55"), "previous-sell-stop")
        stopped.original_trigger_level = D("4411.11")
        stopped.open = False
        confirmation = {
            "dealStatus": "ACCEPTED",
            "dealReference": "o_3a2964aa",
            "dealId": "00000000-6135-eff5",
            "affectedDeals": [{"dealId": "00000000-6135-eff8", "status": "OPENED"}],
            "level": 4410.15,
            "direction": "SELL",
        }
        bot.capital.open_position.return_value = "o_3a2964aa"
        bot.capital.wait_confirmation.side_effect = [
            confirmation,
            {"dealStatus": "ACCEPTED"},
            {"dealStatus": "ACCEPTED"},
        ]
        bot.capital.wait_position.return_value = {
            "dealId": "00000000-6135-eff8",
            "dealReference": "p_00000000-6135-eff8",
            "workingOrderId": "00000000-6135-eff5",
            "direction": "SELL",
            "level": 4410.15,
        }
        bot._apply_protection = Mock(side_effect=[False, True])
        bot._closing_fill = Mock(return_value=None)
        bot._cycle_positions = Mock(side_effect=[
            {"00000000-6135-ee9f": {"dealId": "00000000-6135-ee9f"}},
            {"00000000-6135-eff8": {
                "dealId": "00000000-6135-eff8", "direction": "SELL", "level": 4410.15,
                "stopLevel": 4411.65, "profitLevel": 4405.14,
            }},
            {"00000000-6135-eff8": {
                "dealId": "00000000-6135-eff8", "direction": "SELL", "level": 4410.15,
                "stopLevel": 4411.65, "profitLevel": 4405.14,
            }},
        ])
        bot._tick_cycle = Mock()

        bot._open_passed_trigger_at_market(
            stopped, D("4406.10"), "error.validation.stop.price"
        )

        self.assertEqual(bot.state.short.deal_id, "00000000-6135-eff8")
        self.assertEqual(bot.state.short.current_entry, D("4410.15"))
        self.assertEqual(bot.state.short.original_trigger_level, D("4411.11"))
        self.assertEqual(
            bot.capital.wait_position.call_args.args[:3],
            ("00000000-6135-eff8", "o_3a2964aa", "SELL"),
        )
        bot._tick_cycle.assert_called_once()
        reports = "\n".join(call.args[0] for call in bot.telegram.send.call_args_list)
        self.assertIn("Confirmation dealId: 00000000-6135-eff5", reports)
        self.assertIn("Фактический Deal ID: 00000000-6135-eff8", reports)
        self.assertIn("Причина STOP-отказа: error.validation.stop.price", reports)

    def test_market_fallback_unknown_post_never_submits_twice(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"))
        bot.capital.open_position.side_effect = CapitalError("write timed out")

        with patch("trader.app.time.sleep"):
            bot._open_passed_trigger_at_market(stopped, D("4007.50"), "level crossed")

        bot.capital.open_position.assert_called_once()
        self.assertFalse(bot.state.manual)
        self.assertEqual(stopped.pending_market_kind, "FALLBACK")
        self.assertTrue(stopped.pending_market_unknown_post)

    def test_outer_trigger_loop_does_not_repeat_market_with_pending_confirmation(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"))
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.side_effect = CapitalError(
            'Capital API 400: {"errorCode":"error.validation.stop.price"}'
        )
        bot.capital.quote.return_value = (D("4009.70"), D("4009.90"))
        bot.capital.open_position.return_value = "market-ref"
        bot.capital.wait_confirmation.side_effect = CapitalError("confirmation unavailable")

        bot._create_trigger(stopped)
        bot._create_trigger(stopped)

        bot.capital.open_position.assert_called_once()
        bot.capital.working_stop.assert_called_once()
        self.assertEqual(stopped.pending_market_reference, "market-ref")
        self.assertFalse(bot.state.manual)

    def test_outer_trigger_loop_does_not_repeat_market_when_reconciliation_fails(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"))
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.side_effect = CapitalError(
            'Capital API 400: {"errorCode":"error.validation.stop.price"}'
        )
        bot.capital.quote.return_value = (D("4009.70"), D("4009.90"))
        bot.capital.open_position.side_effect = CapitalError("write timed out")
        bot.capital.positions.side_effect = [
            [], CapitalError("positions unavailable"),
            CapitalError("positions still unavailable"),
        ]

        with patch("trader.app.time.sleep"):
            bot._create_trigger(stopped)
            bot._create_trigger(stopped)

        bot.capital.open_position.assert_called_once()
        self.assertEqual(stopped.pending_market_kind, "FALLBACK")
        self.assertTrue(stopped.pending_market_unknown_post)
        self.assertTrue(bot.state.active)
        self.assertFalse(bot.state.manual)

    def test_initial_unknown_market_survives_tick_and_state_reload(self):
        bot = self.make_bot()
        bot.capital.open_position.side_effect = CapitalError("write timed out")
        bot.capital.positions.side_effect = [
            [], CapitalError("positions unavailable"),
            CapitalError("positions still unavailable"),
        ]

        with patch("trader.app.time.sleep"):
            error = bot._open_initial_leg(bot.state.long)
            bot._tick_cycle()

        self.assertIn("повторное открытие заблокировано", error)
        bot.capital.open_position.assert_called_once()
        self.assertEqual(bot.state.long.pending_market_kind, "INITIAL")
        restored = CycleState.load(bot.cfg.state_file)
        self.assertEqual(restored.long.pending_market_kind, "INITIAL")
        self.assertTrue(restored.long.pending_market_unknown_post)
        self.assertEqual(restored.long.pending_market_preexisting_ids, [])

    def _pending_market_with_opposite_exit(self, source):
        bot = self.make_bot()
        bot.state.long.deal_id = "buy-old"
        stopped = bot.strategy.stopped("SELL", D("4011.10"), "old-sell-stop")
        stopped.pending_market_kind = "FALLBACK"
        stopped.pending_market_reference = "market-ref"
        stopped.pending_market_reason = "error.validation.stop.price"
        stopped.pending_market_preexisting_ids = ["buy-old"]
        position = {
            "dealId": "sell-new", "dealReference": "p-sell-new",
            "workingOrderId": "execution-id", "direction": "SELL", "level": 4009.68,
            "stopLevel": 4011.0, "profitLevel": 4007.5,
        }
        bot.capital.positions.return_value = [{
            "position": position, "market": {"epic": bot.cfg.epic},
        }]
        bot.capital.wait_position.return_value = position
        bot.capital.wait_confirmation.side_effect = lambda reference: {
            "market-ref": {
                "dealStatus": "ACCEPTED", "dealId": "execution-id",
                "affectedDeals": [{"dealId": "sell-new", "status": "OPENED"}],
                "level": 4009.68,
            },
            "close-sell": {"dealStatus": "ACCEPTED", "dealId": "sell-new",
                           "level": 4009.88},
            "trigger-ref": {"dealStatus": "ACCEPTED", "dealId": "buy-trigger"},
        }.get(reference, {"dealStatus": "ACCEPTED"})
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: [{
            "dealId": "buy-old", "source": source, "type": "POSITION",
            "status": "ACCEPTED", "details": {
                "level": 4009.25 if source == "SL" else 4011.70,
            },
        }] if not deal_id or deal_id == "buy-old" else []
        bot.capital.update_position.side_effect = lambda deal_id, stop, target: (
            (_ for _ in ()).throw(CapitalError(
                'Capital API 404: {"errorCode":"error.not-found.dealId"}'
            )) if deal_id == "buy-old" else "update-ref"
        )
        bot.capital.quote.return_value = D("4010.0"), D("4010.2")
        bot.capital.working_orders.return_value = []
        bot.capital.working_stop.return_value = "trigger-ref"
        bot.capital.close_position.return_value = "close-sell"
        return bot, stopped

    def test_pending_market_is_resolved_before_opposite_sl(self):
        bot, stopped = self._pending_market_with_opposite_exit("SL")

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        bot.capital.open_position.assert_not_called()
        self.assertEqual(stopped.deal_id, "sell-new")
        self.assertEqual(stopped.pending_market_kind, "")
        self.assertEqual(bot.state.scenario, 2)
        self.assertEqual(bot.state.phase, "SHORT_ONLY")
        self.assertEqual(
            len([key for key in bot.state.processed_events if key.startswith("stop:buy-old")]), 1
        )
        self.assertEqual(bot.state.long.trigger_id, "buy-trigger")

    def test_pending_market_is_resolved_and_closed_before_opposite_tp_completion(self):
        bot, stopped = self._pending_market_with_opposite_exit("TP")

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        bot.capital.open_position.assert_not_called()
        bot.capital.close_position.assert_called_once_with("sell-new")
        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.phase, "FILTER")
        self.assertFalse(stopped.open)
        self.assertEqual(stopped.pending_market_kind, "")
        self.assertEqual(bot.state.realized_losses, D("1.30"))

    def test_accepted_market_without_level_is_resolved_without_second_post(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"))
        accepted_without_level = {
            "dealStatus": "ACCEPTED", "dealId": "execution-id",
            "affectedDeals": [{"dealId": "position-id", "status": "OPENED"}],
        }
        bot.capital.open_position.return_value = "market-ref"
        bot.capital.wait_confirmation.return_value = accepted_without_level
        bot.capital.wait_position.side_effect = [
            CapitalError("position delayed"),
            {"dealId": "position-id", "direction": "SELL", "level": 4009.68},
        ]
        bot.capital.activity.return_value = []
        bot._apply_protection = Mock(return_value=True)

        with patch("trader.app.time.sleep"):
            bot._open_passed_trigger_at_market(stopped, D("4007.50"), "level crossed")
            self.assertEqual(stopped.pending_market_reference, "market-ref")
            bot._open_passed_trigger_at_market(stopped, D("4007.50"), "level crossed")

        bot.capital.open_position.assert_called_once()
        self.assertEqual(bot.state.scenario, 2)
        self.assertEqual(stopped.deal_id, "position-id")
        self.assertEqual(stopped.current_entry, D("4009.68"))
        self.assertEqual(stopped.pending_market_reference, "")

    def test_trigger_404_stays_unresolved_until_delayed_execution_appears(self):
        bot = self.make_bot()
        stopped = bot.strategy.stopped("SELL", D("4011.10"))
        stopped.trigger_id = "trigger-sell"
        bot.capital.activity.return_value = []
        bot.capital.positions.return_value = []

        with patch("trader.app.time.sleep"):
            self.assertIsNone(bot._close_trigger_that_raced_with_tp(stopped))

        opened = {
            "dateUTC": "2026-09-08T13:01:00",
            "dealId": "late-sell", "source": "USER", "type": "POSITION",
            "status": "ACCEPTED", "details": {
                "workingOrderId": "trigger-sell", "direction": "SELL", "level": 4010.0,
            },
        }
        executed = {
            "dateUTC": "2026-09-08T13:00:59",
            "dealId": "trigger-sell", "source": "USER", "type": "WORKING_ORDER",
            "status": "EXECUTED", "details": {"direction": "SELL"},
        }
        bot.capital.activity.return_value = [executed, opened]
        bot.capital.positions.return_value = [{
            "position": {
                "dealId": "late-sell", "workingOrderId": "trigger-sell",
                "direction": "SELL", "level": 4010.0,
            },
            "market": {"epic": bot.cfg.epic},
        }]
        bot.capital.close_position.return_value = "close-late"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "late-sell", "level": 4010.2,
        }

        with patch("trader.app.time.sleep"):
            loss = bot._close_trigger_that_raced_with_tp(stopped)

        self.assertEqual(loss, D("0.2"))
        bot.capital.close_position.assert_called_once_with("late-sell")

    def test_market_position_resolves_from_activity_if_it_closed_before_positions_sync(self):
        bot = self.make_bot()
        bot.capital.wait_position.side_effect = CapitalError("not visible")
        bot.capital.activity.return_value = [{
            "dateUTC": "2026-09-08T13:00:20.414",
            "dealId": "00000000-6135-eff8",
            "source": "USER",
            "type": "POSITION",
            "status": "ACCEPTED",
            "details": {
                "dealReference": "p_00000000-6135-eff8",
                "workingOrderId": "00000000-6135-eff5",
                "direction": "SELL",
                "level": 4410.15,
            },
        }]
        confirmation = {
            "dealId": "00000000-6135-eff5",
            "affectedDeals": [{"dealId": "00000000-6135-eff8", "status": "OPENED"}],
        }

        position = bot._resolve_market_position(
            confirmation, "o_3a2964aa", "SELL", {"old-sell"}
        )

        self.assertEqual(position["dealId"], "00000000-6135-eff8")
        self.assertEqual(position["workingOrderId"], "00000000-6135-eff5")
        self.assertEqual(position["level"], D("4410.15"))

    def test_demo_both_absent_closes_cycle_using_actual_market_position_id(self):
        bot = self.make_bot()
        bot.state.long.deal_id = "00000000-6135-ee9f"
        bot.state.short.deal_id = "00000000-6135-eff8"
        bot.state.long.current_entry = D("4411.91")
        bot.state.long.stop = D("4410.41")
        bot.state.short.current_entry = D("4410.15")
        bot.state.short.stop = D("4411.65")
        bot.state.short.take_profit = D("4405.14")
        bot.state.scenario = 3
        bot.state.recovery = D("5.01")
        bot.capital.positions.return_value = []
        bot.capital.activity.side_effect = lambda deal_id="", last_period=86400: [
            {
                "dealId": "00000000-6135-ee9f", "source": "SL", "type": "POSITION",
                "status": "ACCEPTED", "details": {"level": 4410.34, "direction": "SELL"},
            },
            {
                "dealId": "00000000-6135-eff8", "source": "TP", "type": "POSITION",
                "status": "ACCEPTED", "details": {"level": 4406.09, "direction": "BUY"},
            },
        ] if not deal_id else [item for item in [
            {
                "dealId": "00000000-6135-ee9f", "source": "SL", "type": "POSITION",
                "status": "ACCEPTED", "details": {"level": 4410.34, "direction": "SELL"},
            },
            {
                "dealId": "00000000-6135-eff8", "source": "TP", "type": "POSITION",
                "status": "ACCEPTED", "details": {"level": 4406.09, "direction": "BUY"},
            },
        ] if item["dealId"] == deal_id]

        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.gross_take_profit, D("4.06"))
        self.assertEqual(bot.state.realized_losses, D("1.57"))
        self.assertEqual(bot.state.net_cycle_result, D("2.49"))

    def test_demo_market_fallback_sl_then_tp_end_to_end(self):
        """Replay historical fills; fixed protection could change a future DEMO outcome."""
        bot = self.make_bot()
        bot.cfg = Settings(
            dry_run=False, api_key="key", identifier="id", password="password",
            stop_distance=D("1.5"), target_profit=D("0.4"), size=D("10"),
        )
        bot.state = CycleState()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4411.91"), D("4411.11"))
        bot.state.long.deal_id = "buy-ee9f"
        bot.state.short.deal_id = "old-sell"
        bot.state.scenario = 2
        stopped = bot.strategy.stopped("SELL", D("4412.61"), "old-sell-stop")
        stopped.original_trigger_level = D("4411.11")
        bot.state.recovery = D("3.44")
        # The log already contains two SELL losses before the final MARKET SELL:
        # 15.10 USD + 15.20 USD at size 10 = 3.03 price points.
        bot.state.realized_losses = D("3.03")

        class DemoBroker:
            def __init__(self):
                self.tp_visible = False
                self.sell_stop = D("4412.61")
                self.sell_tp = D("4406.10")
                self.update_calls = []
                self.open_calls = 0
                self.deleted = []
                self.position_reads = 0

            def positions(self):
                self.position_reads += 1
                if self.position_reads == 1:
                    return [{
                        "position": {
                            "dealId": "buy-ee9f", "direction": "BUY", "level": 4411.91,
                        },
                        "market": {"epic": "GOLD"},
                    }]
                if self.tp_visible:
                    return []
                return [{
                    "position": {
                        "dealId": "sell-eff8", "dealReference": "p-sell-eff8",
                        "workingOrderId": "execution-eff5", "direction": "SELL",
                        "level": 4410.15, "stopLevel": self.sell_stop,
                        "profitLevel": self.sell_tp,
                    },
                    "market": {"epic": "GOLD"},
                }]

            def open_position(self, *args, **kwargs):
                self.open_calls += 1
                return "market-ref"

            def wait_confirmation(self, reference):
                if reference == "market-ref":
                    return {
                        "dealStatus": "ACCEPTED", "dealId": "execution-eff5",
                        "affectedDeals": [{"dealId": "sell-eff8", "status": "OPENED"}],
                        "level": 4410.15, "direction": "SELL",
                    }
                if reference == "trigger-ref":
                    return {"dealStatus": "ACCEPTED", "dealId": "buy-trigger"}
                return {"dealStatus": "ACCEPTED"}

            def wait_position(self, deal_id, reference, direction, **kwargs):
                if deal_id != "sell-eff8":
                    raise AssertionError(f"wrong position id: {deal_id}")
                return self.positions()[0]["position"]

            def update_position(self, deal_id, stop, target):
                self.update_calls.append((deal_id, stop, target))
                if deal_id == "buy-ee9f":
                    raise CapitalError('Capital API 404: {"errorCode":"error.not-found.dealId"}')
                if deal_id != "sell-eff8":
                    raise AssertionError(f"wrong protection id: {deal_id}")
                self.sell_stop, self.sell_tp = stop, target
                return f"update-{len(self.update_calls)}"

            def activity(self, deal_id="", last_period=86400):
                events = [{
                    "dealId": "buy-ee9f", "source": "SL", "type": "POSITION",
                    "status": "ACCEPTED", "details": {"level": 4410.34},
                }]
                if self.tp_visible:
                    events.append({
                        "dealId": "sell-eff8", "source": "TP", "type": "POSITION",
                        "status": "ACCEPTED", "details": {"level": 4406.09},
                    })
                return [event for event in events if not deal_id or event["dealId"] == deal_id]

            def working_orders(self):
                return []

            def quote(self, epic):
                return D("4410.60"), D("4411.10")

            def working_stop(self, *args, **kwargs):
                return "trigger-ref"

            def delete_working_order(self, deal_id):
                self.deleted.append(deal_id)
                return True

        broker = DemoBroker()
        bot.capital = broker

        with patch("trader.app.time.sleep"):
            bot._open_passed_trigger_at_market(
                stopped, D("4406.10"), "error.validation.stop.price"
            )

        self.assertEqual(bot.state.short.deal_id, "sell-eff8")
        self.assertEqual(bot.state.scenario, 3)
        self.assertEqual(bot.state.phase, "SHORT_ONLY")
        self.assertEqual(bot.state.realized_losses, D("4.60"))
        self.assertEqual(
            len([key for key in bot.state.processed_events if key.startswith("stop:buy-ee9f")]), 1
        )
        self.assertTrue(any(call[0] == "sell-eff8" for call in broker.update_calls))
        self.assertFalse(any(call[0] == "execution-eff5" for call in broker.update_calls))
        self.assertEqual(broker.open_calls, 1)

        broker.tp_visible = True
        with patch("trader.app.time.sleep"):
            bot._tick_cycle()

        self.assertFalse(bot.state.active)
        self.assertEqual(bot.state.gross_take_profit, D("4.06"))
        self.assertEqual(bot.state.realized_losses, D("4.60"))
        self.assertEqual(bot.state.net_cycle_result, D("-0.54"))
        self.assertEqual(bot.state.net_cycle_result * bot.cfg.size, D("-5.40"))
        self.assertEqual(broker.deleted, ["buy-trigger"])
        reports = "\n".join(call.args[0] for call in bot.telegram.send.call_args_list)
        self.assertIn("Фактический Deal ID: sell-eff8", reports)
        self.assertIn("Итог завершённого цикла", reports)

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
        self.assertEqual(bot.state.general_recovery, D("0.070"))
        self.assertEqual(bot.state.short.trigger_id, "trigger-1")


    def test_two_confirmed_stops_pause_attempt_once(self):
        bot = self.make_bot()
        object.__setattr__(bot.cfg, "size", D("10"))
        bot.state.active_attempt_id = bot.state.attempt_counter = 224
        bot.state.long.deal_id = "buy-224"
        bot.state.short.deal_id = "sell-224"
        bot.state.long.current_entry = D("4372.74")
        bot.state.short.current_entry = D("4371.16")
        bot.state.long.size = bot.state.short.size = D("10")
        with tempfile.TemporaryDirectory() as directory:
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            object.__setattr__(
                bot.cfg, "diagnostic_log_file", str(Path(directory) / "diagnostics.log")
            )
            with patch("trader.app.end_diagnostic_cycle"):
                bot._begin_double_sl_pause([
                    (bot.state.long, D("4371.22")),
                    (bot.state.short, D("4373.53")),
                ])
                total = bot.state.attempt_result_total
                bot._begin_double_sl_pause([
                    (bot.state.long, D("4371.22")),
                    (bot.state.short, D("4373.53")),
                ])
        self.assertEqual(bot.state.phase, "DOUBLE_SL_PAUSE")
        self.assertTrue(bot.state.active)
        self.assertEqual(total, D("-38.90"))
        self.assertEqual(bot.state.attempt_result_total, total)
        self.assertEqual(len(bot.state.attempt_history), 1)


class DiagnosticHistoryTest(unittest.TestCase):
    def _write(self, handler, text):
        handler.emit(__import__("logging").LogRecord(
            "test", __import__("logging").INFO, __file__, 1, text, (), None
        ))

    def test_more_than_twenty_cycles_are_not_pruned_without_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = CycleFileHandler(str(Path(directory) / "bot_diagnostics.log"))
            for cycle in range(1, 23):
                handler.begin_cycle(cycle, cycle - 1)
                self._write(handler, f"cycle {cycle}")
                handler.end_cycle(cycle + 1)
            handler.begin_cycle(23, 22)
            names = [item["name"] for item in handler._index["segments"]]
            handler.close()
        self.assertTrue(any("cycle-000000002.log" in name for name in names))
        self.assertTrue(any("cycle-000000003.log" in name for name in names))
        self.assertTrue(any("cycle-000000023.log" in name for name in names))

    def test_long_cycle_is_not_truncated_and_snapshot_can_use_many_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = CycleFileHandler(str(Path(directory) / "bot_diagnostics.log"))
            handler.begin_cycle(7)
            payload = "x" * 700
            self._write(handler, payload)
            parts = handler.snapshot(max_part_bytes=400)
            content = b"".join(Path(path).read_bytes() for path in parts)
            handler.close()
            for path in parts:
                Path(path).unlink(missing_ok=True)
        self.assertGreater(len(parts), 2)
        self.assertIn(payload.encode(), content)

    def test_restart_inside_cycle_retains_prior_segment_and_cycle_number(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bot_diagnostics.log")
            first = CycleFileHandler(path)
            first.begin_cycle(11)
            self._write(first, "before restart")
            first.close()
            second = CycleFileHandler(path)
            second.begin_cycle(11)
            self._write(second, "after restart")
            parts = second.snapshot(max_part_bytes=10000)
            content = Path(parts[0]).read_text(encoding="utf-8")
            second.close()
            Path(parts[0]).unlink(missing_ok=True)
        self.assertLess(content.index("before restart"), content.index("after restart"))
        self.assertGreaterEqual(content.count("ТОРГОВАЯ ПОПЫТКА 11"), 2)

    def test_runs_without_sendlog_and_stop_marker_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bot_diagnostics.log")
            first = CycleFileHandler(path)
            self._write(first, "run one without sendlog")
            first.close()
            second = CycleFileHandler(path)
            self._write(second, "/stop in same process")
            parts = second.snapshot(max_part_bytes=10000)
            content = b"".join(Path(p).read_bytes() for p in parts).decode()
            second.close()
        self.assertIn("run one without sendlog", content)
        self.assertIn("/stop in same process", content)
        self.assertIn("ЗАПУСК ПРОЦЕССА №1", content)
        self.assertIn("ЗАПУСК ПРОЦЕССА №2", content)

    def test_repeat_sendlog_contains_full_current_run(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = CycleFileHandler(str(Path(directory) / "bot_diagnostics.log"))
            self._write(handler, "begin current")
            first = handler.snapshot(max_part_bytes=10000)
            first_id = handler.pending_snapshots()[-1]["id"]
            for part in handler.pending_snapshots()[-1]["parts"]:
                handler.acknowledge(first_id, part["number"], "delivered")
            self._write(handler, "tail current")
            second = handler.snapshot(max_part_bytes=10000)
            content = b"".join(Path(p).read_bytes() for p in second).decode()
            handler.close()
        self.assertIn("begin current", content)
        self.assertIn("tail current", content)

    def test_partial_failure_keeps_sources_and_delivered_parts(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = CycleFileHandler(str(Path(directory) / "bot_diagnostics.log"))
            self._write(handler, "important diagnostics" * 30)
            handler.snapshot(max_part_bytes=200)
            snapshot = handler.pending_snapshots()[-1]
            handler.acknowledge(snapshot["id"], 1, "delivered")
            handler.acknowledge(snapshot["id"], 2, "failed")
            persisted = json.loads(handler.index_path.read_text())
            source_exists = all((handler.history_dir / r["name"]).exists()
                                for r in snapshot["ranges"])
            handler.close()
        saved = next(item for item in persisted["snapshots"] if item["id"] == snapshot["id"])
        self.assertEqual(saved["parts"][0]["status"], "delivered")
        self.assertEqual(saved["parts"][1]["status"], "failed")
        self.assertTrue(source_exists)

    def test_acknowledged_boundary_preserves_tail_on_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "bot_diagnostics.log")
            first = CycleFileHandler(path)
            self._write(first, "sent boundary")
            first.snapshot(max_part_bytes=10000)
            snap = first.pending_snapshots()[-1]
            for part in snap["parts"]:
                first.acknowledge(snap["id"], part["number"], "delivered")
            self._write(first, "unsent tail")
            first.close()
            second = CycleFileHandler(path)
            self._write(second, "new run")
            parts = second.snapshot(max_part_bytes=10000)
            content = b"".join(Path(p).read_bytes() for p in parts).decode()
            second.close()
        self.assertNotIn("sent boundary", content)
        self.assertIn("unsent tail", content)
        self.assertIn("new run", content)

    def test_legacy_log_migrates_as_unsent(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "bot_diagnostics.log"
            legacy.write_text("legacy unsent diagnostics", encoding="utf-8")
            handler = CycleFileHandler(str(legacy))
            parts = handler.snapshot(max_part_bytes=10000)
            content = b"".join(Path(p).read_bytes() for p in parts).decode()
            legacy_items = [item for item in handler._index["segments"]
                            if item.get("run_id") == "legacy"]
            handler.close()
        self.assertIn("legacy unsent diagnostics", content)
        self.assertTrue(legacy_items)
        self.assertEqual(legacy_items[0]["delivered_bytes"], 0)

    def test_successful_new_snapshot_supersedes_failed_covered_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = CycleFileHandler(str(Path(directory) / "bot_diagnostics.log"))
            self._write(handler, "same covered data")
            handler.snapshot(max_part_bytes=10000)
            old = handler.pending_snapshots()[-1]
            handler.acknowledge(old["id"], 1, "failed")
            handler.snapshot(max_part_bytes=10000)
            new = handler.pending_snapshots()[-1]
            for part in new["parts"]:
                handler.acknowledge(new["id"], part["number"], "delivered")
            old_saved = next(item for item in handler._index["snapshots"]
                             if item["id"] == old["id"])
            handler.close()
        self.assertEqual(old_saved["status"], "superseded")
        self.assertFalse(any(Path(part["path"]).exists() for part in old_saved["parts"]))

    def test_interrupted_legacy_move_is_recovered_without_duplicate_index_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = Path(directory) / "bot_diagnostics.log"
            history = Path(directory) / "bot_diagnostics.log.history"
            history.mkdir()
            orphan = history / "legacy-bot_diagnostics.log"
            orphan.write_text("moved before index save", encoding="utf-8")
            first = CycleFileHandler(str(legacy)); first.close()
            second = CycleFileHandler(str(legacy))
            records = [item for item in second._index["segments"] if item["name"] == orphan.name]
            parts = second.snapshot(max_part_bytes=10000)
            content = b"".join(Path(path).read_bytes() for path in parts).decode()
            second.close()
        self.assertEqual(len(records), 1)
        self.assertIn("moved before index save", content)

    def test_snapshot_streams_large_source_in_bounded_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = CycleFileHandler(str(Path(directory) / "bot_diagnostics.log"))
            self._write(handler, "x" * (1024 * 1024))
            original_open = Path.open
            reads = []

            class Reader:
                def __init__(self, wrapped): self.wrapped = wrapped
                def __enter__(self): return self
                def __exit__(self, *args): self.wrapped.close()
                def seek(self, *args): return self.wrapped.seek(*args)
                def read(self, size=-1):
                    reads.append(size)
                    return self.wrapped.read(size)

            def monitored(path, mode="r", *args, **kwargs):
                opened = original_open(path, mode, *args, **kwargs)
                return Reader(opened) if mode == "rb" and path.suffix == ".log" else opened

            with patch.object(Path, "open", monitored):
                parts = handler.snapshot(max_part_bytes=300000)
            handler.close()
        self.assertGreater(len(parts), 2)
        self.assertTrue(reads)
        self.assertTrue(all(0 < size <= 256 * 1024 for size in reads))


class CapitalClientTest(unittest.TestCase):
    def test_activity_uses_explicit_one_day_utc_range_without_last_period(self):
        client = CapitalClient(Settings(api_key="key", identifier="id", password="password"))
        client.request = Mock(return_value={"activities": []})
        client.activity(
            "deal-old", from_date="2026-09-14T12:00:00", to_date="2026-09-15T12:00:00"
        )
        params = client.request.call_args.kwargs["params"]
        self.assertEqual(params["from"], "2026-09-14T12:00:00")
        self.assertEqual(params["to"], "2026-09-15T12:00:00")
        self.assertEqual(params["dealId"], "deal-old")
        self.assertNotIn("lastPeriod", params)

    def test_activity_rejects_last_period_over_documented_limit(self):
        client = CapitalClient(Settings(api_key="key", identifier="id", password="password"))
        with self.assertRaises(ValueError):
            client.activity(last_period=86401)
        with self.assertRaises(ValueError):
            client.activity(
                from_date="2026-09-14T00:00:00", to_date="2026-09-15T00:00:01"
            )

    def test_repeated_activity_responses_log_unique_events_once(self):
        client = CapitalClient(Settings(api_key="key", identifier="id", password="password"))
        response = Mock(status_code=200, content=b"yes", text="")
        event = {"dealId": "sell-224", "source": "SL", "details": {"level": 4373.53}}
        response.json.return_value = {"activities": [event]}
        with self.assertLogs("trader.capital", level="INFO") as captured:
            client._log_response(1, response, time.monotonic(), path="/history/activity")
            client._log_response(2, response, time.monotonic(), path="/history/activity")
        joined = "\n".join(captured.output)
        self.assertEqual(joined.count('"dealId":"sell-224"'), 1)
        self.assertIn('"repeated_activities_suppressed":1', joined)

    def test_parallel_session_refresh_is_performed_once(self):
        client = CapitalClient(Settings(api_key="key", identifier="id", password="password"))
        response = Mock(ok=True, status_code=200, content=b"{}")
        response.json.return_value = {}
        response.headers = {"CST": "cst", "X-SECURITY-TOKEN": "security"}
        client.http.post = Mock(return_value=response)
        barrier = __import__("threading").Barrier(3)

        def refresh():
            barrier.wait()
            client.login()

        threads = [__import__("threading").Thread(target=refresh) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(1)

        self.assertEqual(client.http.post.call_count, 1)
        self.assertEqual(client.session_generation, 1)

    def test_working_order_cancellation_waits_for_accepted_confirmation(self):
        client = CapitalClient.__new__(CapitalClient)
        client.request = Mock(return_value={"dealReference": "cancel-ref"})
        client.wait_confirmation = Mock(return_value={"dealStatus": "ACCEPTED"})

        self.assertTrue(client.delete_working_order("trigger-1"))

        client.wait_confirmation.assert_called_once_with("cancel-ref")

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
    def test_long_report_is_split_into_ordered_numbered_messages(self):
        telegram = Telegram("token", "123")
        with patch.object(telegram, "send", return_value=True) as send:
            self.assertTrue(telegram.send_report("A\n" * 30, limit=20))
        parts = [call.args[0] for call in send.call_args_list]
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].startswith("Часть 1/"))
        self.assertTrue(parts[-1].startswith(f"Часть {len(parts)}/{len(parts)}"))

    def test_close_wait_retries_deal_and_global_activity(self):
        bot = Bot.__new__(Bot)
        bot.state = CycleState()
        leg = Leg("SELL", D("4010"), D("4010"), deal_id="sell-1", open=True)
        bot.capital = Mock()
        bot.capital.activity.side_effect = [
            [], [], [], [],
            [{
                "dateUTC": "2026-08-27T12:00:01",
                "dealId": "sell-1", "source": "TP", "type": "POSITION",
                "status": "ACCEPTED", "details": {"level": 4008.7},
            }],
        ]

        with patch("trader.app.time.sleep"):
            fill = bot._wait_closing_fill(leg, "TP", attempts=4, delay=0)

        self.assertEqual(fill, D("4008.7"))
        self.assertEqual(bot.capital.activity.call_args_list[-1].args, ())
        self.assertEqual(bot.state.deal_history[-1]["close_source"], "TP")

    def test_position_protection_readback_404_is_deferred(self):
        bot = Bot.__new__(Bot)
        bot.capital = Mock()
        bot.capital.position.side_effect = CapitalError(
            'Capital API 404: {"errorCode":"error.not-found.dealId"}'
        )

        actual = bot._wait_position_protection("gone", D("9"), D("11"))

        self.assertEqual(actual, (None, None))

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

    def test_survivor_tp_trigger_race_is_resolved_by_api_without_manual_mode(self):
        bot = Bot.__new__(Bot)
        bot.state = CycleState()
        bot.cfg = Settings()
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.strategy.begin(D("4550.20"), D("4549.52"))
        stopped = bot.strategy.stopped("BUY", D("4548.64"), "initial-buy-stop")
        stopped.trigger_id = "trigger-buy"
        survivor = bot.state.short
        bot.capital = Mock()
        bot.capital.activity.return_value = [
            {"dateUTC": "2026-08-28T15:39:18", "dealId": "reopened-buy",
             "source": "USER", "type": "POSITION", "status": "ACCEPTED",
             "details": {"workingOrderId": "trigger-buy", "direction": "BUY",
                         "level": 4550.26}},
            {"dateUTC": "2026-08-28T15:39:19", "dealId": "reopened-buy",
             "source": "SL", "type": "POSITION", "status": "ACCEPTED",
             "details": {"level": 4548.64}},
            {"dateUTC": "2026-08-28T15:39:38", "dealId": survivor.deal_id,
             "source": "TP", "type": "POSITION", "status": "ACCEPTED",
             "details": {"level": 4547.56}},
        ]
        bot._close_trigger_that_raced_with_tp = Mock(return_value=D("1.62"))
        bot._complete_cycle = Mock()
        bot.telegram = Mock()

        with tempfile.NamedTemporaryFile() as state_file:
            bot.cfg = Settings(state_file=state_file.name)
            handled = bot._recover_trigger_round_trip_from_activity(survivor, stopped)

        self.assertTrue(handled)
        self.assertFalse(bot.state.manual)
        self.assertEqual(bot.state.realized_losses, D("3.18"))
        bot._close_trigger_that_raced_with_tp.assert_called_once_with(stopped)
        bot._complete_cycle.assert_called_once_with("SELL", D("4547.56"))
        self.assertIn("проверены через Capital.com API", bot.telegram.send.call_args.args[0])

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
        self.assertTrue(is_crossed_level_rejection("error.validation.stop.price"))
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
        bot.state.long.open = False
        bot.capital.working_stop.return_value = "manual-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "manual-order",
        }
        with self.assertRaisesRegex(RuntimeError, "pending D"):
            bot.command("/settrigger long 4020.50")
        bot.capital.working_stop.assert_not_called()

    def test_manual_trigger_is_rejected_after_completed_cycle(self):
        bot = EntryRetryTest().make_bot()
        bot.state.active = False
        bot.state.long.open = False
        with self.assertRaisesRegex(RuntimeError, "активного цикла"):
            bot.command("/settrigger long 4020.50")
        bot.capital.working_stop.assert_not_called()

    def test_recover_rebinds_permanent_ids_and_reapplies_protection(self):
        bot = EntryRetryTest().make_bot()
        bot.state.manual = True
        bot.state.paused = True
        bot.state.long.deal_reference = "accepted-buy-reference"
        bot.state.short.deal_reference = "accepted-sell-reference"
        bot.capital.positions.return_value = [
            {"position": {"dealId": "permanent-buy", "dealReference": "accepted-buy-reference",
                          "direction": "BUY", "level": 4010.30},
             "market": {"epic": "GOLD"}},
            {"position": {"dealId": "permanent-sell", "dealReference": "accepted-sell-reference",
                          "direction": "SELL", "level": 4010.00},
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

    def test_recover_refuses_unrelated_positions_with_matching_directions(self):
        bot = EntryRetryTest().make_bot()
        bot.state.manual = True
        bot.capital.positions.return_value = [
            {"position": {"dealId": "foreign-buy", "direction": "BUY", "level": 4010},
             "market": {"epic": "GOLD"}},
            {"position": {"dealId": "foreign-sell", "direction": "SELL", "level": 4009},
             "market": {"epic": "GOLD"}},
        ]
        with self.assertRaisesRegex(RuntimeError, "не связана"):
            bot.command("/recover")
        bot.capital.update_position.assert_not_called()

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


class StreamingQuoteTest(unittest.TestCase):
    def test_directional_levels_wake_only_when_reached(self):
        stream = QuoteStream("GOLD", lambda: ("cst", "token"))
        stream.watch([
            PriceWatch("SL", "BUY", D("99")),
            PriceWatch("TP", "BUY", D("105")),
            PriceWatch("TRIGGER", "SELL", D("98")),
        ])

        stream._message(json.dumps({
            "destination": "quote",
            "payload": {"epic": "GOLD", "bid": 100, "ofr": 100.2, "timestamp": 1},
        }))
        self.assertFalse(stream.wait(0))

        stream._message(json.dumps({
            "destination": "quote",
            "payload": {"epic": "GOLD", "bid": 97.9, "ofr": 98.1, "timestamp": 2},
        }))
        self.assertTrue(stream.wait(0))
        self.assertEqual(stream.latest().bid, D("97.9"))

    def test_quotes_never_wake_for_other_epics_or_malformed_messages(self):
        stream = QuoteStream("GOLD", lambda: ("cst", "token"))
        stream.watch([PriceWatch("TP", "SELL", D("100"))])
        stream._message("not-json")
        stream._message(json.dumps({
            "destination": "quote",
            "payload": {"epic": "SILVER", "bid": 90, "ofr": 90.2, "timestamp": 1},
        }))
        self.assertFalse(stream.wait(0))
        self.assertIsNone(stream.latest())

    def test_out_of_order_quote_cannot_generate_a_false_crossing_after_reconnect(self):
        stream = QuoteStream("GOLD", lambda: ("cst", "token"))
        stream.watch([PriceWatch("SL", "BUY", D("99"))])
        stream._message(json.dumps({
            "destination": "quote", "status": "OK",
            "payload": {"epic": "GOLD", "bid": 100, "ofr": 100.2, "timestamp": 20},
        }))
        stream._message(json.dumps({
            "destination": "quote", "status": "OK",
            "payload": {"epic": "GOLD", "bid": 98, "ofr": 98.2, "timestamp": 10},
        }))

        self.assertFalse(stream.wait(0))
        self.assertEqual(stream.latest().bid, D("100"))

    def test_rejected_stream_message_forces_reconnect(self):
        stream = QuoteStream("GOLD", lambda: ("cst", "token"))
        with self.assertRaises(ConnectionError):
            stream._message(json.dumps({
                "destination": "marketData.subscribe", "status": "ERROR", "payload": {},
            }))

    def test_disconnect_reconnects_and_resubscribes_with_current_tokens(self):
        connections = []
        tokens = [("cst-1", "security-1", 1), ("cst-2", "security-2", 2)]
        token_calls = 0

        def current_tokens():
            nonlocal token_calls
            value = tokens[min(token_calls, 1)]
            token_calls += 1
            return value

        class Connection:
            def __init__(self, disconnect):
                self.disconnect = disconnect
                self.sent = []

            def send(self, message):
                self.sent.append(json.loads(message))

            def settimeout(self, timeout):
                self.timeout = timeout

            def recv(self):
                if self.disconnect:
                    self.disconnect = False
                    raise ConnectionError("network lost")
                subscription = self.sent[0]
                return json.dumps({
                    "status": "OK", "destination": "marketData.subscribe",
                    "correlationId": subscription["correlationId"],
                    "payload": {"subscriptions": {"GOLD": "PROCESSED"}},
                })

            def close(self):
                pass

        def factory(url, timeout):
            connection = Connection(disconnect=not connections)
            connections.append(connection)
            return connection

        stream = QuoteStream(
            "GOLD", current_tokens,
            connection_factory=factory, reconnect_initial=0.01
        )
        stream.start()
        deadline = time.monotonic() + 1
        while len(connections) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        stream.stop()

        self.assertGreaterEqual(len(connections), 2)
        self.assertEqual(connections[0].sent[0]["destination"], "marketData.subscribe")
        self.assertEqual(connections[0].sent[0]["cst"], "cst-1")
        self.assertEqual(connections[1].sent[0]["cst"], "cst-2")
        self.assertNotIn("cst-1", str(connections[1].sent[0]))

    def test_subscription_requires_processed_acknowledgement(self):
        stream = QuoteStream("GOLD", lambda: ("cst", "token", 1))
        connection = Mock()
        connection.recv.return_value = json.dumps({
            "status": "OK", "destination": "marketData.subscribe", "correlationId": "sub-1",
            "payload": {"subscriptions": {"GOLD": "REJECTED"}},
        })
        with self.assertRaisesRegex(ConnectionError, "not processed"):
            stream._await_subscription(connection, "sub-1")

    def test_missing_quote_timestamp_is_ignored(self):
        stream = QuoteStream("GOLD", lambda: ("cst", "token"))
        stream._message(json.dumps({
            "destination": "quote", "payload": {"epic": "GOLD", "bid": 100, "ofr": 101},
        }))
        self.assertIsNone(stream.latest())

    def test_bot_watches_protected_positions_and_pending_triggers(self):
        bot = Bot.__new__(Bot)
        bot.state = CycleState(active=True)
        bot.state.long = Leg("BUY", D("100"), D("101"), stop=D("99"), take_profit=D("105"))
        bot.state.short = Leg(
            "SELL", D("100"), D("100"), open=False, trigger_id="order-1"
        )

        watches = bot._price_watches()

        self.assertEqual(set(watches), {
            PriceWatch("SL", "BUY", D("99")),
            PriceWatch("TP", "BUY", D("105")),
            PriceWatch("TRIGGER", "SELL", D("100")),
        })

    def test_stream_signal_triggers_rest_confirmation_while_quiet_quotes_do_not(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings(websocket_rest_fallback_seconds=10)
        bot.state = CycleState(active=True)
        bot.quotes = Mock()
        bot.quotes.latest.return_value = object()
        bot._tick_cycle = Mock()
        bot._last_cycle_rest_check = time.monotonic()
        bot._stream_signal = False

        bot.tick()
        bot._tick_cycle.assert_not_called()

        bot._stream_signal = True
        bot.tick()
        bot._tick_cycle.assert_called_once()

    def test_stale_stream_preserves_rest_polling_fallback(self):
        bot = Bot.__new__(Bot)
        bot.cfg = Settings(websocket_rest_fallback_seconds=10)
        bot.state = CycleState(active=True)
        bot.quotes = Mock()
        bot.quotes.latest.return_value = None
        bot._tick_cycle = Mock()
        bot._last_cycle_rest_check = time.monotonic()
        bot._stream_signal = False

        bot.tick()

        bot._tick_cycle.assert_called_once()


class DetailedNotificationRegressionTest(unittest.TestCase):
    def settings(self):
        return Settings(
            scenario_sizes=(D("10"), D("10")) + (D("20"),) * 7,
            scenario_stop_distances=(D("1"), D("2"), D("3")) + (D("4"),) * 6,
        )

    def test_cycle_273_first_and_second_increase_arithmetic(self):
        state = CycleState(general_recovery=D("102"))
        leg = Leg("SELL", D("4000"), D("3999.9"), size=D("20"), stop_distance=D("4"))
        state.short = leg
        text = leg_details(leg, general_recovery=state.general_recovery)
        self.assertIn("recovery_distance=5.1", text)

    def test_double_sl_attempt_uses_actual_sizes_ten_and_twenty(self):
        bot = EntryRetryTest().make_bot()
        bot.state.active_attempt_id = bot.state.attempt_counter = 501
        bot.state.cycle_id, bot.state.cycle_attempt = 50, 2
        buy, sell = bot.state.long, bot.state.short
        buy.deal_id, buy.current_entry, buy.stop, buy.size = "buy", D("100"), D("99"), D("10")
        sell.deal_id, sell.current_entry, sell.stop, sell.size = "sell", D("100"), D("103"), D("20")
        with tempfile.TemporaryDirectory() as directory, patch("trader.app.time.time", return_value=1000):
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            object.__setattr__(bot.cfg, "diagnostic_log_file", str(Path(directory) / "diag.log"))
            bot._begin_double_sl_pause([(buy, D("99")), (sell, D("103"))])
            first_total = bot.state.attempt_result_total
            bot._begin_double_sl_pause([(buy, D("99")), (sell, D("103"))])
        self.assertEqual(first_total, D("-70"))
        self.assertEqual(bot.state.attempt_result_total, D("-70"))
        self.assertEqual(bot.state.realized_loss_money, D("70"))
        report = bot.telegram.send.call_args.args[0]
        self.assertIn("результат закрытия -10", report)
        self.assertIn("результат закрытия -60", report)
        self.assertIn("результат попытки: -70", report)
        self.assertIn("накопленные убытки цикла: 70", report)

    def test_cycle_274_repeated_large_side_then_alignment(self):
        settings = self.settings(); state = CycleState(); strategy = Strategy(settings, state)
        strategy.begin(D("4300.5"), D("4300")); strategy.confirm_initial_fills(D("4300.5"), D("4300"))
        for direction in ("SELL", "SELL", "SELL", "BUY", "BUY"):
            leg = state.short if direction == "SELL" else state.long
            strategy.stopped(direction, leg.stop, f"stop-{state.scenario}-{direction}")
            strategy.reopened(direction, leg.original_trigger_level, f"deal-{state.scenario + 1}", f"open-{state.scenario + 1}")
        self.assertEqual(state.scenario, 6)

    def test_protection_details_use_individual_recovery_and_sources(self):
        leg = Leg("BUY", D("4285.14"), D("4285.14"), deal_id="buy", size=D("20"), stop_distance=D("1"))
        text = leg_details(leg, general_recovery=D("102"))
        self.assertIn("recovery_distance=5.1", text)
        self.assertNotIn("temporary:", text)

    def test_malformed_positions_never_confirms_expected_protection(self):
        bot = EntryRetryTest().make_bot()
        bot.capital.position.side_effect = None
        bot.capital.position.return_value = {
            "position": {"dealId": "buy", "stopLevel": "unparseable", "profitLevel": "999"}
        }
        with patch("trader.app.time.sleep"):
            actual = bot._wait_position_protection("buy", D("99"), D("102"), attempts=2)
        self.assertEqual(actual, (None, None))

    def test_failed_readback_retries_get_without_second_put(self):
        bot = EntryRetryTest().make_bot()
        leg = bot.state.long
        leg.deal_id, leg.stop, leg.take_profit = "buy", D("99"), D("102")
        bot.capital.update_position.return_value = "put-ref"
        bot.capital.wait_confirmation.return_value = {"dealStatus": "ACCEPTED"}
        bot.capital.position.side_effect = None
        bot.capital.position.return_value = {
            "position": {"dealId": "buy", "stopLevel": "bad", "profitLevel": "102"}}
        with tempfile.TemporaryDirectory() as directory, patch("trader.app.time.sleep"):
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            self.assertFalse(bot._apply_protection(leg))
            self.assertFalse(bot._apply_protection(leg))
        bot.capital.update_position.assert_called_once_with("buy", D("99"), D("102"))
        self.assertIn("НЕ ПОДТВЕРЖДЕНО", leg.protection_readback)

    def test_confirmation_and_readback_are_distinct_revisions(self):
        leg = Leg("BUY", D("100"), D("100"), deal_id="buy", stop=D("99"),
                  take_profit=D("103"), confirmed_stop=D("99"),
                  confirmed_take_profit=D("102"), protection_sent_stop=D("99"),
                  protection_sent_take_profit=D("103"), confirmation_stop=D("99"),
                  confirmation_take_profit=D("103"), protection_confirmation="ACCEPTED",
                  protection_readback="ожидается")
        text = leg_details(leg)
        self.assertIn("расчётные SL/TP=99 / 103", text)
        self.assertIn("принятые SL/TP=99 / 103", text)
        self.assertIn("последние сохранённые read-back SL/TP", text)
        self.assertIn("повторное чтение /positions=ожидается", text)

    def test_nonclose_deal_history_falls_back_to_global_and_uses_old_range(self):
        client = Mock()
        client.activity.side_effect = [
            [{"dealId": "buy-old", "source": "USER", "status": "ACCEPTED",
              "type": "POSITION", "level": 100}],
            [{"dealId": "buy-old", "source": "TP", "status": "ACCEPTED",
              "type": "POSITION", "level": 105}],
        ]
        started = time.time() - 3 * 86400
        result = NotificationHistoryWorker._resolve(client, {
            "waiting": {"deal_id": "buy-old"}, "search_from_epoch": started,
            "search_to_epoch": started + 86400,
        })
        self.assertEqual(result, {"source": "TP", "fill": "105"})
        first = client.activity.call_args_list[0]
        self.assertIn("from_date", first.kwargs)
        self.assertIn("to_date", first.kwargs)
        self.assertNotIn("last_period", first.kwargs)
        start_value = datetime.fromisoformat(first.kwargs["from_date"]).replace(tzinfo=timezone.utc)
        end_value = datetime.fromisoformat(first.kwargs["to_date"]).replace(tzinfo=timezone.utc)
        self.assertLessEqual((end_value - start_value).total_seconds(), 86400)

    def test_specific_sl_returns_without_global_request(self):
        client = Mock()
        client.activity.return_value = [{
            "dealId": "wanted", "source": "SL", "status": "ACCEPTED", "level": 99,
        }]
        result = NotificationHistoryWorker._resolve(client, {
            "waiting": {"deal_id": "wanted"}, "search_from_epoch": 1000,
            "search_to_epoch": 2000,
        })
        self.assertEqual(result, {"source": "SL", "fill": "99"})
        client.activity.assert_called_once()
        self.assertEqual(client.activity.call_args.args[0], "wanted")

    def test_specific_tp_returns_even_if_global_would_fail(self):
        client = Mock()

        def activity(deal_id="", **kwargs):
            if not deal_id:
                raise CapitalError("global unavailable")
            return [{"dealId": "wanted", "source": "TP", "status": "ACCEPTED",
                     "level": 103}]

        client.activity.side_effect = activity
        result = NotificationHistoryWorker._resolve(client, {
            "waiting": {"deal_id": "wanted"}, "search_from_epoch": 1000,
            "search_to_epoch": 2000,
        })
        self.assertEqual(result, {"source": "TP", "fill": "103"})
        self.assertEqual(client.activity.call_count, 1)

    def test_foreign_deal_close_is_not_accepted(self):
        client = Mock()
        client.activity.return_value = [{
            "dealId": "foreign", "source": "SL", "status": "ACCEPTED", "level": 99,
        }]
        result = NotificationHistoryWorker._resolve(client, {
            "waiting": {"deal_id": "wanted"}, "search_from_epoch": 1000,
            "search_to_epoch": 2000,
        })
        self.assertIsNone(result)
        self.assertEqual(client.activity.call_count, 2)

    def test_legacy_job_without_dates_uses_frozen_last_day_and_finds_recent_sl(self):
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        event_time = now - 60
        calls = []

        class FilteringClient:
            def activity(self, deal_id="", **kwargs):
                calls.append((deal_id, kwargs))
                start = datetime.fromisoformat(kwargs["from_date"]).replace(tzinfo=timezone.utc).timestamp()
                end = datetime.fromisoformat(kwargs["to_date"]).replace(tzinfo=timezone.utc).timestamp()
                if deal_id == "legacy" and start <= event_time <= end:
                    return [{"dealId": "legacy", "source": "SL", "status": "ACCEPTED",
                             "level": 98, "dateUTC": "2026-09-17T11:59:00"}]
                return []

        job = {"waiting": {"deal_id": "legacy"}}
        with patch("trader.notifications.time.time", return_value=now):
            result = NotificationHistoryWorker._resolve(FilteringClient(), job)
        self.assertEqual(result, {"source": "SL", "fill": "98"})
        self.assertEqual(job["search_from_epoch"], now - 86400)
        self.assertEqual(job["search_to_epoch"], now)
        self.assertTrue(job["history_range_uncertain"])
        self.assertEqual(len(calls), 1)

    def test_legacy_job_migration_survives_load_save_and_preserves_known_bounds(self):
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        legacy = {"key": "legacy", "waiting": {"deal_id": "old"}, "closed": {}}
        known = {"key": "known", "waiting": {"deal_id": "dated"},
                 "search_from_epoch": 1234.0, "search_to_epoch": 5678.0,
                 "search_range_source": "saved_attempt"}
        state = CycleState(pending_notification_jobs=[legacy, known])
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            state.save(path)
            loaded = CycleState.load(path)
            self.assertTrue(migrate_notification_jobs(
                loaded.pending_notification_jobs, now=now
            ))
            loaded.save(path)
            restored = CycleState.load(path)
        job = restored.pending_notification_jobs[0]
        self.assertEqual(job["search_from_epoch"], now - 86400)
        self.assertEqual(job["search_to_epoch"], now)
        self.assertEqual(job["search_range_source"], "legacy_fallback_last_24h")
        self.assertFalse(migrate_notification_jobs([job], now=now + 9999))
        self.assertEqual(job["search_from_epoch"], now - 86400)
        known_restored = restored.pending_notification_jobs[1]
        self.assertEqual(known_restored["search_from_epoch"], 1234.0)
        self.assertEqual(known_restored["search_to_epoch"], 5678.0)

    def test_legacy_job_uses_trustworthy_snapshot_timestamp_not_migration_time(self):
        event_time = datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc).timestamp()
        migration_time = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        job = {
            "waiting": {"deal_id": "old"},
            "closed": {"deal_id": "known", "dateUTC": "2026-09-10T08:30:00Z"},
        }
        self.assertTrue(migrate_notification_jobs([job], now=migration_time))
        self.assertEqual(job["search_from_epoch"], event_time - 3600)
        self.assertEqual(job["search_to_epoch"], event_time + 3600)
        self.assertEqual(job["search_range_source"], "attempt_snapshot")
        self.assertFalse(job["history_range_uncertain"])

    def test_broker_utc_timestamps_are_timezone_independent(self):
        expected = datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc).timestamp()
        representations = (
            ("dateUTC", "2026-09-10T08:30:00"),
            ("createdDateUTC", "2026-09-10T08:30:00"),
            ("dateUTC", "2026-09-10T08:30:00Z"),
            ("dateUTC", "2026-09-10T13:30:00+05:00"),
        )
        original_tz = os.environ.get("TZ")
        try:
            for process_tz in ("UTC", "Etc/GMT-5"):
                os.environ["TZ"] = process_tz
                time.tzset()
                for field, value in representations:
                    job = {"waiting": {"deal_id": "old"}, "closed": {field: value}}
                    self.assertTrue(migrate_notification_jobs([job], now=expected + 86400))
                    self.assertEqual(job["search_from_epoch"], expected - 3600)
                    self.assertEqual(job["search_to_epoch"], expected + 3600)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

    def test_numeric_epoch_is_timezone_independent_but_generic_naive_time_is_uncertain(self):
        expected = datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc).timestamp()
        original_tz = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "Etc/GMT-5"
            time.tzset()
            numeric = {"waiting": {"deal_id": "old"}, "event_epoch": expected}
            self.assertTrue(migrate_notification_jobs([numeric], now=expected + 86400))
            self.assertEqual(numeric["search_from_epoch"], expected - 3600)
            generic = {
                "waiting": {"deal_id": "old", "timestamp": "2026-09-10T08:30:00"}
            }
            migration_time = expected + 7 * 86400
            self.assertTrue(migrate_notification_jobs([generic], now=migration_time))
            self.assertEqual(generic["search_range_source"], "legacy_fallback_last_24h")
            self.assertTrue(generic["history_range_uncertain"])
            self.assertEqual(generic["search_from_epoch"], migration_time - 86400)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time.tzset()

    def test_repair_old_local_timezone_migration_and_find_close_after_restart(self):
        event_epoch = datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc).timestamp()
        wrong_start = datetime(2026, 9, 10, 2, 30, tzinfo=timezone.utc).timestamp()
        wrong_end = datetime(2026, 9, 10, 4, 30, tzinfo=timezone.utc).timestamp()
        original = {
            "key": "old-migrated", "cycle_id": 270,
            "waiting": {"deal_id": "buy-270", "size": "10"},
            "closed": {"deal_id": "sell-270", "dateUTC": "2026-09-10T08:30:00"},
            "search_from_epoch": wrong_start, "search_to_epoch": wrong_end,
            "search_range_source": "attempt_snapshot", "history_range_uncertain": False,
        }
        state = CycleState(pending_notification_jobs=[original])
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            state.save(path)
            loaded = CycleState.load(path)
            self.assertTrue(migrate_notification_jobs(loaded.pending_notification_jobs))
            loaded.save(path)
            restored = CycleState.load(path)
        repaired = restored.pending_notification_jobs[0]
        self.assertEqual(repaired["search_from_epoch"], event_epoch - 3600)
        self.assertEqual(repaired["search_to_epoch"], event_epoch + 3600)
        self.assertEqual(repaired["search_time_version"], 2)
        self.assertEqual(repaired["cycle_id"], 270)
        self.assertEqual(repaired["waiting"]["deal_id"], "buy-270")
        self.assertFalse(migrate_notification_jobs([repaired]))

        class FilteringClient:
            def activity(self, deal_id="", **kwargs):
                start = datetime.fromisoformat(kwargs["from_date"]).replace(tzinfo=timezone.utc).timestamp()
                end = datetime.fromisoformat(kwargs["to_date"]).replace(tzinfo=timezone.utc).timestamp()
                if deal_id == "buy-270" and start <= event_epoch <= end:
                    return [{"dealId": "buy-270", "source": "SL", "status": "ACCEPTED",
                             "level": 98, "dateUTC": "2026-09-10T08:30:00"}]
                return []

        with patch("trader.notifications.time.time", return_value=event_epoch + 86400):
            self.assertEqual(
                NotificationHistoryWorker._resolve(FilteringClient(), repaired),
                {"source": "SL", "fill": "98"},
            )

    def test_unverifiable_old_attempt_snapshot_range_is_marked_uncertain_not_shifted(self):
        job = {
            "waiting": {"deal_id": "old"}, "search_from_epoch": 1000,
            "search_to_epoch": 2000, "search_range_source": "attempt_snapshot",
        }
        self.assertTrue(migrate_notification_jobs([job], now=9999))
        self.assertEqual((job["search_from_epoch"], job["search_to_epoch"]), (1000, 2000))
        self.assertEqual(job["search_range_source"], "attempt_snapshot_unverified")
        self.assertTrue(job["history_range_uncertain"])
        self.assertFalse(migrate_notification_jobs([job], now=19999))

    def test_uncertain_range_stays_pending_and_late_publication_is_found(self):
        now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc).timestamp()
        events = []

        class Client:
            def activity(self, deal_id="", **kwargs):
                return list(events)

        job = {"waiting": {"deal_id": "late"}}
        with patch("trader.notifications.time.time", return_value=now):
            self.assertIsNone(NotificationHistoryWorker._resolve(Client(), job))
            frozen = (job["search_from_epoch"], job["search_to_epoch"])
            events.append({"dealId": "late", "source": "SL", "status": "ACCEPTED",
                           "level": 97})
            self.assertEqual(
                NotificationHistoryWorker._resolve(Client(), job),
                {"source": "SL", "fill": "97"},
            )
        self.assertEqual((job["search_from_epoch"], job["search_to_epoch"]), frozen)

    def test_history_api_error_does_not_invent_a_result(self):
        client = Mock()
        client.activity.side_effect = CapitalError("history unavailable")
        job = {"waiting": {"deal_id": "late"}}
        with patch("trader.notifications.time.time", return_value=100000):
            with self.assertRaises(CapitalError):
                NotificationHistoryWorker._resolve(client, job)
        self.assertTrue(job["history_range_uncertain"])

    def test_old_history_job_splits_saved_range_into_one_day_windows(self):
        calls = []

        class HistoryClient:
            def activity(self, deal_id="", last_period=86400, *, from_date="", to_date=""):
                calls.append((deal_id, from_date, to_date, last_period))
                if len(calls) == 6:
                    return [{"dealId": "old-deal", "source": "SL", "status": "ACCEPTED",
                             "type": "POSITION", "level": 97}]
                return []

        started = time.time() - 5 * 86400
        result = NotificationHistoryWorker._resolve(HistoryClient(), {
            "waiting": {"deal_id": "old-deal"}, "search_from_epoch": started,
            "search_to_epoch": started + 3 * 86400,
        })
        self.assertEqual(result, {"source": "SL", "fill": "97"})
        self.assertEqual(len(calls), 6)
        for _, start_text, end_text, _ in calls:
            start_value = datetime.fromisoformat(start_text).replace(tzinfo=timezone.utc)
            end_value = datetime.fromisoformat(end_text).replace(tzinfo=timezone.utc)
            self.assertLessEqual((end_value - start_value).total_seconds(), 86400)

    def test_late_tp_report_uses_historical_tp_and_current_state(self):
        bot = EntryRetryTest().make_bot()
        bot.state.phase, bot.state.active, bot.state.manual = "BOTH_OPEN", True, False
        first = {"direction": "SELL", "deal_id": "s", "entry": "100", "size": "10",
                 "stop": "101", "take_profit": "98", "source": "SL", "fill": "101"}
        second = {"direction": "BUY", "deal_id": "b", "entry": "100", "size": "10",
                  "stop": "99", "take_profit": "102", "source": "TP", "fill": "102.1"}
        text = bot._initial_pair_report_text(first, second, final=True, meta={
            "cycle_id": 270, "cycle_attempt": 1,
            "original_decision": "MANUAL: исходная пара не сформирована",
        })
        self.assertIn("исторический TP=102", text)
        self.assertIn("slippage=0.1", text)
        self.assertIn("причина=TP", text)
        self.assertIn("текущее состояние бота: phase=BOTH_OPEN, active=True, manual=False", text)

    def test_cycle_completion_and_report_are_one_state_commit(self):
        bot = EntryRetryTest().make_bot()
        bot.state.active_attempt_id = 73
        bot.state.cycle_id = 73
        bot.state.long.deal_id = "winner"
        with tempfile.TemporaryDirectory() as directory:
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            bot._complete_cycle("BUY", D("4012"))
            restored = CycleState.load(bot.cfg.state_file)
        self.assertFalse(restored.active)
        self.assertEqual(restored.completed_cycles, 1)
        self.assertTrue(any(item["key"].startswith("cycle-complete:73")
                            for item in restored.report_outbox))

    def test_delivery_ack_is_saved_immediately(self):
        bot = Bot.__new__(Bot)
        bot.telegram = Telegram("token", "chat")
        bot.notification_worker = None
        bot._queued_report_parts = {("1:r", 1)}
        bot._queued_log_parts = set()
        bot.state = CycleState(report_outbox=[{"id": "1:r", "key": "r", "parts": [
            {"number": 1, "text": "done", "status": "pending"}]}])
        with tempfile.TemporaryDirectory() as directory:
            bot.cfg = Settings(state_file=str(Path(directory) / "state.json"),
                               diagnostic_log_file=str(Path(directory) / "diag.log"))
            bot.telegram._delivery_acks.append(
                {"report_id": "1:r", "part": 1, "status": "delivered"})
            bot._tick_notifications()
            restored = CycleState.load(bot.cfg.state_file)
        self.assertEqual(restored.report_outbox, [])

    def test_closed_leg_levels_are_labelled_historical(self):
        leg = Leg("SELL", D("100"), D("99"), deal_id="closed", open=False,
                  size=D("20"), stop_distance=D("3"), recovery=D("3"),
                  stop=D("102"), take_profit=D("93"))
        text = leg_details(leg)
        self.assertIn("исторические последние SL/TP", text)
        self.assertIn("не являются расчётом от нового Recovery", text)

    def test_cycle_result_lists_weighted_deals_and_trigger_fate(self):
        state = CycleState(gross_take_profit=D("25.71"), realized_losses=D("0"),
                           realized_loss_money=D("424.70"), net_cycle_money=D("89.50"))
        state.long = Leg("BUY", D("4300"), D("4300"), size=D("20"))
        state.attempt_deal_ids = ["winner"] + [f"loss-{i}" for i in range(8)]
        state.deal_history = [
            {"deal_id": "winner", "direction": "BUY", "entry": "4295.15",
             "close_level": "4320.86", "close_source": "TP", "size": "20"},
        ] + [
            {"deal_id": f"loss-{index}", "direction": "SELL", "entry": "4300",
             "close_level": str(D("4300") + loss / size), "close_source": "SL",
             "size": str(size)}
            for index, (loss, size) in enumerate(zip(
                map(D, ("10.60", "10.00", "60.00", "20.30", "81.40", "80.40", "81.20", "80.80")),
                map(D, ("10", "10", "20", "10", "20", "20", "20", "20")),
            ))
        ]
        state.last_trigger_resolution = "workingOrderId=trigger: DELETE confirmation ACCEPTED."
        text = cycle_result_text(state, "BUY", D("4320.86"), D("10"))
        self.assertIn("dealId=winner", text)
        self.assertIn("причина=TP", text)
        self.assertIn("(4320.86 − 4295.15) × 20=514.20", text)
        self.assertIn("убытки 424.70; итог 89.50", text)
        self.assertIn("workingOrderId=trigger", text)
        self.assertEqual(text.count("причина=SL"), 8)

    def test_cycle_273_result_is_summed_from_every_deal(self):
        state = CycleState(gross_take_profit=D("22.10"))
        state.long = Leg("BUY", D("100"), D("100"), size=D("10"))
        state.attempt_deal_ids = ["winner", "loss-a", "loss-b"]
        state.deal_history = [
            {"deal_id": "winner", "direction": "BUY", "entry": "100",
             "close_level": "122.10", "close_source": "TP", "size": "10"},
            {"deal_id": "loss-a", "direction": "SELL", "entry": "100",
             "close_level": "105", "close_source": "SL", "size": "10"},
            {"deal_id": "loss-b", "direction": "BUY", "entry": "100",
             "close_level": "94.83", "close_source": "SL", "size": "10"},
        ]
        text = cycle_result_text(state, "BUY", D("122.10"), D("10"))
        self.assertIn("прибыль 221.00; убытки 101.70; итог 119.30", text)

    def test_continuation_cycle_result_keeps_prior_attempt_losses(self):
        state = CycleState(cycle_id=77, gross_take_profit=D("5"),
                           realized_loss_money=D("30"), net_cycle_money=D("20"))
        state.long = Leg("BUY", D("100"), D("100"), size=D("10"))
        state.deal_history = [
            {"deal_id": "current-loss", "cycle_id": 77, "direction": "SELL",
             "entry": "100", "close_level": "101", "close_source": "SL", "size": "10"},
            {"deal_id": "winner", "cycle_id": 77, "direction": "BUY",
             "entry": "100", "close_level": "105", "close_source": "TP", "size": "10"},
        ]
        state.attempt_history = [
            {"cycle_id": 77, "cycle_attempt": 1, "status": "DOUBLE_SL_CONTINUATION",
             "result": "-20"},
            {"cycle_id": 77, "cycle_attempt": 2, "status": "COMPLETED_CYCLE",
             "result": "20"},
        ]
        text = cycle_result_text(state, "BUY", D("105"), D("10"))
        self.assertIn("прибыль 50; убытки 30; итог 20", text)
        self.assertIn("НЕПОЛНАЯ; денежный итог взят из полного сохранённого агрегата", text)
        self.assertIn("попытка 1: DOUBLE_SL_CONTINUATION = -20", text)

    def test_completion_has_one_durable_full_result(self):
        bot = EntryRetryTest().make_bot()
        bot.telegram = Telegram("token", "chat")
        bot._queued_report_parts = set()
        bot.state.active_attempt_id = bot.state.cycle_id = 88
        bot.state.long.deal_id = "winner"
        with tempfile.TemporaryDirectory() as directory:
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            bot._complete_cycle("BUY", D("4012"))
            bot._send_report(
                "✅ wrapper\n" + cycle_result_text(bot.state, "BUY", D("4012"), bot.cfg.size)
            )
        full = [item for item in bot.state.report_outbox
                if any("Итог завершённого цикла" in part["text"] for part in item["parts"])]
        self.assertEqual(len(full), 1)

    def test_positions_match_updates_readback_without_put(self):
        bot = EntryRetryTest().make_bot()
        leg = bot.state.long
        leg.deal_id, leg.stop, leg.take_profit = "buy", D("99"), D("103")
        leg.protection_sent_stop, leg.protection_sent_take_profit = D("99"), D("103")
        leg.protection_readback = "НЕ ПОДТВЕРЖДЕНО"
        with tempfile.TemporaryDirectory() as directory:
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            self.assertTrue(bot._protection_matches(
                {"dealId": "buy", "stopLevel": "99", "profitLevel": "103"}, leg
            ))
        self.assertEqual(leg.confirmed_stop, D("99"))
        self.assertEqual(leg.confirmed_take_profit, D("103"))
        self.assertEqual(leg.protection_readback, "ПОДТВЕРЖДЕНО")
        bot.capital.update_position.assert_not_called()

    def test_legacy_legs_keep_individual_size_and_stop_distance(self):
        raw = {
            "active": True, "scenario": 3, "phase": "BOTH_OPEN",
            "long": {"direction": "BUY", "original_trigger_level": "100",
                     "current_entry": "101", "deal_id": "buy", "recovery": "7",
                     "temporary_stop_compensation": "0", "temporary_spread_compensation": "0",
                     "temporary_slippage_compensation": "0"},
            "short": {"direction": "SELL", "original_trigger_level": "100",
                      "current_entry": "100", "deal_id": "sell", "size": "10",
                      "stop_distance": "1", "recovery": "7",
                      "temporary_stop_compensation": "0", "temporary_spread_compensation": "0",
                      "temporary_slippage_compensation": "0"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            state = CycleState.load(str(path))
            bot = EntryRetryTest().make_bot(); bot.state = state
            object.__setattr__(bot.cfg, "state_file", str(path))
            bot._migrate_legacy_legs({
                "buy": {"dealId": "buy", "size": "20", "stopLevel": "98"},
                "sell": {"dealId": "sell", "size": "10", "stopLevel": "101"},
            })
            restored = CycleState.load(str(path))
        self.assertEqual(restored.long.size, D("20"))
        self.assertEqual(restored.long.stop_distance, D("3"))
        self.assertEqual(restored.short.size, D("10"))
        self.assertEqual(restored.short.stop_distance, D("1"))
        self.assertEqual(restored.long.original_trigger_level, D("100"))
        self.assertEqual(restored.long.current_entry, D("101"))

    def test_legacy_leg_with_unknown_recovery_is_durably_blocked(self):
        raw = {"active": True, "scenario": 3, "phase": "BOTH_OPEN",
               "long": {"direction": "BUY", "original_trigger_level": "100",
                        "current_entry": "101", "deal_id": "buy"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            bot = EntryRetryTest().make_bot(); bot.state = CycleState.load(str(path))
            object.__setattr__(bot.cfg, "state_file", str(path))
            with self.assertRaisesRegex(RuntimeError, "старое состояние"):
                bot._migrate_legacy_legs(
                    {"buy": {"dealId": "buy", "size": "20", "stopLevel": "98"}}
                )
            restored = CycleState.load(str(path))
        self.assertIn("recovery", restored.long.legacy_missing_fields)
        self.assertNotIn("size", restored.long.legacy_missing_fields)
        self.assertNotIn("stop_distance", restored.long.legacy_missing_fields)

    def test_second_initial_confirmation_announces_protection_not_another_open(self):
        bot = EntryRetryTest().make_bot()
        bot.state.long.deal_id = "buy"
        bot.capital.open_position.return_value = "sell-ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "sell", "level": 99,
        }
        bot.capital.wait_position.return_value = {
            "dealId": "sell", "direction": "SELL", "level": 99,
        }
        with tempfile.TemporaryDirectory() as directory:
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            self.assertIsNone(bot._open_initial_leg(bot.state.short))
        text = bot.telegram.send.call_args.args[0]
        self.assertIn("установить и проверить точные SL/TP обеих сторон", text)
        self.assertNotIn("открыть вторую сторону", text)

    def test_history_worker_resolves_late_close_without_trading_mutation(self):
        client = Mock()
        client.activity.return_value = [{
            "dealId": "buy-270", "source": "SL", "status": "ACCEPTED",
            "type": "POSITION", "details": {"level": 4285.04},
        }]
        job = {"waiting": {"deal_id": "buy-270"}}
        result = NotificationHistoryWorker._resolve(client, job)
        self.assertEqual(result, {"source": "SL", "fill": "4285.04"})
        client.open_position.assert_not_called()
        client.update_position.assert_not_called()

    def test_notification_jobs_and_outbox_survive_restart(self):
        state = CycleState()
        state.pending_notification_jobs = [{
            "key": "initial", "waiting": {"deal_id": "buy"}, "closed": {},
        }]
        state.report_outbox = [{
            "id": "1:report", "key": "report", "parts": [
                {"number": 1, "text": "one", "status": "delivered"},
                {"number": 2, "text": "two", "status": "pending"},
                {"number": 3, "text": "retry next process", "status": "failed"},
            ],
        }]
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.json")
            state.save(path)
            loaded = CycleState.load(path)
        self.assertEqual(loaded.pending_notification_jobs[0]["key"], "initial")
        self.assertEqual(loaded.report_outbox[0]["parts"][0]["status"], "delivered")
        self.assertEqual(loaded.report_outbox[0]["parts"][1]["status"], "pending")
        self.assertEqual(loaded.report_outbox[0]["parts"][2]["status"], "pending")

    def test_late_manual_result_uses_saved_snapshot_after_legs_change(self):
        bot = EntryRetryTest().make_bot()
        bot.state.long = Leg("BUY", D("999"), D("999"), deal_id="new-cycle")
        bot.state.pending_notification_jobs = [{
            "key": "initial-270", "cycle_id": 270, "cycle_attempt": 1, "scenario": 1,
            "closed": {"direction": "SELL", "deal_id": "sell-270", "entry": "4285.12",
                       "size": "10", "stop": "4286.12", "source": "SL", "fill": "4286.14"},
            "waiting": {"direction": "BUY", "deal_id": "buy-270", "entry": "4286.07",
                        "size": "10", "stop": "4285.07", "source": "", "fill": None},
        }]
        worker = Mock()
        worker.results.return_value = [{
            "key": "initial-270", "result": {"source": "SL", "fill": "4285.04"},
        }]
        bot.notification_worker = worker
        with tempfile.TemporaryDirectory() as directory:
            object.__setattr__(bot.cfg, "state_file", str(Path(directory) / "state.json"))
            bot._tick_notifications()
        text = bot.telegram.send.call_args.args[0]
        self.assertIn("Цикл №270", text)
        self.assertIn("dealId=buy-270", text)
        self.assertIn("-20.50", text)
        self.assertNotIn("new-cycle", text)
        self.assertEqual(bot.state.pending_notification_jobs, [])

    def test_restart_queues_only_unconfirmed_report_parts(self):
        bot = Bot.__new__(Bot)
        bot.telegram = Telegram("token", "chat")
        bot.state = CycleState(report_outbox=[{
            "id": "7:result", "key": "result", "parts": [
                {"number": 1, "text": "already sent", "status": "delivered"},
                {"number": 2, "text": "resume me", "status": "pending"},
            ],
        }])
        bot._queued_report_parts = set()
        bot._queue_pending_reports()
        self.assertEqual(len(bot.telegram._messages), 1)
        self.assertEqual(bot.telegram._messages[0].part, 2)
        self.assertEqual(bot.telegram._messages[0].value, "resume me")

    def test_stable_report_parts_and_delivery_ack(self):
        parts = split_report("A\n" * 4000, limit=1000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(parts[0].startswith(f"Часть 1/{len(parts)}"))
        telegram = Telegram("token", "chat")
        telegram.send_report_part(parts[0], "report-1", 1, len(parts))
        item = telegram._messages[0]
        telegram._ack(item, "delivered")
        self.assertEqual(telegram.delivery_acks(), [{
            "report_id": "report-1", "part": 1, "status": "delivered",
        }])


class PydroidConfigTest(unittest.TestCase):
    def test_document_success_stays_inflight_until_ack_is_persisted(self):
        telegram = Telegram("token", "123")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.log"; path.write_text("data", encoding="utf-8")
            self.assertTrue(telegram.send_document_part(str(path), "snap", 1, 1))
            item = telegram._documents[0]
            telegram._report_document_part(item, delivered=True)
            self.assertIn(("snap", 1), telegram.queued_document_parts())
            self.assertTrue(telegram.send_document_part(str(path), "snap", 1, 1))
            self.assertEqual(len(telegram._documents), 1)
            telegram.confirm_document_ack("snap", 1)
            self.assertNotIn(("snap", 1), telegram.queued_document_parts())

    def test_restored_document_group_keeps_delivered_part_and_completes_once(self):
        telegram = Telegram("token", "123")
        telegram.restore_document_group("snap", 2, [1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part-2.log"; path.write_text("two", encoding="utf-8")
            telegram.send_document_part(str(path), "snap", 2, 2)
            item = telegram._documents[0]
            telegram._report_document_part(item, delivered=True)
            acks = telegram.document_acks()
            telegram._report_document_part(item, delivered=True)
        self.assertEqual(acks, [{"snapshot_id": "snap", "part": 2, "status": "delivered"}])
        self.assertNotIn("snap", telegram._document_groups)

    def test_main_owner_emits_one_completion_after_persisting_document_ack(self):
        bot = Bot.__new__(Bot)
        bot.telegram = Telegram("token", "123")
        bot.notification_worker = None
        bot.state = CycleState()
        bot._queued_report_parts = set(); bot._queued_log_parts = {("snap", 2)}
        bot._send_report = Mock()
        bot.cfg = Settings(diagnostic_log_file="unused.log", state_file="unused-state.json")
        bot.telegram._document_acks.append(
            {"snapshot_id": "snap", "part": 2, "status": "delivered"}
        )
        with patch("trader.app.acknowledge_diagnostic_snapshot", return_value=True), \
                patch("trader.app.pending_diagnostic_snapshots", return_value=[]):
            bot._tick_notifications()
            bot._tick_notifications()
        bot._send_report.assert_called_once_with(
            "✅ /sendlog полностью доставлен: снимок snap", key="sendlog-complete:snap"
        )

    def test_missing_document_file_reports_failure_without_killing_worker(self):
        telegram = Telegram("token", "123")
        response = Mock()
        delivered = __import__("threading").Event()

        def post(url, **kwargs):
            if url.endswith("/sendMessage"):
                delivered.set()
            return response

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.log"; path.write_text("data", encoding="utf-8")
            telegram.send_document_part(str(path), "missing", 1, 1)
            path.unlink()
            with patch("trader.telegram.requests.get", side_effect=requests.ReadTimeout("offline")), \
                    patch("trader.telegram.requests.post", side_effect=post):
                telegram.start()
                telegram.send("worker still alive")
                self.assertTrue(delivered.wait(1))
                deadline = time.monotonic() + 1
                acks = []
                while not acks and time.monotonic() < deadline:
                    acks = telegram.document_acks()
                    time.sleep(0.01)
                telegram.stop()
        self.assertEqual(acks[0]["status"], "failed")

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
            (target / "bot_diagnostics.log").write_text("legacy log", encoding="utf-8")
            history = target / "bot_diagnostics.log.history"
            history.mkdir()
            (history / "index.json").write_text('{"next_segment":7}', encoding="utf-8")

            copy_project(source, target)
            created = create_config(target)

            self.assertFalse(created)
            self.assertEqual((target / "bot_config.json").read_text(), '{"secret":"keep"}')
            self.assertEqual((target / "bot_state.json").read_text(), '{"scenario":4}')
            self.assertEqual((target / "bot_diagnostics.log").read_text(), "legacy log")
            self.assertEqual((history / "index.json").read_text(), '{"next_segment":7}')
            self.assertEqual((target / "trader/app.py").read_text(), "NEW = True\n")

    def test_telegram_queue_calls_never_perform_network_io(self):
        telegram = Telegram("token", "123")
        with patch("trader.telegram.requests.get") as get, patch("trader.telegram.requests.post") as post:
            telegram.send("ready", show_menu=True)
            telegram.send_document(__file__)
            telegram.install_commands()
            self.assertEqual(telegram.commands(), [])
        get.assert_not_called()
        post.assert_not_called()
        self.assertEqual(telegram.pending_reports, 3)

    def test_telegram_sender_timeout_cannot_block_trading_thread(self):
        telegram = Telegram("token", "123")
        entered = __import__("threading").Event()
        release = __import__("threading").Event()
        response = Mock()

        def slow_post(*args, **kwargs):
            entered.set()
            release.wait(1)
            return response

        with patch("trader.telegram.requests.get", side_effect=requests.ReadTimeout("offline")), \
                patch("trader.telegram.requests.post", side_effect=slow_post):
            telegram.start()
            started = time.monotonic()
            self.assertTrue(telegram.send("important"))
            self.assertLess(time.monotonic() - started, 0.05)
            self.assertTrue(entered.wait(0.5))
            self.assertEqual(telegram.commands(), [])
            release.set()
            deadline = time.monotonic() + 1
            while telegram.pending_reports and time.monotonic() < deadline:
                time.sleep(0.01)
            telegram.stop()
        self.assertEqual(telegram.pending_reports, 0)

    def test_telegram_poll_worker_discards_old_then_queues_new_command(self):
        telegram = Telegram("token", "123")
        old = Mock()
        old.json.return_value = {"result": [{"update_id": 77, "message": {
            "chat": {"id": 123}, "text": "/old"}}]}
        new = Mock()
        new.json.return_value = {"result": [{"update_id": 78, "message": {
            "chat": {"id": 123}, "text": "/status"}}]}
        empty = Mock()
        empty.json.return_value = {"result": []}
        with patch("trader.telegram.requests.get", side_effect=[old, new, empty, empty, empty]), \
                patch("trader.telegram.requests.post", return_value=Mock()):
            telegram.start()
            deadline = time.monotonic() + 1
            commands = []
            while not commands and time.monotonic() < deadline:
                commands = telegram.commands()
                time.sleep(0.01)
            telegram.stop()
        self.assertEqual(commands, ["/status"])
        self.assertEqual(telegram.offset, 79)

    def test_slow_failed_document_does_not_block_normal_message(self):
        telegram = Telegram("secret-token", "123")
        upload_started = __import__("threading").Event()
        release_upload = __import__("threading").Event()
        message_delivered = __import__("threading").Event()
        response = Mock()

        def post(url, **kwargs):
            if url.endswith("/sendDocument"):
                upload_started.set()
                release_upload.wait(1)
                raise requests.Timeout(f"write timeout {url}")
            if url.endswith("/sendMessage"):
                message_delivered.set()
            return response

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "bot_diagnostics.log"
            log.write_text("quote event\n" * 10000, encoding="utf-8")
            with patch("trader.telegram.requests.get", side_effect=requests.ReadTimeout("offline")), \
                    patch("trader.telegram.requests.post", side_effect=post), \
                    patch.object(telegram, "_failure_policy", return_value=(True, 0)):
                telegram.start()
                telegram.send_document(str(log), compress=True)
                self.assertTrue(upload_started.wait(1))
                telegram.send("ordinary report")
                self.assertTrue(message_delivered.wait(0.5))
                release_upload.set()
                deadline = time.monotonic() + 1
                while telegram.pending_reports and time.monotonic() < deadline:
                    time.sleep(0.01)
                telegram.stop()
        self.assertEqual(telegram.pending_reports, 0)

    def test_compressed_document_recovers_after_transient_write_timeout(self):
        telegram = Telegram("token", "123")
        attempts = 0
        uploads = []
        timeouts = []
        response = Mock()

        def post(url, **kwargs):
            nonlocal attempts
            if url.endswith("/sendDocument"):
                attempts += 1
                document = kwargs["files"]["document"]
                uploads.append((Path(document.name).suffix, Path(document.name).stat().st_size))
                timeouts.append(kwargs["timeout"])
                if attempts == 1:
                    raise requests.Timeout("temporary upload failure")
            return response

        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "bot_diagnostics.log"
            log.write_text("same diagnostic line\n" * 50000, encoding="utf-8")
            original_size = log.stat().st_size
            with patch("trader.telegram.requests.get", side_effect=requests.ReadTimeout("offline")), \
                    patch("trader.telegram.requests.post", side_effect=post), \
                    patch.object(telegram, "_failure_policy", return_value=(False, 0.01)):
                telegram.start()
                telegram.send_document(str(log), compress=True)
                deadline = time.monotonic() + 2
                while (attempts < 2 or telegram.pending_reports) and time.monotonic() < deadline:
                    time.sleep(0.01)
                telegram.stop()
            leftovers = list(Path(directory).glob("telegram-*.gz"))
        self.assertEqual(attempts, 2)
        self.assertTrue(all(size < original_size for _, size in uploads))
        self.assertEqual(timeouts, [180, 180])
        self.assertEqual(leftovers, [])

    def test_two_part_snapshot_retries_only_failed_part(self):
        telegram = Telegram("token", "123")
        response = Mock()
        attempts = {}
        messages = []

        def post(url, **kwargs):
            if url.endswith("/sendDocument"):
                name = Path(kwargs["files"]["document"].name).name
                attempts[name] = attempts.get(name, 0) + 1
                if "part-2" in name and attempts[name] == 1:
                    raise requests.Timeout("second part delayed")
            elif url.endswith("/sendMessage"):
                messages.append(kwargs["json"]["text"])
            return response

        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for number in (1, 2):
                path = Path(directory) / f"history-part-{number}-of-2.log"
                path.write_text(f"part {number}\n", encoding="utf-8")
                paths.append(str(path))
            with patch("trader.telegram.requests.get", side_effect=requests.ReadTimeout("offline")), \
                    patch("trader.telegram.requests.post", side_effect=post), \
                    patch.object(telegram, "_failure_policy", return_value=(False, 0.01)):
                telegram.start()
                telegram.send_log_snapshot(lambda: paths)
                deadline = time.monotonic() + 2
                while (telegram.pending_reports or sum(attempts.values()) < 3) \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
                telegram.stop()

        self.assertEqual(attempts["history-part-1-of-2.log"], 1)
        self.assertEqual(attempts["history-part-2-of-2.log"], 2)
        self.assertEqual(
            sorted((ack["part"], ack["status"]) for ack in telegram.document_acks()),
            [(1, "delivered"), (2, "delivered")],
        )

    def test_one_part_snapshot_is_uploaded_as_plain_log(self):
        telegram = Telegram("token", "123")
        response = Mock()
        uploaded = []

        def post(url, **kwargs):
            if url.endswith("/sendDocument"):
                document = Path(kwargs["files"]["document"].name)
                uploaded.append((document.suffix, document.read_bytes()))
            return response

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history-part-1-of-1.log"
            path.write_text("plain diagnostics\n", encoding="utf-8")
            with patch("trader.telegram.requests.get", side_effect=requests.ReadTimeout("offline")), \
                    patch("trader.telegram.requests.post", side_effect=post):
                telegram.start()
                telegram.send_log_snapshot(lambda: [str(path)])
                deadline = time.monotonic() + 1
                while telegram.pending_reports and time.monotonic() < deadline:
                    time.sleep(0.01)
                telegram.stop()

        self.assertEqual(uploaded, [(".log", b"plain diagnostics\n")])

    def test_telegram_failure_policy_honours_retry_after_and_stops_bad_requests(self):
        telegram = Telegram("token", "123")
        limited_response = Mock(status_code=429, headers={})
        limited_response.json.return_value = {"parameters": {"retry_after": 17}}
        limited = requests.HTTPError("rate limited", response=limited_response)
        self.assertEqual(telegram._failure_policy(limited, 1, document=True), (False, 17))
        self.assertEqual(telegram._failure_policy(limited, 5, document=True), (False, 17))

        bad_response = Mock(status_code=400, headers={})
        bad = requests.HTTPError("bad document", response=bad_response)
        self.assertEqual(telegram._failure_policy(bad, 1, document=True)[0], True)
        self.assertFalse(telegram._failure_policy(requests.Timeout(), 5, document=True)[0])

        header_response = Mock(status_code=429, headers={"Retry-After": "23"})
        header_response.json.return_value = {"parameters": {}}
        header_limited = requests.HTTPError("rate limited", response=header_response)
        self.assertEqual(
            telegram._failure_policy(header_limited, 1, document=True), (False, 23)
        )

    def test_telegram_error_diagnostics_redact_bot_token(self):
        telegram = Telegram("very-secret-token", "123")
        error = requests.RequestException(
            "https://api.telegram.org/botvery-secret-token/sendDocument failed"
        )
        safe = telegram._safe_error(error)
        self.assertNotIn("very-secret-token", safe)
        self.assertIn("<redacted>", safe)


if __name__ == "__main__":
    unittest.main()

class GeneralRecoveryModelTest(unittest.TestCase):
    def settings(self, *, sizes=None, stops=None, target="0.30"):
        return Settings(
            size=D("10"), stop_distance=D("1"), target_profit=D(target),
            scenario_sizes=sizes or (D("10"),) * 9,
            scenario_stop_distances=stops or tuple(map(D, ("1", "1.5", "2", "4", "4", "4", "4", "4", "4"))),
        )

    def begin(self, cfg=None):
        state = CycleState(cycle_id=1, cycle_attempt=1)
        strategy = Strategy(cfg or self.settings(), state)
        strategy.begin(D("4000.50"), D("4000.00"))
        strategy.confirm_initial_fills(D("4000.50"), D("4000.00"))
        return strategy, state

    def test_s1_sl_trigger_sl_trigger_sequence(self):
        strategy, state = self.begin()
        self.assertEqual((state.general_recovery, state.target_value), (D("8"), D("3")))
        strategy.stopped("BUY", D("3999.40"), "buy-sl-1")
        self.assertEqual(state.general_recovery, D("9"))
        self.assertEqual(D(state.pending_recovery[-1]["pending_d_value"]), D("10"))
        self.assertEqual(state.short.take_profit, D("3998.10"))
        strategy.reopened("BUY", D("4000.60"), "buy-2", "buy-open-2")
        self.assertEqual(state.general_recovery, D("20"))
        self.assertEqual(state.scenario, 2)
        self.assertEqual((state.long.stop_distance, state.short.stop_distance), (D("1.5"), D("1.5")))
        strategy.stopped("SELL", D("4001.60"), "sell-sl-2")
        self.assertEqual(state.general_recovery, D("21"))
        strategy.reopened("SELL", D("3999.90"), "sell-3", "sell-open-3")
        self.assertEqual(state.general_recovery, D("37"))
        self.assertEqual((state.long.take_profit, state.short.take_profit),
                         (D("4006.30"), D("3994.20")))

    def test_volume_change_keeps_money_and_uses_new_size_for_trigger_slippage(self):
        cfg = self.settings(sizes=(D("10"), D("20")) + (D("20"),) * 7,
                            stops=(D("4"),) * 9, target="0")
        strategy, state = self.begin(cfg)
        state.general_recovery = state.recovery = D("59")
        strategy._targets_from_entries()
        strategy.stopped("SELL", D("4004.10"), "sell-sl")
        self.assertEqual(state.general_recovery, D("60"))
        self.assertEqual(D(state.pending_recovery[-1]["pending_d_value"]), D("40"))
        strategy.reopened("SELL", D("3999.90"), "sell-2", "sell-open")
        self.assertEqual(state.general_recovery, D("102"))
        self.assertEqual(state.general_recovery / state.long.size, D("10.2"))
        self.assertEqual(state.general_recovery / state.short.size, D("5.1"))
        self.assertEqual((state.long.size, state.short.size), (D("10"), D("20")))
        self.assertEqual((state.long.take_profit, state.short.take_profit),
                         (D("4014.70"), D("3990.80")))

    def test_same_side_can_reopen_repeatedly(self):
        cfg = self.settings(sizes=(D("10"), D("20")) + (D("20"),) * 7,
                            stops=(D("3"),) * 9, target="0")
        strategy, state = self.begin(cfg)
        state.general_recovery = state.recovery = D("60")
        strategy._targets_from_entries()
        strategy.stopped("SELL", D("4003.10"), "sell-sl-a")
        self.assertEqual(state.general_recovery, D("61"))
        strategy.reopened("SELL", D("3999.90"), "sell-b", "sell-open-b")
        self.assertEqual(state.general_recovery, D("93"))
        strategy.stopped("SELL", D("4003.00"), "sell-sl-b")
        strategy.reopened("SELL", D("3999.80"), "sell-c", "sell-open-c")
        self.assertEqual(state.scenario, 3)
        self.assertEqual(state.long.size, D("10"))
        self.assertEqual(state.short.size, D("20"))

    def test_projected_reopen_is_pure_and_excludes_unknown_future_slippage(self):
        strategy, state = self.begin()
        strategy.stopped("BUY", D("3999.40"), "buy-sl")
        before = (state.general_recovery, state.scenario, list(state.recovery_events))
        size, distance, projected = strategy.projected_reopen("BUY")
        self.assertEqual((size, distance, projected), (D("10"), D("1.5"), D("1.9")))
        self.assertEqual((state.general_recovery, state.scenario, state.recovery_events), before)

    def test_double_sl_transfers_each_pending_d_once_and_continuation_spread_once(self):
        strategy, state = self.begin(self.settings(stops=(D("4"),) * 9, target="0"))
        state.general_recovery = state.recovery = D("59")
        strategy._targets_from_entries()
        strategy.stopped("BUY", D("3996.40"), "buy-sl")
        strategy.stopped("SELL", D("4004.20"), "sell-sl")
        self.assertEqual(state.general_recovery, D("62"))
        self.assertEqual(strategy.account_double_sl_pending(), D("80"))
        self.assertEqual(state.general_recovery, D("142"))
        self.assertEqual(strategy.account_double_sl_pending(), D("0"))
        state.cycle_attempt = 2
        strategy.begin_continuation(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, D("142"))
        strategy.confirm_continuation_fills(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, D("147"))

    def test_continuation_spread_uses_actual_pair_size_without_target_again(self):
        cfg = self.settings(sizes=(D("20"),) * 9, stops=(D("4"),) * 9)
        strategy, state = self.begin(cfg)
        state.general_recovery = state.recovery = D("142")
        state.cycle_attempt = 2
        strategy.begin_continuation(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, D("142"))
        strategy.confirm_continuation_fills(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, D("152"))
        self.assertEqual(state.target_value, D("6"))
        self.assertEqual((state.long.take_profit, state.short.take_profit),
                         (D("4012.10"), D("3988.40")))

    def test_scenario_eight_to_nine_accounts_pending_and_trigger_only(self):
        strategy, state = self.begin(self.settings(sizes=(D("20"),) * 9, stops=(D("4"),) * 9))
        state.scenario = 8
        state.general_recovery = state.recovery = D("102")
        state.short.open = True
        state.short.stop_distance = D("4")
        state.short.stop = D("4004")
        strategy.stopped("SELL", D("4004"), "sell-sl-8")
        # Remove zero slippage from the headline example: 102 after the confirmed SL.
        self.assertEqual(state.general_recovery, D("102"))
        strategy.reopened("SELL", D("3999.90"), "sell-9", "sell-open-9")
        self.assertEqual(state.general_recovery, D("184"))
        self.assertEqual((state.scenario, state.phase), (9, "SCENARIO_9_CLOSING"))

    def test_restart_preserves_pending_and_idempotent_reopen(self):
        strategy, state = self.begin()
        strategy.stopped("BUY", D("3999.40"), "buy-sl")
        with tempfile.NamedTemporaryFile() as file:
            state.save(file.name)
            restored = CycleState.load(file.name)
        restored_strategy = Strategy(strategy.cfg, restored)
        restored_strategy.reopened("BUY", D("4000.60"), "buy-2", "open-2")
        restored_strategy.reopened("BUY", D("4000.60"), "buy-2", "open-2")
        self.assertEqual(restored.general_recovery, D("20"))
        self.assertEqual(restored.scenario, 2)

    def test_zero_general_recovery_valid_but_zero_size_rejected(self):
        state = CycleState(general_recovery=D("0"), recovery_model_version=2)
        strategy = Strategy(self.settings(), state)
        state.long = Leg("BUY", D("1"), D("1"), size=D("0"), stop_distance=D("1"))
        state.short = Leg("SELL", D("1"), D("1"), size=D("10"), stop_distance=D("1"))
        with self.assertRaisesRegex(RuntimeError, "size"):
            strategy.refresh_targets()

    def test_profit_override_fixes_target_value_once(self):
        strategy, state = self.begin()
        state.reset()
        state.profit_override = D("0.40")
        state.profit_override_remaining = 200
        strategy.begin(D("4000.50"), D("4000.00"))
        strategy.confirm_initial_fills(D("4000.50"), D("4000.00"))
        self.assertEqual((state.cycle_target_profit, state.target_value), (D("0.40"), D("4")))
        state.cycle_attempt = 2
        strategy.begin_continuation(D("4001"), D("4000"))
        self.assertEqual(state.target_value, D("4"))

    def test_continuation_attempt_result_is_incremental_not_full_cycle_again(self):
        strategy, state = self.begin()
        state.cycle_id = 7
        state.cycle_attempt = 2
        state.active_attempt_id = 2
        state.attempt_history = [{"attempt_id": 1, "cycle_id": 7, "cycle_attempt": 1,
                                  "status": "DOUBLE_SL_CONTINUATION", "result": "-20"}]
        state.attempt_result_total = D("-20")
        state.realized_loss_money = D("30")
        state.cycle_attempt_start_loss_money = D("20")
        state.long.current_entry = D("100")
        state.long.size = D("10")
        bot = Bot.__new__(Bot)
        bot.state, bot.strategy, bot.cfg = state, strategy, strategy.cfg
        bot.continuation = Mock()
        bot.continuation.release = Mock()
        bot._store_report = Mock()
        bot._send_report = Mock()
        with tempfile.NamedTemporaryFile() as file:
            bot.cfg = Settings(
                state_file=file.name, size=D("10"), stop_distance=D("1"),
                scenario_sizes=(D("10"),) * 9, scenario_stop_distances=(D("1"),) * 9,
            )
            strategy.cfg = bot.cfg
            bot._complete_cycle("BUY", D("105"))
        self.assertEqual(state.net_cycle_money, D("20"))
        self.assertEqual(state.attempt_history[-1]["result"], "40")
        self.assertEqual(state.attempt_result_total, D("20"))

    def test_legacy_active_state_is_blocked_without_guessing(self):
        state = CycleState(active=True, recovery_model_version=1, recovery=D("9.5"))
        bot = Bot.__new__(Bot)
        bot.state = state
        with tempfile.NamedTemporaryFile() as file:
            bot.cfg = Settings(
                state_file=file.name, size=D("10"), stop_distance=D("1"),
                scenario_sizes=(D("10"),) * 9, scenario_stop_distances=(D("1"),) * 9,
            )
            bot.strategy = Strategy(bot.cfg, state)
            with self.assertRaisesRegex(RuntimeError, "прежнюю per-leg"):
                bot._migrate_recovery_model()
            restored = CycleState.load(file.name)
        self.assertTrue(restored.manual)
        self.assertEqual(restored.recovery, D("9.5"))
        self.assertEqual(restored.recovery_model_version, 1)

class GeneralRecoveryIntegrationFixTest(unittest.TestCase):
    def cfg(self, sizes=None, stops=None):
        return Settings(
            size=D("10"), target_profit=D("0.30"), stop_distance=D("1"),
            scenario_sizes=sizes or (D("10"),) * 9,
            scenario_stop_distances=stops or (D("1"), D("3")) + (D("4"),) * 7,
        )

    def active(self, cfg=None):
        state = CycleState(cycle_id=90, cycle_attempt=1)
        strategy = Strategy(cfg or self.cfg(), state)
        strategy.begin(D("4000.50"), D("4000.00"))
        strategy.confirm_initial_fills(D("4000.50"), D("4000.00"))
        return strategy, state

    def test_begin_continuation_and_unformed_attempts_do_not_add_projected_spread(self):
        strategy, state = self.active()
        state.general_recovery = D("142")
        state.cycle_attempt = 2
        strategy.begin_continuation(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, D("142"))
        state.long.current_entry = D("4000.55")  # only BUY confirmed
        state.long.entry_confirmation = "positions"
        self.assertEqual(state.general_recovery, D("142"))
        state.cycle_attempt = 3
        strategy.begin_continuation(D("4010.50"), D("4010.00"))
        self.assertEqual(state.general_recovery, D("142"))

    def test_continuation_actual_pair_adds_spread_once(self):
        strategy, state = self.active(self.cfg(sizes=(D("20"),) * 9))
        state.general_recovery = D("142")
        state.cycle_attempt = 2
        strategy.begin_continuation(D("3999"), D("3998"))
        state.long.size = state.short.size = D("20")
        strategy.confirm_continuation_fills(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, D("152"))
        strategy.confirm_continuation_fills(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, D("152"))
        event = next(item for item in state.recovery_events
                     if item["key"] == "continuation-pair:90:2")
        self.assertEqual((event["spread_distance"], event["size"], event["amount"]),
                         ("0.50", "20", "10.00"))

    def test_initial_pair_uses_broker_actual_size(self):
        strategy = Strategy(self.cfg(), CycleState(cycle_id=1, cycle_attempt=1))
        strategy.begin(D("4000.50"), D("4000.00"))
        strategy.state.long.size = strategy.state.short.size = D("12")
        strategy.state.long.size_confirmation = strategy.state.short.size_confirmation = "positions"
        strategy.confirm_initial_fills(D("4000.50"), D("4000.00"))
        state = strategy.state
        self.assertEqual((state.initial_position_size, state.target_value,
                          state.general_recovery), (D("12"), D("3.60"), D("9.60")))

    def test_unequal_pair_sizes_are_safe_and_do_not_change_general_recovery(self):
        strategy, state = self.active()
        before = state.general_recovery
        state.cycle_attempt = 2
        strategy.begin_continuation(D("4000.50"), D("4000.00"))
        state.long.size, state.short.size = D("20"), D("19")
        with self.assertRaisesRegex(RuntimeError, "sizes differ"):
            strategy.confirm_continuation_fills(D("4000.50"), D("4000.00"))
        self.assertEqual(state.general_recovery, before)
        self.assertFalse(any(item["key"] == "continuation-pair:90:2"
                             for item in state.recovery_events))

    def _protection_race(self, direction, new_confirmed):
        strategy, state = self.active()
        closed = state.short if direction == "SELL" else state.long
        survivor = state.long if direction == "SELL" else state.short
        closed.deal_id = direction.lower()
        survivor.deal_id = "survivor"
        old_stop = closed.current_entry + D("1") if direction == "SELL" else closed.current_entry - D("1")
        closed.confirmed_stop = old_stop
        closed.confirmed_stop_distance = D("1")
        closed.protection_readback = "ПОДТВЕРЖДЕНО"
        # Trigger on the other side advances desired D for both legs to 3.
        strategy.stopped(survivor.direction, survivor.stop, "other-stop")
        strategy.reopened(survivor.direction, survivor.original_trigger_level,
                          "other-new", "other-open")
        self.assertEqual(closed.stop_distance, D("3"))
        desired_stop = closed.stop
        closed.protection_sent_stop = desired_stop
        closed.protection_readback = "ожидается"
        closed.protection_confirmation = "ACCEPTED" if new_confirmed else "ожидается"
        closed.confirmation_stop = desired_stop if new_confirmed else None
        fill = desired_stop if new_confirmed else old_stop
        strategy.stopped(direction, fill, f"{direction}-race-stop")
        return state.pending_recovery[-1]

    def test_sell_old_confirmed_d_wins_race_against_unconfirmed_desired_d(self):
        pending = self._protection_race("SELL", False)
        self.assertEqual((pending["stop_distance"], pending["desired_stop_distance"],
                          pending["pending_d_value"], pending["stop_source"]),
                         ("1.00", "3", "10.00", "positions_readback"))

    def test_buy_old_confirmed_d_wins_race_against_unconfirmed_desired_d(self):
        pending = self._protection_race("BUY", False)
        self.assertEqual((pending["stop_distance"], pending["pending_d_value"]),
                         ("1.00", "10.00"))

    def test_new_confirmation_changes_effective_pending_d_before_sl(self):
        pending = self._protection_race("SELL", True)
        self.assertEqual((pending["stop_distance"], pending["pending_d_value"],
                          pending["stop_source"]),
                         ("3.00", "30.00", "confirmation"))

    def test_reporting_uses_general_not_legacy_leg_recovery(self):
        leg = Leg("SELL", D("4000"), D("4000"), size=D("20"), stop_distance=D("4"),
                  recovery=D("0"))
        text = leg_details(leg, general_recovery=D("102"))
        self.assertIn("recovery_distance=5.1", text)
        self.assertNotIn("recovery_distance=0", text)

    def test_reporting_continuation_does_not_claim_initial_formula(self):
        strategy, state = self.active(self.cfg(sizes=(D("20"),) * 9))
        state.general_recovery = D("142"); state.cycle_attempt = 2
        before = recovery_snapshot(state)
        strategy.begin_continuation(D("4000.5"), D("4000"))
        strategy.confirm_continuation_fills(D("4000.5"), D("4000"))
        text = recovery_change_text(state, before, event="continuation pair", direction="BUY")
        self.assertIn("ДО=142", text); self.assertIn("ПОСЛЕ=152.0", text)
        self.assertNotIn("initial spread", text)

    def test_full_scenario_one_through_eight_and_non_alternating_sequence(self):
        cfg = self.cfg(
            sizes=(D("10"), D("10"), D("10"), D("10"), D("20"), D("20"), D("20"), D("20"), D("20")),
            stops=(D("1"), D("1.5"), D("2"), D("3"), D("4"), D("4"), D("4"), D("4"), D("4")),
        )
        strategy, state = self.active(cfg)
        directions = ("SELL", "SELL", "SELL", "BUY", "BUY", "SELL", "BUY")
        for expected_scenario, direction in enumerate(directions, 2):
            leg = state.short if direction == "SELL" else state.long
            survivor = state.long if direction == "SELL" else state.short
            anchor, survivor_entry, before = leg.original_trigger_level, survivor.current_entry, state.general_recovery
            old_size, old_d = leg.size, leg.stop_distance
            sl_fill = leg.stop + (D("0.10") if direction == "SELL" else D("-0.10"))
            strategy.stopped(direction, sl_fill, f"e2e-stop-{expected_scenario}")
            pending = state.pending_recovery[-1]
            self.assertEqual(D(pending["pending_d_value"]), old_size * old_d)
            fill = anchor + (D("-0.10") if direction == "SELL" else D("0.10"))
            strategy.reopened(direction, fill, f"deal-{expected_scenario}", f"e2e-open-{expected_scenario}")
            self.assertEqual(state.scenario, expected_scenario)
            self.assertEqual(leg.original_trigger_level, anchor)
            self.assertEqual(leg.current_entry, fill)
            self.assertEqual(survivor.current_entry, survivor_entry)
            self.assertGreater(state.general_recovery, before)
            self.assertTrue(pending["d_accounted"])
            self.assertEqual(leg.stop_distance, cfg.stop_for(expected_scenario))
            self.assertEqual(survivor.stop_distance, cfg.stop_for(expected_scenario))
            self.assertEqual(leg.take_profit,
                             target_for(direction, fill, leg.stop_distance,
                                        state.general_recovery / leg.size))
        self.assertEqual(state.scenario, 8)
        self.assertEqual(directions[:5], ("SELL", "SELL", "SELL", "BUY", "BUY"))

    def test_named_bot_state_is_removed_or_restored_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot_state.json"
            state = CycleState(general_recovery=D("12")); state.save(str(path))
            path.unlink()
            self.assertFalse(path.exists())
            original = b'{"user":"state"}\n'; path.write_bytes(original)
            backup = path.read_bytes()
            CycleState(general_recovery=D("99")).save(str(path))
            path.write_bytes(backup)
            self.assertEqual(path.read_bytes(), original)
