from __future__ import annotations

import importlib.util
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trader.app import Bot
from trader.config import Settings
from trader.engine import Strategy
from trader.execution import trigger_level_passed
from trader.model import CycleState, Leg, protection_levels

D = Decimal


def settings(path: str = "state.sqlite3") -> Settings:
    return Settings(
        dry_run=True, state_file=path, target_profit=D("0.30"),
        scenario_sizes=tuple(D(x) for x in ("10", "10", "10", "20", "20", "20", "20", "20", "20")),
        scenario_stop_distances=tuple(D(x) for x in ("1", "2", "3", "4", "5", "6", "7", "8", "9")),
    )


def initial(path: str = "state.sqlite3") -> tuple[Strategy, CycleState]:
    state = CycleState(cycle_id=1, active_attempt_id=1, cycle_attempt=1)
    strategy = Strategy(settings(path), state)
    strategy.begin(D("4142.54"), D("4141.95"))
    state.long.deal_id, state.short.deal_id = "buy-1", "sell-1"
    state.long.size_confirmation = state.short.size_confirmation = "broker"
    strategy.confirm_initial_fills(D("4142.54"), D("4141.95"))
    return strategy, state


class V33MoneyTest(unittest.TestCase):
    def test_initial_pair_uses_opposite_expected_loss_and_money_target(self):
        _, state = initial()
        self.assertEqual(state.strategy_version, "V3.3")
        self.assertEqual(state.target_value, D("3.00"))
        self.assertEqual(state.long.stop, D("4141.54"))
        self.assertEqual(state.short.stop, D("4142.95"))
        self.assertEqual(state.long.take_profit, D("4143.84"))
        self.assertEqual(state.short.take_profit, D("4140.65"))
        self.assertEqual(state.actual_cycle_loss, 0)
        self.assertEqual(len(state.projected_losses), 2)

    def test_first_sell_sl_does_not_advance_scenario_and_trigger_anchor_is_buy_sl(self):
        strategy, state = initial()
        closed = strategy.stopped("SELL", D("4142.95"), "sell-sl", actual_closed_size=D("10"))
        self.assertEqual(state.scenario, 1)
        self.assertEqual(state.actual_cycle_loss, D("10"))
        self.assertEqual(closed.original_trigger_level, state.long.stop)
        self.assertEqual(closed.original_trigger_level, D("4141.54"))
        self.assertEqual(state.long.take_profit, D("4143.84"))

    def test_first_buy_sl_is_mirrored(self):
        strategy, state = initial()
        closed = strategy.stopped("BUY", D("4141.54"), "buy-sl", actual_closed_size=D("10"))
        self.assertEqual(state.scenario, 1)
        self.assertEqual(closed.original_trigger_level, state.short.stop)
        self.assertEqual(closed.original_trigger_level, D("4142.95"))
        self.assertEqual(state.short.take_profit, D("4140.65"))

    def test_full_sell_first_chain_matches_control_table(self):
        strategy, state = initial()
        strategy.stopped("SELL", D("4142.95"), "sl-1-sell", actual_closed_size=D("10"))
        strategy.stopped("BUY", D("4141.54"), "sl-1-buy", actual_closed_size=D("10"))
        table = [
            (2, "SELL", "4141.54", "4143.54", "4139.24", "20"),
            (3, "BUY",  "4143.54", "4140.54", "4147.84", "40"),
            (4, "SELL", "4140.54", "4144.54", "4136.89", "70"),
            (5, "BUY",  "4144.54", "4139.54", "4152.19", "150"),
            (6, "SELL", "4139.54", "4145.54", "4126.89", "250"),
            (7, "BUY",  "4145.54", "4138.54", "4164.19", "370"),
            (8, "SELL", "4138.54", "4146.54", "4112.89", "510"),
            (9, "BUY",  "4146.54", "4137.54", "4180.19", "670"),
        ]
        for scenario, direction, entry, stop, tp, loss_before in table:
            with self.subTest(scenario=scenario):
                self.assertEqual(state.actual_cycle_loss, D(loss_before))
                leg = state.long if direction == "BUY" else state.short
                strategy.reopened(direction, D(entry), f"deal-{scenario}", f"open-{scenario}",
                                  actual_size=settings().size_for(scenario),
                                  working_order_id=f"trigger-{scenario}")
                self.assertEqual(state.scenario, scenario)
                self.assertEqual(leg.size, settings().size_for(scenario))
                self.assertEqual(leg.stop, D(stop))
                self.assertEqual(leg.take_profit, D(tp))
                if scenario < 9:
                    strategy.stopped(direction, D(stop), f"sl-{scenario}",
                                     actual_closed_size=leg.size)
        self.assertEqual(state.actual_cycle_loss, D("670"))
        strategy.stopped("BUY", D("4137.54"), "sl-9", actual_closed_size=D("20"))
        self.assertEqual(state.actual_cycle_loss, D("850"))
        self.assertEqual(state.actual_cycle_pnl, D("-850"))

    def test_full_buy_first_chain_is_mirrored_and_advances_once_per_fill(self):
        strategy, state = initial()
        strategy.stopped("BUY", state.long.stop, "buy-first", actual_closed_size=D("10"))
        strategy.stopped("SELL", state.short.stop, "sell-second", actual_closed_size=D("10"))
        direction = "BUY"
        for scenario in range(2, 10):
            leg = state.long if direction == "BUY" else state.short
            entry = (state.short.stop if direction == "BUY" else state.long.stop)
            strategy.reopened(direction, entry, f"mirror-{scenario}", f"mirror-open-{scenario}",
                              actual_size=settings().size_for(scenario))
            self.assertEqual(state.scenario, scenario)
            self.assertEqual(leg.direction, direction)
            if scenario < 9:
                strategy.stopped(direction, leg.stop, f"mirror-sl-{scenario}",
                                 actual_closed_size=leg.size)
            direction = "SELL" if direction == "BUY" else "BUY"

    def test_tp_distance_can_be_less_than_stop_distance(self):
        strategy, state = initial()
        state.actual_cycle_loss = state.general_recovery = D("70")
        state.projected_losses.clear(); state.scenario = 4
        leg = state.short
        leg.open, leg.current_entry, leg.size, leg.stop_distance = True, D("4140.54"), D("20"), D("4")
        state.long.open = False
        strategy.refresh_targets()
        self.assertEqual(leg.stop, D("4144.54"))
        self.assertEqual(leg.take_profit, D("4136.89"))
        self.assertLess(leg.current_entry - leg.take_profit, leg.stop_distance)

    def test_actual_fill_slippage_is_only_in_actual_loss(self):
        strategy, state = initial()
        strategy.stopped("SELL", D("4142.98"), "sell-worse", actual_closed_size=D("10"))
        strategy.stopped("BUY", D("4141.49"), "buy-worse", actual_closed_size=D("10"))
        strategy.reopened("SELL", D("4141.46"), "sell-s2", "s2-open", actual_size=D("10"))
        self.assertEqual(state.actual_cycle_loss, D("20.80"))
        self.assertEqual(state.short.stop, D("4143.46"))
        self.assertEqual(state.short.take_profit, D("4139.08"))
        self.assertFalse(any(e.get("kind") in {"TRIGGER_SLIPPAGE", "SL_SLIPPAGE"}
                             for e in state.recovery_events))

    def test_delayed_actual_replaces_projection_not_adds_on_top(self):
        strategy, state = initial()
        strategy.stopped("SELL", state.short.stop, "sell-sl", actual_closed_size=D("10"))
        # Trigger position is published before BUY close evidence.
        strategy.reopened("SELL", state.long.stop, "sell-s2", "s2-open", actual_size=D("10"))
        self.assertEqual(state.short.take_profit, D("4139.24"))
        strategy.stopped("BUY", D("4141.49"), "buy-late", actual_closed_size=D("10"))
        self.assertEqual(state.actual_cycle_loss, D("20.50"))
        self.assertEqual(state.short.take_profit, D("4139.19"))
        projection = next(p for p in state.projected_losses if p["deal_id"] == "buy-1")
        self.assertTrue(projection["replaced"])
        self.assertEqual(D(projection["correction_money"]), D("0.50"))
        strategy.stopped("BUY", D("4141.49"), "buy-late", actual_closed_size=D("10"))
        self.assertEqual(state.actual_cycle_loss, D("20.50"))

    def test_better_actual_loss_reduces_target(self):
        strategy, state = initial()
        strategy.stopped("SELL", state.short.stop, "sell-sl", actual_closed_size=D("10"))
        strategy.reopened("SELL", state.long.stop, "sell-s2", "s2-open", actual_size=D("10"))
        strategy.stopped("BUY", D("4141.59"), "buy-better", actual_closed_size=D("10"))
        self.assertEqual(state.actual_cycle_loss, D("19.50"))
        self.assertEqual(state.short.take_profit, D("4139.29"))

    def test_s4_s5_delayed_example(self):
        strategy, state = initial()
        state.actual_cycle_loss = state.general_recovery = D("40")
        state.target_value = D("3")
        state.projected_losses = [{"deal_id": "s3", "amount": "30", "replaced": False}]
        state.scenario = 4
        state.long.open = False
        state.short = Leg("SELL", D("4140.54"), D("4140.54"), deal_id="s4",
                          size=D("20"), stop_distance=D("4"), stop=D("4144.54"))
        strategy._remember_projection(state.short, 4)
        strategy.refresh_targets()
        self.assertEqual(state.short.take_profit, D("4136.89"))
        size, distance, remainder = strategy.projected_reopen("BUY")
        self.assertEqual((size, distance, distance + remainder), (D("20"), D("5"), D("7.65")))
        old = next(p for p in state.projected_losses if p["deal_id"] == "s3")
        old.update(replaced=True, actual_loss="31")
        state.actual_cycle_loss = state.general_recovery = D("71")
        strategy.refresh_targets()
        self.assertEqual(state.short.current_entry - state.short.take_profit, D("3.70"))
        size, distance, remainder = strategy.projected_reopen("BUY")
        self.assertEqual(distance + remainder, D("7.70"))
        self.assertEqual(state.short.stop, D("4144.54"))
        self.assertEqual(state.scenario, 4)

    def test_target_profit_records_actual_pnl_only(self):
        strategy, state = initial()
        strategy.stopped("SELL", state.short.stop, "sell-sl", actual_closed_size=D("10"))
        strategy.complete("BUY", state.long.take_profit)
        self.assertEqual(state.actual_cycle_pnl, D("3.00"))
        self.assertEqual(state.net_cycle_money, D("3.00"))

    def test_s9_has_no_scenario_ten(self):
        strategy, state = initial()
        state.scenario = 9
        with self.assertRaisesRegex(RuntimeError, "Scenario 10"):
            strategy.projected_reopen("SELL")
        with self.assertRaisesRegex(RuntimeError, "Scenario 10"):
            strategy.reopened("SELL", D("1"), "x", "x")

    def test_protection_helper_has_no_old_stop_minimum(self):
        stop, tp = protection_levels("SELL", D("100"), D("4"), D("73"), D("20"), scenario=4)
        self.assertEqual(stop, D("104"))
        self.assertEqual(tp, D("96.35"))


