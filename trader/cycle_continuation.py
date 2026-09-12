"""Exclusive state-machine owner for recovery-cycle continuation attempts.

The normal Bot controller hands ownership to this object after a flat, confirmed double-SL.
Broker I/O and strategy arithmetic remain shared with Bot/Strategy, but only this dispatcher may
advance continuation pause, filter, pair formation, and subsequent cycle events.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import TYPE_CHECKING

from .reporting import cycle_result_text

if TYPE_CHECKING:  # pragma: no cover
    from .app import Bot


LOG = logging.getLogger(__name__)


class CycleContinuation:
    def __init__(self, bot: "Bot") -> None:
        self.bot = bot

    @property
    def state(self):
        return self.bot.state

    def take_ownership(self, stage: str) -> None:
        self.state.continuation_managed = True
        self.state.continuation_stage = stage
        self.state.save(self.bot.cfg.state_file)

    def release(self) -> None:
        self.state.continuation_managed = False
        self.state.continuation_stage = ""

    def tick(self) -> None:
        """Advance exactly one continuation state; normal Bot.tick must not run in parallel."""
        stage = self.state.continuation_stage
        if stage == "RECONCILING":
            self.bot._tick_double_sl_reconciling()
            if self.state.active and self.state.phase != "DOUBLE_SL_RECONCILING":
                self.state.continuation_stage = "ACTIVE"
                self.state.save(self.bot.cfg.state_file)
        elif stage == "PAUSE":
            self.bot._tick_continuation_pause()
        elif stage == "FILTER":
            if not self.state.paused:
                self.bot._tick_filter(self.start_pair)
        elif stage == "PREFLIGHT":
            self._tick_preflight()
        elif stage == "FORMING_PAIR":
            # A persisted INITIAL submission is always resolved before any further POST. This is
            # the critical restart path for attempt 255.
            if any(leg and leg.pending_market_kind for leg in (self.state.long, self.state.short)):
                self.bot._resume_pending_market()
                return
            if self._pair_was_submitted():
                self.state.continuation_stage = "ACTIVE"
                self.state.save(self.bot.cfg.state_file)
                self.handle_active_scenario()
            else:
                self._continue_forming_pair()
        elif stage == "MANUAL_PAIR_PAUSE":
            return
        elif stage == "ACTIVE":
            self.handle_active_scenario()
        else:
            LOG.error("Unknown continuation stage %r; blocking entries", stage)
            self.state.paused = True
            self.state.save(self.bot.cfg.state_file)

    def start_pair(self, filter_reason: str) -> None:
        """Own formation of a repeated pair without invoking initial-cycle state resets."""
        self.state.continuation_stage = "PREFLIGHT"
        self.state.continuation_flat_checks = 0
        self.state.continuation_filter_reason = filter_reason
        self.state.save(self.bot.cfg.state_file)

    def _tick_preflight(self) -> None:
        if self.state.continuation_stopped_by_user:
            return
        positions = self.bot._cycle_positions()
        orders = [item for item in self.bot.capital.working_orders()
                  if self.bot._order_epic(item) == self.bot.cfg.epic]
        if positions or orders:
            self.state.continuation_flat_checks = 0
            self.state.save(self.bot.cfg.state_file)
            return
        self.state.continuation_flat_checks += 1
        self.state.save(self.bot.cfg.state_file)
        if self.state.continuation_flat_checks < 3:
            return
        self.state.continuation_stage = "FORMING_PAIR"
        self.state.save(self.bot.cfg.state_file)
        self._continue_forming_pair()

    def _continue_forming_pair(self) -> None:
        """Retry preparation only while no MARKET submission is pending or was sent."""
        if self.state.continuation_stopped_by_user:
            return
        if any(leg and leg.pending_market_kind for leg in (self.state.long, self.state.short)):
            return
        try:
            self.bot._start_pair_common(
                self.state.continuation_filter_reason or "условие фильтра выполнено",
                continuation=True, preflight_done=True,
            )
        except Exception as exc:
            # Quote/preparation failed before a POST: keep the same attempt and retry next tick.
            self.state.continuation_stage = "FORMING_PAIR"
            self.state.continuation_filter_reason = str(exc)
            self.state.save(self.bot.cfg.state_file)
            LOG.warning("Continuation pair preparation delayed before MARKET POST: %s", exc)

    def _pair_was_submitted(self) -> bool:
        return set(self.state.initial_submitted_directions) == {"BUY", "SELL"}

    def confirm_pair_fills(self) -> None:
        if not self.state.long or not self.state.short:
            raise RuntimeError("Continuation pair legs are missing")
        self.bot.strategy.confirm_continuation_fills(
            self.state.long.current_entry, self.state.short.current_entry
        )

    def handle_fast_second_close(self, closed, source: str, fill: Decimal) -> bool:
        """Replay a fast close using the current scenario, never scenario-1 validation."""
        self.confirm_pair_fills()
        closed.open = True
        stopped = self.bot.strategy.stopped(
            closed.direction, fill, f"stop:{closed.deal_id}:{fill}"
        )
        survivor = self.state.short if closed.direction == "BUY" else self.state.long
        if survivor and survivor.open and self.bot._apply_protection(survivor):
            self.bot._create_trigger(stopped)
        self.state.continuation_stage = "ACTIVE"
        self.state.save(self.bot.cfg.state_file)
        return True

    def handle_active_scenario(self) -> None:
        """Continuation-owned scenario dispatcher for the current scenario (1 through 9)."""
        if self.state.pending_close_reference:
            self.bot._resume_pending_close()
            return
        if self.state.pending_tp_direction and self.state.pending_tp_fill is not None:
            self._finish_take_profit()
            return
        if self.bot._resume_pending_market():
            return
        positions = self.bot._cycle_positions()
        self.bot._detect_trigger_fill(positions)
        if self.state.scenario >= self.bot.cfg.max_scenarios:
            self.bot._enter_manual_nine()
            return
        open_legs = [leg for leg in (self.state.long, self.state.short) if leg and leg.open]
        missing = [leg for leg in open_legs if leg.deal_id not in positions]
        if not missing:
            for leg in open_legs:
                if not self.bot._protection_matches(positions[leg.deal_id], leg):
                    if not self.bot._apply_protection(leg):
                        return
            self.bot._ensure_expected_trigger()
            return
        positions = self.bot._retry_missing_positions(attempts=1, delay=0)
        missing = [leg for leg in open_legs if leg.deal_id not in positions]
        if not missing:
            return
        activity = self.bot.capital.activity()
        closes = []
        for leg in missing:
            tp = self.bot._closing_fill_any_index(leg, "TP", activity)
            sl = None if tp is not None else self.bot._closing_fill_any_index(leg, "SL", activity)
            if tp is None and sl is None:
                return
            closes.append((leg, "TP" if tp is not None else "SL", tp or sl))
        winners = [item for item in closes if item[1] == "TP"]
        if winners:
            winner, _, fill = winners[0]
            if any(leg.deal_id in positions for leg in open_legs if leg is not winner):
                # TP geometry implies the opposite SL, but visibility is not closure evidence.
                return
            for loser, source, loser_fill in closes:
                if loser is not winner and source == "SL" and loser.open:
                    self.bot.strategy.stopped(
                        loser.direction, loser_fill, f"stop:{loser.deal_id}:{loser_fill}"
                    )
            self.state.pending_tp_direction = winner.direction
            self.state.pending_tp_fill = fill
            self.state.save(self.bot.cfg.state_file)
            self._finish_take_profit()
            return
        for leg, _, fill in closes:
            if leg.open:
                self.bot.strategy.stopped(leg.direction, fill, f"stop:{leg.deal_id}:{fill}")
        remaining = [leg for leg in (self.state.long, self.state.short) if leg and leg.open]
        if remaining:
            survivor = remaining[0]
            stopped = self.state.short if survivor.direction == "BUY" else self.state.long
            if survivor.deal_id in positions and self.bot._apply_protection(survivor):
                self.bot._create_trigger(stopped)
            return
        for trigger_leg in (self.state.long, self.state.short):
            if trigger_leg and trigger_leg.trigger_id:
                if not self.bot._resolve_trigger_for_double_stop(trigger_leg, positions):
                    return
        self.bot._begin_double_sl_pause([(leg, fill) for leg, _, fill in closes])

    def _finish_take_profit(self) -> None:
        """Resolve every trigger race before releasing continuation ownership."""
        direction = self.state.pending_tp_direction
        fill = self.state.pending_tp_fill
        if not direction or fill is None:
            return
        winner = self.state.long if direction == "BUY" else self.state.short
        if winner is None:
            return
        try:
            self.bot._cancel_pending_trigger_for_completion(winner)
        except Exception as exc:
            self.state.continuation_stage = "ACTIVE"
            self.state.save(self.bot.cfg.state_file)
            LOG.info("Continuation TP waits for trigger reconciliation: %s", exc)
            return
        self.state.pending_tp_direction = ""
        self.state.pending_tp_fill = None
        self.bot._complete_cycle(direction, fill)
        self.state.armed = not self.state.paused
        self.state.waiting_current_candle = False
        self.state.phase = "FILTER" if self.state.armed else "PAUSED"
        self.state.save(self.bot.cfg.state_file)
        suffix = ("Перехожу к фильтру нового цикла со сценарием 1."
                  if self.state.armed else "Следующий новый цикл ожидает /start.")
        self.bot.telegram.send(
            f"✅ Продолженный цикл завершён по TP {direction}. {suffix}\n"
            f"{cycle_result_text(self.state, direction, fill, self.bot.cfg.size)}"
        )

    def start_pause(self) -> None:
        self.state.continuation_stage = "PAUSE"
        self.state.continuation_pause_until = time.time() + 300
        self.state.save(self.bot.cfg.state_file)

    def filter_ready(self) -> None:
        self.state.continuation_stage = "FILTER"
        self.state.save(self.bot.cfg.state_file)

    def block_unknown(self) -> None:
        self.take_ownership("RECONCILING")

    def finish_single_leg(self, leg, source: str, fill: Decimal) -> None:
        """Persist a failed repeated-pair leg without resetting the parent recovery cycle."""
        event_id = f"continuation-single:{leg.deal_id}:{source}:{fill}"
        if event_id not in self.state.processed_events:
            loss = max(Decimal("0"), leg.current_entry - fill) if leg.direction == "BUY" else max(
                Decimal("0"), fill - leg.current_entry
            )
            self.state.realized_losses += loss
            self.state.processed_events.append(event_id)
            self.state.remember_deal(leg, self.state.scenario)
            self.state.remember_close(leg.deal_id, source, fill)
        result = -(self.state.realized_losses - self.state.cycle_attempt_start_losses) * self.bot.cfg.size
        self.state.remember_attempt(
            "CONTINUATION_PAIR_NOT_FORMED", result, scenario=self.state.scenario,
            direction=leg.direction, deal_id=leg.deal_id, entry=leg.current_entry,
            close=fill, close_source=source, completed_cycle=None,
        )
        self.state.long = self.state.short = None
        self.state.active_attempt_id = 0
        self.state.paused = True
        self.state.armed = False
        self.state.phase = "CONTINUATION_MANUAL_PAIR_PAUSE"
        self.state.continuation_stage = "MANUAL_PAIR_PAUSE"
        self.state.save(self.bot.cfg.state_file)
        self.bot.telegram.send(
            f"⏸ Продолжение цикла №{self.state.cycle_id}: пара не сформирована.\n"
            f"{leg.direction} {leg.deal_id}: вход {leg.current_entry}, {source} {fill}.\n"
            f"Результат попытки: {result}; накопленные потери цикла: "
            f"{self.state.realized_losses * self.bot.cfg.size}.\n"
            "Автоматический повтор отключён. После проверки брокера продолжение — только /start."
        )
