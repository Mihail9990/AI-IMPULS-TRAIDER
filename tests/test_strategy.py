from decimal import Decimal as D
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
from trader.model import CycleState, Leg, stop_slippage, trigger_slippage
from trader.reconcile import RemoteSnapshot
from trader.reporting import cycle_result_text, pnl_text
from trader.streaming import PriceWatch, QuoteStream
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
        self.state.short.pending_market_reference = "market-pending"
        self.state.short.pending_market_reason = "confirmation delayed"
        self.state.attempt_counter = self.state.active_attempt_id = 212
        self.state.attempt_result_total = D("-46.40")
        self.state.attempt_history = [{"attempt_id": 212, "status": "INITIAL_PAIR_NOT_FORMED",
                                       "result": "-15.20"}]
        self.state.initial_submitted_directions = ["BUY", "SELL"]
        self.state.pending_close_direction = "SELL"
        self.state.pending_close_reference = "close-ref"
        self.state.cycle_id = 224
        self.state.cycle_attempt = 2
        self.state.continuation_pause_until = 1789146300.0
        self.state.continuation_stopped_by_user = True
        self.state.cycle_attempt_start_losses = D("14.71")
        self.state.continuation_managed = True
        self.state.continuation_stage = "FORMING_PAIR"
        with tempfile.NamedTemporaryFile() as file:
            self.state.save(file.name)
            restored = CycleState.load(file.name)
        self.assertEqual(restored.long.original_trigger_level, D("4010.30"))
        self.assertEqual(restored.long.current_entry, D("4010.30"))
        self.assertEqual(restored.recovery, D("0.60"))
        self.assertEqual(restored.telegram_offset, 123)
        self.assertEqual(restored.short.pending_market_reference, "market-pending")
        self.assertEqual(restored.short.pending_market_reason, "confirmation delayed")
        self.assertEqual(restored.active_attempt_id, 212)
        self.assertEqual(restored.attempt_result_total, D("-46.40"))
        self.assertEqual(restored.attempt_history[0]["attempt_id"], 212)
        self.assertEqual(restored.initial_submitted_directions, ["BUY", "SELL"])
        self.assertEqual(restored.cycle_id, 224)
        self.assertEqual(restored.cycle_attempt, 2)
        self.assertEqual(restored.continuation_pause_until, 1789146300.0)
        self.assertTrue(restored.continuation_stopped_by_user)
        self.assertEqual(restored.cycle_attempt_start_losses, D("14.71"))
        self.assertTrue(restored.continuation_managed)
        self.assertEqual(restored.continuation_stage, "FORMING_PAIR")
        self.assertEqual(restored.pending_close_reference, "close-ref")

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
        with patch("trader.app.time.sleep"):
            error = bot._open_initial_leg(bot.state.long)
        self.assertIsNone(error)
        self.assertEqual(bot.capital.open_position.call_count, 4)
        self.assertEqual(bot.state.long.current_entry, D("4010.35"))
        kwargs = bot.capital.open_position.call_args.kwargs
        self.assertEqual(kwargs["stop_distance"], D("1"))
        self.assertNotIn("profit_distance", kwargs)

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
        self.assertEqual(bot.state.scenario_nine_total_loss, D("21.2"))
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
        self.assertEqual(bot.state.recovery, D("15.66"))
        self.assertEqual(bot.state.long.original_trigger_level, D("100.50"))
        self.assertEqual(bot.state.short.original_trigger_level, D("100.00"))
        bot._create_trigger.assert_called_once_with(bot.state.short)
        self.assertEqual(bot.state.continuation_stage, "ACTIVE")

    def test_continuation_recovery_uses_losses_target_and_new_spread_once(self):
        bot = self.make_bot()
        bot.state.realized_losses = D("14.71")
        bot.state.cycle_target_profit = D("0.40")
        bot.state.scenario = 8
        bot.state.cycle_attempt = 2
        bot.strategy.begin_continuation(D("4375.20"), D("4375.00"))
        self.assertEqual(bot.state.recovery, D("15.31"))
        self.assertEqual(bot.state.long.original_trigger_level, D("4375.20"))
        self.assertEqual(bot.state.short.original_trigger_level, D("4375.00"))
        self.assertEqual(bot.state.scenario, 8)

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
        self.assertEqual(bot.state.recovery, D("2.02"))
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
        self.assertEqual(bot.state.recovery, D("0.70"))
        self.assertEqual(bot.state.short.trigger_id, "trigger-1")


    def test_two_confirmed_stops_pause_attempt_once(self):
        bot = self.make_bot()
        object.__setattr__(bot.cfg, "size", D("10"))
        bot.state.active_attempt_id = bot.state.attempt_counter = 224
        bot.state.long.deal_id = "buy-224"
        bot.state.short.deal_id = "sell-224"
        bot.state.long.current_entry = D("4372.74")
        bot.state.short.current_entry = D("4371.16")
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

    def test_window_keeps_twenty_completed_cycles_plus_current(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = CycleFileHandler(str(Path(directory) / "bot_diagnostics.log"))
            for cycle in range(1, 23):
                handler.begin_cycle(cycle, cycle - 1)
                self._write(handler, f"cycle {cycle}")
                handler.end_cycle(cycle + 1)
            handler.begin_cycle(23, 22)
            names = [item["name"] for item in handler._index["segments"]]
            handler.close()
        self.assertFalse(any("cycle-000000002.log" in name for name in names))
        self.assertTrue(any("cycle-000000003.log" in name for name in names))
        self.assertTrue(any("cycle-000000023.log" in name for name in names))

    def test_long_cycle_is_not_truncated_and_snapshot_can_split_in_two(self):
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
        self.assertEqual(len(parts), 2)
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


class CapitalClientTest(unittest.TestCase):
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
        bot.command("/settrigger long 4020.50")
        self.assertEqual(bot.state.long.trigger_id, "manual-order")
        self.assertIn("manual-order", bot.state.cycle_trigger_ids)
        bot.capital.working_stop.assert_called_once_with(
            "GOLD", "BUY", D("0.1"), D("4020.50"), D("4019.50"), D("4012.60")
        )

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
                while (telegram.pending_reports or sum(attempts.values()) < 3
                       or not any("полностью доставлен" in message for message in messages)) \
                        and time.monotonic() < deadline:
                    time.sleep(0.01)
                telegram.stop()

        self.assertEqual(attempts["history-part-1-of-2.log"], 1)
        self.assertEqual(attempts["history-part-2-of-2.log"], 2)
        self.assertTrue(any("полностью доставлен" in message for message in messages))

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
        self.assertEqual(telegram._failure_policy(limited, 5, document=True), (True, 17))

        bad_response = Mock(status_code=400, headers={})
        bad = requests.HTTPError("bad document", response=bad_response)
        self.assertEqual(telegram._failure_policy(bad, 1, document=True)[0], True)
        self.assertEqual(
            telegram._failure_policy(requests.Timeout(), 5, document=True)[0], True
        )

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