class V33DurabilityTest(unittest.TestCase):
    def test_cycle_configuration_and_projections_survive_sqlite_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            strategy, state = initial(path)
            strategy.stopped("SELL", state.short.stop, "sell-sl", actual_closed_size=D("10"))
            state.save(path)
            restored = CycleState.load(path)
            self.assertEqual(restored.cycle_strategy_version, "V3.3")
            self.assertEqual(restored.cycle_scenario_sizes[3], "20")
            self.assertEqual(restored.actual_cycle_loss, D("10.00"))
            self.assertEqual(len(restored.projected_losses), 2)
            Strategy(settings(path), restored).stopped(
                "SELL", restored.short.stop, "sell-sl", actual_closed_size=D("10"))
            self.assertEqual(restored.actual_cycle_loss, D("10.00"))

    def test_changed_config_does_not_change_active_cycle_snapshot(self):
        strategy, state = initial()
        changed = Settings(target_profit=D("99"), scenario_sizes=(D("1"),) * 9,
                           scenario_stop_distances=(D("9"),) * 9)
        resumed = Strategy(changed, state)
        self.assertEqual(resumed._size_for(4), D("20"))
        self.assertEqual(resumed._stop_for(4), D("4"))
        self.assertEqual(state.target_value, D("3.00"))

    def test_legacy_continuation_fields_are_readable_but_not_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            state = CycleState()
            state.save(path)
            from trader.storage import StateStore
            payload = StateStore(path).load()
            payload.update(continuation_managed=True, continuation_stage="PAUSE",
                           continuation_pause_until=123)
            StateStore(path).save(payload)
            restored = CycleState.load(path)
            self.assertFalse(hasattr(restored, "continuation_managed"))

    def test_cycle_continuation_module_is_removed(self):
        self.assertIsNone(importlib.util.find_spec("trader.cycle_continuation"))


