from __future__ import annotations

from decimal import Decimal

from .config import Settings
from .model import (
    CycleState,
    Leg,
    stop_for,
    target_for,
    stop_slippage,
    trigger_slippage,
)


class Strategy:
    """Deterministic bookkeeping for the nine-scenario recovery cycle."""

    def __init__(self, settings: Settings, state: CycleState):
        self.cfg, self.state = settings, state

    def begin(self, ask: Decimal, bid: Decimal) -> None:
        if self.state.active:
            raise RuntimeError("A cycle is already active")
        self.state.active = True
        self.state.armed = self.state.waiting_current_candle = False
        self.state.manual = self.state.paused = False
        self.state.scenario = 1
        self.state.realized_losses = Decimal("0")
        self.state.gross_take_profit = Decimal("0")
        self.state.net_cycle_result = Decimal("0")
        self.state.scenario_nine_prior_losses = Decimal("0")
        self.state.scenario_nine_close_gap = Decimal("0")
        self.state.scenario_nine_total_loss = Decimal("0")
        self.state.scenario_nine_extra_loss = Decimal("0")
        self.state.scenario_nine_triggers_verified = False
        self.state.cycle_trigger_ids.clear()
        self.state.scenario_nine_long_fill = None
        self.state.scenario_nine_short_fill = None
        self.state.cycle_target_profit = (
            self.state.profit_override
            if self.state.profit_override is not None and self.state.profit_override_remaining > 0
            else self.cfg.target_profit
        )
        self.state.phase = "BOTH_OPEN"
        self.state.long = Leg("BUY", ask, ask)
        self.state.short = Leg("SELL", bid, bid)
        self.confirm_initial_fills(ask, bid)

    def confirm_initial_fills(self, long_fill: Decimal, short_fill: Decimal) -> None:
        if not self.state.long or not self.state.short or self.state.scenario != 1:
            raise RuntimeError("Initial legs have not been prepared")
        raw_gap = long_fill - short_fill
        # The strategy defines spread as the distance between the two actual fills. Direction does
        # not matter: SELL above BUY is a more favorable spread, but its absolute distance is still
        # carried into recovery.
        spread = abs(raw_gap)
        self.state.entry_spread = spread
        target_profit = self.state.cycle_target_profit
        self.state.recovery = spread + target_profit
        for leg, fill in ((self.state.long, long_fill), (self.state.short, short_fill)):
            leg.original_trigger_level = leg.current_entry = fill
            leg.stop = stop_for(leg.direction, fill, self.cfg.stop_distance)
            self.state.remember_deal(leg, 1)
        self._targets_from_entries()
        self.state.events.append(
            f"scenario 1 fills confirmed; raw_gap={raw_gap}; spread={spread}; "
            f"recovery={self.state.recovery}"
        )

    def begin_continuation(self, ask: Decimal, bid: Decimal) -> None:
        """Prepare a fresh pair without resetting the logical recovery cycle."""
        if not self.state.active or self.state.scenario < 1:
            raise RuntimeError("No recovery cycle is available for continuation")
        self.state.long = Leg("BUY", ask, ask)
        self.state.short = Leg("SELL", bid, bid)
        self.confirm_continuation_fills(ask, bid)

    def confirm_continuation_fills(self, long_fill: Decimal, short_fill: Decimal) -> None:
        if not self.state.long or not self.state.short:
            raise RuntimeError("Continuation legs have not been prepared")
        self.state.long.current_entry = self.state.long.original_trigger_level = long_fill
        self.state.short.current_entry = self.state.short.original_trigger_level = short_fill
        spread = abs(long_fill - short_fill)
        self.state.entry_spread = spread
        # realized_losses is authoritative. Rebuilding from it avoids adding losses already
        # represented by the old recovery value, while cycle_target_profit is included once.
        self.state.recovery = self.state.realized_losses + self.state.cycle_target_profit + spread
        for leg in (self.state.long, self.state.short):
            leg.stop = stop_for(leg.direction, leg.current_entry, self.cfg.stop_distance)
            self.state.remember_deal(leg, self.state.scenario)
        self._targets_from_entries()
        self.state.phase = "BOTH_OPEN"
        self.state.events.append(
            f"continuation attempt {self.state.cycle_attempt}; scenario={self.state.scenario}; "
            f"spread={spread}; losses={self.state.realized_losses}; recovery={self.state.recovery}"
        )

    def stopped(self, direction: str, fill: Decimal, event_id: str = "") -> Leg:
        leg = self._leg(direction)
        if event_id and event_id in self.state.processed_events:
            return leg
        if not leg.open or leg.stop is None:
            raise RuntimeError(f"{direction} is not an open protected leg")
        slippage = stop_slippage(direction, leg.stop, fill)
        loss = max(Decimal("0"), leg.current_entry - fill) if direction == "BUY" else max(
            Decimal("0"), fill - leg.current_entry
        )
        self.state.realized_losses += loss
        leg.open = False
        self.state.remember_deal(leg)
        self.state.remember_close(leg.deal_id, "SL", fill)
        self.state.recovery += slippage
        survivor = self._leg("SELL" if direction == "BUY" else "BUY")
        survivor.take_profit = target_for(
            survivor.direction, survivor.current_entry,
            self.cfg.stop_distance, self.state.recovery,
        )
        self.state.phase = "LONG_ONLY" if survivor.direction == "BUY" else "SHORT_ONLY"
        if event_id:
            self.state.processed_events.append(event_id)
        self.state.events.append(
            f"{direction} stopped at {fill}; slippage={slippage}; recovery={self.state.recovery}"
        )
        return leg

    def reopened(self, direction: str, fill: Decimal, deal_id: str = "", event_id: str = "") -> None:
        if self.state.scenario >= self.cfg.max_scenarios:
            raise RuntimeError("Scenario limit reached")
        leg = self._leg(direction)
        if event_id and event_id in self.state.processed_events:
            return
        slippage = trigger_slippage(direction, leg.original_trigger_level, fill)
        self.state.scenario += 1
        self.state.recovery += self.cfg.stop_distance + slippage
        leg.current_entry = fill
        leg.deal_id = deal_id
        leg.open = True
        leg.trigger_id = leg.trigger_reference = ""
        leg.stop = stop_for(direction, fill, self.cfg.stop_distance)
        self.state.remember_deal(leg, self.state.scenario)
        self._targets_from_entries()
        self.state.phase = "BOTH_OPEN"
        if event_id:
            self.state.processed_events.append(event_id)
        if self.state.scenario == self.cfg.max_scenarios:
            self.state.phase = "SCENARIO_9_CLOSING"
        self.state.events.append(
            f"scenario {self.state.scenario}; {direction} reopened at {fill}; "
            f"slippage={slippage}; recovery={self.state.recovery}"
        )

    def complete(self, direction: str, fill: Decimal | None = None) -> None:
        leg = self._leg(direction)
        close = fill if fill is not None else leg.take_profit
        if close is not None:
            self.state.remember_deal(leg)
            self.state.remember_close(leg.deal_id, "TP", close)
            gross = close - leg.current_entry if direction == "BUY" else leg.current_entry - close
            self.state.gross_take_profit = max(Decimal("0"), gross)
            self.state.net_cycle_result = self.state.gross_take_profit - self.state.realized_losses
        self.state.events.append(f"cycle completed by {direction} take profit")
        self.state.completed_cycles += 1
        self._consume_profit_override()
        self.state.active = False
        self.state.phase = "COMPLETED"

    def complete_scenario_nine(
        self, long_fill: Decimal, short_fill: Decimal, extra_loss: Decimal = Decimal("0")
    ) -> None:
        """Finish scenario 9 using actual broker fills from both closing requests."""
        prior_losses = self.state.realized_losses
        close_gap = abs(long_fill - short_fill)
        self.state.scenario_nine_prior_losses = prior_losses
        self.state.scenario_nine_close_gap = close_gap
        self.state.scenario_nine_extra_loss = extra_loss
        self.state.scenario_nine_total_loss = prior_losses + close_gap + extra_loss
        self.state.scenario_nine_long_fill = long_fill
        self.state.scenario_nine_short_fill = short_fill
        self.state.net_cycle_result = -self.state.scenario_nine_total_loss
        self.state.events.append(
            f"scenario 9 closed; long={long_fill}; short={short_fill}; "
            f"prior_losses={prior_losses}; gap={close_gap}; extra_loss={extra_loss}; "
            f"total_loss={self.state.scenario_nine_total_loss}"
        )
        self.state.completed_cycles += 1
        self._consume_profit_override()
        self.state.active = False
        self.state.manual = False
        self.state.phase = "COMPLETED"

    def _consume_profit_override(self) -> None:
        if self.state.profit_override is None or self.state.profit_override_remaining <= 0:
            return
        if self.state.cycle_target_profit != self.state.profit_override:
            return
        self.state.profit_override_remaining -= 1
        if self.state.profit_override_remaining == 0:
            self.state.profit_override = None

    def _targets_from_entries(self) -> None:
        if not self.state.long or not self.state.short:
            raise RuntimeError("Both legs are required")
        for leg in (self.state.long, self.state.short):
            leg.take_profit = target_for(
                leg.direction, leg.current_entry,
                self.cfg.stop_distance, self.state.recovery,
            )

    def refresh_targets(self) -> None:
        """Recalculate automatic TP levels without changing Recovery or scenario state."""
        self._targets_from_entries()

    def _leg(self, direction: str) -> Leg:
        leg = self.state.long if direction == "BUY" else self.state.short
        if leg is None:
            raise RuntimeError("Cycle has no such leg")
        return leg