class V33ExecutionAndBotTest(unittest.TestCase):
    def bot(self) -> Bot:
        bot = Bot.__new__(Bot)
        bot.cfg = settings()
        bot.state = CycleState(active=True, cycle_id=7, active_attempt_id=7,
                               cycle_attempt=1, scenario=3)
        bot.state.cycle_scenario_sizes = [str(bot.cfg.size_for(i)) for i in range(1, 10)]
        bot.state.cycle_stop_distances = [str(bot.cfg.stop_for(i)) for i in range(1, 10)]
        bot.state.target_value = D("3")
        bot.state.long = Leg("BUY", D("99"), D("100"), deal_id="old-buy", open=False,
                             size=D("10"), stop_distance=D("3"))
        bot.state.short = Leg("SELL", D("99"), D("99"), deal_id="survivor", open=True,
                              size=D("10"), stop_distance=D("3"), stop=D("102"))
        bot.state.projected_losses = [{"deal_id": "survivor", "direction": "SELL",
                                      "amount": "30", "replaced": False}]
        bot.strategy = Strategy(bot.cfg, bot.state)
        bot.execution_policy = SimpleNamespace(attempts=4)
        bot.capital = Mock()
        bot.telegram = Mock()
        bot._send_report = Mock()
        bot._manual = Mock()
        return bot

    def test_quote_sides_for_crossed_stop_entry(self):
        self.assertTrue(trigger_level_passed("BUY", D("101"), D("100"), D("101")))
        self.assertFalse(trigger_level_passed("BUY", D("101"), D("101"), D("100.9")))
        self.assertTrue(trigger_level_passed("SELL", D("99"), D("99"), D("100")))
        self.assertFalse(trigger_level_passed("SELL", D("99"), D("99.1"), D("98")))

    def test_production_trigger_request_uses_confirmed_survivor_sl_and_s4_size(self):
        bot = self.bot()
        bot.state.long.original_trigger_level = bot.state.short.stop = D("102")
        bot.state.pending_recovery = [{"deal_id": "old-buy", "direction": "BUY",
                                       "trigger_id": "", "reentry_accounted": False}]
        bot.state.save = Mock(); bot._find_order = Mock(return_value=None)
        bot.capital.working_stop.return_value = "ref-4"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "ACCEPTED", "dealId": "order-4"}
        bot._create_trigger(bot.state.long)
        args = bot.capital.working_stop.call_args.args
        self.assertEqual(args[2], D("20"))
        self.assertEqual(args[3], D("102"))
        self.assertEqual(args[4], D("98"))
        self.assertEqual(bot.state.scenario, 3)
        self.assertEqual(bot.state.long.trigger_id, "order-4")

    def test_rejected_trigger_before_level_does_not_send_market(self):
        bot = self.bot()
        bot.state.long.original_trigger_level = D("102")
        bot.state.pending_recovery = [{"deal_id": "old-buy", "direction": "BUY",
                                       "trigger_id": "", "reentry_accounted": False}]
        bot.state.save = Mock(); bot._find_order = Mock(return_value=None)
        bot.capital.working_stop.return_value = "ref"
        bot.capital.wait_confirmation.return_value = {
            "dealStatus": "REJECTED", "reason": "error.invalid.level"}
        bot._trigger_level_passed = Mock(return_value=False)
        bot._open_passed_trigger_at_market = Mock()
        bot._create_trigger(bot.state.long)
        bot._open_passed_trigger_at_market.assert_not_called()
        self.assertEqual(bot.state.scenario, 3)

    def test_market_rejection_schedules_same_scenario_after_sixty_seconds(self):
        bot = self.bot()
        with patch("trader.app.time.time", return_value=100):
            bot._schedule_transition_retry(bot.state.long, "rejected")
        retry = bot.state.transition_retry
        self.assertEqual(retry["expected_scenario"], 4)
        self.assertEqual(retry["direction"], "BUY")
        self.assertEqual(retry["size"], "20")
        self.assertEqual(retry["deadline"], 160)
        self.assertEqual(bot.state.scenario, 3)

    def test_retry_timer_does_not_submit_early(self):
        bot = self.bot(); bot.state.transition_retry = {
            "cycle_id": 7, "attempt_id": 7, "direction": "BUY", "expected_scenario": 4,
            "size": "20", "stop_distance": "4", "tp_distance": "3.65",
            "trigger_level": "99", "deadline": 160, "reason": "rejected", "state": "PAUSE"}
        bot._open_passed_trigger_at_market = Mock()
        self.assertTrue(bot._tick_transition_retry(now=159))
        bot._open_passed_trigger_at_market.assert_not_called()

    def test_retry_after_deadline_reconciles_then_submits_once(self):
        bot = self.bot(); bot.state.transition_retry = {
            "cycle_id": 7, "attempt_id": 7, "direction": "BUY", "expected_scenario": 4,
            "size": "20", "stop_distance": "4", "tp_distance": "3.65",
            "trigger_level": "99", "deadline": 160, "reason": "rejected", "state": "PAUSE"}
        bot._cycle_positions = Mock(return_value={}); bot.capital.working_orders.return_value = []
        bot._open_passed_trigger_at_market = Mock()
        self.assertTrue(bot._tick_transition_retry(now=160))
        bot._cycle_positions.assert_called_once_with()
        bot.capital.working_orders.assert_called_once_with()
        bot._open_passed_trigger_at_market.assert_called_once()
        self.assertEqual(bot.state.scenario, 3)

    def test_unknown_market_operation_blocks_timer_duplicate(self):
        bot = self.bot(); bot.state.long.pending_market_kind = "FALLBACK"
        bot.state.transition_retry = {"cycle_id": 7, "expected_scenario": 4,
            "direction": "BUY", "deadline": 0}
        bot._open_passed_trigger_at_market = Mock()
        self.assertTrue(bot._tick_transition_retry(now=100))
        bot._open_passed_trigger_at_market.assert_not_called()

    def test_completion_intent_cancels_retry(self):
        bot = self.bot(); bot.state.completion_intent = {"reason": "TP"}
        bot.state.transition_retry = {"cycle_id": 7, "expected_scenario": 4,
            "direction": "BUY", "deadline": 0}
        self.assertFalse(bot._tick_transition_retry(now=100))
        self.assertEqual(bot.state.transition_retry, {})

    def test_stop_keeps_active_transition_running(self):
        bot = self.bot(); bot.state.transition_retry = {"deadline": 100}
        bot.state.save = Mock()
        bot.command("/stop")
        self.assertTrue(bot.state.paused)
        self.assertTrue(bot.state.active)
        self.assertEqual(bot.state.transition_retry, {"deadline": 100})

    def test_s9_handler_protects_position_and_creates_no_trigger(self):
        bot = self.bot(); bot.state.scenario = 9
        bot.state.long.open = True; bot.state.short.open = False
        bot.state.long.deal_id = "s9"; bot.state.long.size = D("20")
        bot.state.long.stop_distance = D("9"); bot.state.long.stop = D("91")
        bot._apply_protection = Mock(return_value=True)
        bot._enter_manual_nine()
        bot._apply_protection.assert_called_once_with(bot.state.long)
        self.assertEqual(bot.state.phase, "LONG_ONLY")
        self.assertEqual(bot.state.long.trigger_id, "")

    def test_tp_completion_intent_is_durable_and_retry_is_removed(self):
        bot = self.bot(); bot.state.scenario = 3
        bot.state.short.open = True; bot.state.short.take_profit = D("96")
        bot.state.transition_retry = {"deadline": 10}
        bot.state.remember_attempt = Mock()
        bot.state.save = Mock()
        bot._resume_normal_finalization = Mock()
        bot.strategy.complete = Mock(side_effect=lambda *_: setattr(bot.state, "active", False))
        bot._complete_cycle("SELL", D("96"))
        self.assertEqual(bot.state.completion_intent["reason"], "TP")
        self.assertEqual(bot.state.transition_retry, {})
        bot.state.save.assert_called()


if __name__ == "__main__":
    unittest.main()
