from __future__ import annotations

from decimal import Decimal

from .config import Settings
from .model import (
    CycleState, Leg, protection_levels, recovery_distance, stop_for, stop_slippage,
    remaining_recovery_distance, trigger_slippage,
)


class Strategy:
    """Deterministic bookkeeping for the monetary GENERAL_RECOVERY model."""

    MODEL_VERSION = 3

    def __init__(self, settings: Settings, state: CycleState):
        self.cfg, self.state = settings, state

    def _record_recovery(self, key: str, kind: str, amount: Decimal, **details) -> bool:
        """Atomically account a named component once; callers persist the enclosing transition."""
        if any(item.get("key") == key for item in self.state.recovery_events):
            return False
        before = self.state.general_recovery
        self.state.general_recovery += amount
        self.state.recovery_events.append({
            "key": key, "kind": kind, "before": str(before), "amount": str(amount),
            "after": str(self.state.general_recovery),
            **{name: str(value) if isinstance(value, Decimal) else value
               for name, value in details.items()},
        })
        self.state.events.append(
            f"GENERAL_RECOVERY {kind}: key={key}; before={before}; amount={amount}; "
            f"after={self.state.general_recovery}; details={details}"
        )
        return True

    def _set_pair_component(self, key: str, kind: str, amount: Decimal, **details) -> None:
        """Set a pair component from confirmed fills; repeated identical confirmation is inert."""
        existing = next((item for item in self.state.recovery_events if item.get("key") == key), None)
        if existing is None:
            self._record_recovery(key, kind, amount, **details)
            return
        old = Decimal(str(existing["amount"]))
        if old != amount:
            raise RuntimeError(
                f"Conflicting confirmed pair component {key}: saved={old}, received={amount}"
            )

    def _clear_legacy_leg_components(self) -> None:
        for leg in (self.state.long, self.state.short):
            if leg is None:
                continue
            # Legacy fields remain readable for migration only; v2 never stores a derived
            # recovery_distance as a second source of truth.
            leg.recovery = Decimal("0")
            leg.temporary_stop_compensation = Decimal("0")
            leg.temporary_spread_compensation = Decimal("0")
            leg.temporary_slippage_compensation = Decimal("0")

    def begin(self, ask: Decimal, bid: Decimal) -> None:
        if self.state.active:
            raise RuntimeError("A cycle is already active")
        self.state.active = True
        self.state.armed = self.state.waiting_current_candle = False
        self.state.manual = self.state.paused = False
        self.state.scenario = 1
        self.state.realized_losses = self.state.realized_loss_money = Decimal("0")
        self.state.gross_take_profit = self.state.net_cycle_result = Decimal("0")
        self.state.net_cycle_money = Decimal("0")
        self.state.scenario_nine_prior_losses = Decimal("0")
        self.state.scenario_nine_close_gap = Decimal("0")
        self.state.scenario_nine_total_loss = Decimal("0")
        self.state.scenario_nine_extra_loss = Decimal("0")
        self.state.scenario_nine_triggers_verified = False
        self.state.cycle_trigger_ids.clear()
        self.state.scenario_nine_long_fill = self.state.scenario_nine_short_fill = None
        self.state.cycle_target_profit = (
            self.state.profit_override
            if self.state.profit_override is not None and self.state.profit_override_remaining > 0
            else self.cfg.target_profit
        )
        self.state.recovery_model_version = self.MODEL_VERSION
        self.state.general_recovery = Decimal("0")
        self.state.recovery = Decimal("0")
        self.state.recovery_events.clear()
        self.state.pending_recovery.clear()
        self.state.recovery_migration_error = ""
        self.state.phase = "BOTH_OPEN"
        size, distance = self.cfg.size_for(1), self.cfg.stop_for(1)
        self.state.long = Leg("BUY", ask, ask, size=size, stop_distance=distance)
        self.state.short = Leg("SELL", bid, bid, size=size, stop_distance=distance)
        for leg in (self.state.long, self.state.short):
            leg.stop = stop_for(leg.direction, leg.current_entry, leg.stop_distance)

    def confirm_initial_fills(self, long_fill: Decimal, short_fill: Decimal) -> None:
        if not self.state.long or not self.state.short or self.state.scenario != 1:
            raise RuntimeError("Initial legs have not been prepared")
        long_size, short_size = self.state.long.size, self.state.short.size
        if long_size <= 0 or short_size <= 0:
            raise RuntimeError("Initial position size is unknown")
        if long_size != short_size:
            raise RuntimeError(
                f"Initial hedge sizes differ: BUY={long_size}, SELL={short_size}; "
                "GENERAL_RECOVERY was not changed"
            )
        self.state.initial_position_size = long_size
        self.state.target_value = self.state.cycle_target_profit * long_size
        spread = abs(long_fill - short_fill)
        self.state.entry_spread = spread
        key = f"initial-pair:{self.state.cycle_id}:{self.state.cycle_attempt}"
        self._set_pair_component(
            key, "INITIAL_SPREAD_AND_TARGET",
            spread * self.state.initial_position_size + self.state.target_value,
            spread_distance=spread, size=self.state.initial_position_size,
            target_value=self.state.target_value,
        )
        for leg, fill in ((self.state.long, long_fill), (self.state.short, short_fill)):
            leg.original_trigger_level = leg.current_entry = fill
            leg.entry_confirmation = "broker"
            if leg.size_confirmation == "requested":
                leg.size_confirmation = "accepted_request"
            self.state.remember_deal(leg, 1)
        self._targets_from_entries()
        self.state.events.append(
            f"GENERAL_RECOVERY {self.state.general_recovery}; initial spread={spread}; "
            f"target_value={self.state.target_value}"
        )

    def begin_continuation(self, ask: Decimal, bid: Decimal) -> None:
        if not self.state.active or self.state.scenario < 1:
            raise RuntimeError("No recovery cycle is available for continuation")
        size, distance = self.cfg.size_for(self.state.scenario), self.cfg.stop_for(self.state.scenario)
        self.state.long = Leg("BUY", ask, ask, size=size, stop_distance=distance)
        self.state.short = Leg("SELL", bid, bid, size=size, stop_distance=distance)
        # Quotes are projections only. They must never create a monetary pair component.
        self._targets_from_entries()

    def confirm_continuation_fills(self, long_fill: Decimal, short_fill: Decimal) -> None:
        if not self.state.long or not self.state.short:
            raise RuntimeError("Continuation legs have not been prepared")
        self.state.long.current_entry = self.state.long.original_trigger_level = long_fill
        self.state.short.current_entry = self.state.short.original_trigger_level = short_fill
        self.state.long.entry_confirmation = self.state.short.entry_confirmation = "broker"
        for leg in (self.state.long, self.state.short):
            if leg.size_confirmation == "requested":
                leg.size_confirmation = "accepted_request"
        spread = abs(long_fill - short_fill)
        self.state.entry_spread = spread
        # Both confirmed fills are required. A repeated confirmation has the same stable key.
        size, short_size = self.state.long.size, self.state.short.size
        if size <= 0 or short_size <= 0:
            raise RuntimeError("Continuation pair size is unknown")
        if size != short_size:
            raise RuntimeError(
                f"Continuation hedge sizes differ: BUY={size}, SELL={short_size}; "
                "GENERAL_RECOVERY was not changed"
            )
        key = f"continuation-pair:{self.state.cycle_id}:{self.state.cycle_attempt}"
        self._set_pair_component(key, "CONTINUATION_SPREAD", spread * size,
                                 spread_distance=spread, size=size)
        for leg in (self.state.long, self.state.short):
            self.state.remember_deal(leg, self.state.scenario)
        self._targets_from_entries()
        self.state.phase = "BOTH_OPEN"

    def stopped(self, direction: str, fill: Decimal, event_id: str = "", *,
                scenario_at_close: int | None = None,
                broker_execution_time: str = "") -> Leg:
        leg = self._leg(direction)
        if event_id and event_id in self.state.processed_events:
            return leg
        if not leg.open or leg.stop is None:
            raise RuntimeError(f"{direction} is not an open protected leg")
        if leg.size <= 0:
            raise RuntimeError("Closed position size is unknown")
        if (leg.protection_confirmation == "ACCEPTED"
                and leg.confirmation_stop is not None):
            expected_stop = leg.confirmation_stop
            stop_source = "confirmation"
        elif leg.confirmed_stop is not None:
            expected_stop = leg.confirmed_stop
            stop_source = "positions_readback"
        else:
            # The calculated stop is usable only when no conflicting/unconfirmed revision exists.
            if leg.protection_sent_stop is not None and leg.protection_readback != "ПОДТВЕРЖДЕНО":
                raise RuntimeError("Cannot calculate SL slippage from an unconfirmed protection")
            expected_stop = leg.stop
            stop_source = "initial_calculated_without_new_put"
        confirmed_distance = abs(leg.current_entry - expected_stop)
        slip_distance = stop_slippage(direction, expected_stop, fill)
        slip_value = slip_distance * leg.size
        loss = max(Decimal("0"), leg.current_entry - fill) if direction == "BUY" else max(
            Decimal("0"), fill - leg.current_entry
        )
        self.state.realized_losses += loss
        self.state.realized_loss_money += loss * leg.size
        leg.open = False
        self.state.remember_deal(leg)
        self.state.remember_close(leg.deal_id, "SL", fill)
        close_key = event_id or f"stop:{leg.deal_id}:{fill}"
        self._record_recovery(
            f"slippage:{close_key}", "SL_SLIPPAGE", slip_value, deal_id=leg.deal_id,
            distance=slip_distance, size=leg.size,
        )
        close_scenario = self.state.scenario if scenario_at_close is None else scenario_at_close
        d_value = confirmed_distance * leg.size
        d_accounted = close_scenario >= 2
        if d_accounted:
            self._record_recovery(
                f"stop-distance:{close_key}", "STOP_DISTANCE_VALUE", d_value,
                deal_id=leg.deal_id, scenario_at_close=close_scenario,
                distance=confirmed_distance, size=leg.size,
            )
        if not any(item.get("close_key") == close_key for item in self.state.pending_recovery):
            self.state.pending_recovery.append({
                "close_key": close_key, "deal_id": leg.deal_id, "direction": direction,
                "entry": str(leg.current_entry), "size": str(leg.size),
                "stop_distance": str(confirmed_distance), "confirmed_stop": str(expected_stop),
                "desired_stop_distance": str(leg.stop_distance), "desired_stop": str(leg.stop),
                "stop_source": stop_source,
                "close_fill": str(fill), "sl_slippage_distance": str(slip_distance),
                "sl_slippage_value": str(slip_value),
                "pending_d_value": str(d_value), "d_value": str(d_value),
                "scenario_at_close": close_scenario,
                "broker_execution_time": broker_execution_time,
                "original_trigger_anchor": str(leg.original_trigger_level),
                "d_accounted": d_accounted, "reentry_accounted": False,
                "reopen_event_id": "", "trigger_slippage_accounted": False,
            })
        survivor = self._leg("SELL" if direction == "BUY" else "BUY")
        if survivor.open:
            survivor.stop, survivor.take_profit = protection_levels(
                survivor.direction, survivor.current_entry, survivor.stop_distance,
                self.state.general_recovery, survivor.size,
                scenario=self.state.scenario,
            )
        self._clear_legacy_leg_components()
        self.state.phase = "LONG_ONLY" if survivor.direction == "BUY" else "SHORT_ONLY"
        if event_id:
            self.state.processed_events.append(event_id)
        return leg

    def _pending_for(self, direction: str) -> dict:
        candidates = [item for item in self.state.pending_recovery
                      if item.get("direction") == direction
                      and not item.get("reentry_accounted", bool(item.get("reopen_event_id")))]
        if not candidates:
            raise RuntimeError(f"No unaccounted pending D snapshot for {direction}")
        return candidates[-1]

    def reopened(self, direction: str, fill: Decimal, deal_id: str = "", event_id: str = "",
                 actual_size: Decimal | None = None) -> None:
        if self.state.scenario >= self.cfg.max_scenarios:
            raise RuntimeError("Scenario limit reached")
        leg = self._leg(direction)
        if event_id and event_id in self.state.processed_events:
            return
        pending = self._pending_for(direction)
        next_scenario = self.state.scenario + 1
        requested_size = self.cfg.size_for(next_scenario)
        new_size = actual_size if actual_size is not None else requested_size
        new_distance = self.cfg.stop_for(next_scenario)
        if new_size <= 0:
            raise RuntimeError("Reopened position size is unknown")
        anchor = Decimal(str(pending.get("original_trigger_anchor", leg.original_trigger_level)))
        slip_distance = trigger_slippage(direction, anchor, fill)
        slip_value = slip_distance * new_size
        reopen_key = event_id or f"reopen:{pending['close_key']}:{deal_id}:{fill}"
        d_to_add = (Decimal(str(pending.get("d_value", pending["pending_d_value"])))
                    if not pending.get("d_accounted") else Decimal("0"))
        if d_to_add and self._record_recovery(
            f"reopen-d:{reopen_key}", "PENDING_STOP_DISTANCE_VALUE", d_to_add,
            closed_deal_id=pending["deal_id"], reopened_deal_id=deal_id,
        ):
            pending["d_accounted"] = True
        slippage_added = self._record_recovery(
            f"reopen-slippage:{reopen_key}", "TRIGGER_SLIPPAGE", slip_value,
            closed_deal_id=pending["deal_id"], reopened_deal_id=deal_id,
            d_to_add=d_to_add, trigger_slippage_distance=slip_distance,
            trigger_slippage_value=slip_value, new_size=new_size,
        )
        if slippage_added or any(
            item.get("key") == f"reopen-slippage:{reopen_key}"
            for item in self.state.recovery_events
        ):
            pending["reopen_event_id"] = reopen_key
            pending["trigger_slippage_accounted"] = True
            pending["reentry_accounted"] = True
        self.state.scenario = next_scenario
        # New scenario D applies to every position that is actually still open; survivor size and
        # entry never change merely because the scenario advanced.
        for candidate in (self.state.long, self.state.short):
            if candidate and (candidate.open or candidate is leg):
                candidate.stop_distance = new_distance
        leg.size = new_size
        leg.size_confirmation = "broker" if actual_size is not None else "accepted_request"
        leg.entry_confirmation = "broker"
        leg.current_entry = fill
        leg.deal_id = deal_id
        leg.open = True
        leg.trigger_id = leg.trigger_reference = ""
        leg.confirmed_stop = leg.confirmed_take_profit = None
        leg.confirmed_stop_distance = None
        leg.protection_sent_stop = leg.protection_sent_take_profit = None
        leg.confirmation_stop = leg.confirmation_take_profit = None
        leg.protection_confirmation = leg.protection_readback = ""
        self.state.remember_deal(leg, self.state.scenario)
        self._targets_from_entries()
        self.state.phase = "SCENARIO_9_CLOSING" if next_scenario == self.cfg.max_scenarios else "BOTH_OPEN"
        if event_id:
            self.state.processed_events.append(event_id)

    def account_double_sl_pending(self) -> Decimal:
        """Transfer all still-pending D only after the caller has proved broker-flat state."""
        added = Decimal("0")
        for pending in self.state.pending_recovery:
            if pending.get("d_accounted"):
                continue
            value = Decimal(str(pending["pending_d_value"]))
            key = f"double-sl:{pending['close_key']}"
            if self._record_recovery(key, "DOUBLE_SL_PENDING_D", value,
                                     closed_deal_id=pending.get("deal_id", "")):
                pending["d_accounted"] = True
                pending["reopen_event_id"] = key
                added += value
        self._clear_legacy_leg_components()
        return added

    def complete(self, direction: str, fill: Decimal | None = None) -> None:
        leg = self._leg(direction)
        close = fill if fill is not None else leg.take_profit
        if close is not None:
            self.state.remember_deal(leg)
            self.state.remember_close(leg.deal_id, "TP", close)
            gross = close - leg.current_entry if direction == "BUY" else leg.current_entry - close
            self.state.gross_take_profit = max(Decimal("0"), gross)
            self.state.net_cycle_result = self.state.gross_take_profit - self.state.realized_losses
            self.state.net_cycle_money = self.state.gross_take_profit * leg.size - self.state.realized_loss_money
        self.state.events.append(f"cycle completed by {direction} take profit")
        self.state.completed_cycles += 1
        self._consume_profit_override()
        self.state.active = False
        self.state.phase = "COMPLETED"

    def complete_scenario_nine(self, long_fill: Decimal, short_fill: Decimal,
                               extra_loss: Decimal = Decimal("0")) -> None:
        prior_losses = self.state.realized_losses
        close_gap = abs(long_fill - short_fill)
        self.state.scenario_nine_prior_losses = prior_losses
        self.state.scenario_nine_close_gap = close_gap
        self.state.scenario_nine_extra_loss = extra_loss
        self.state.scenario_nine_total_loss = prior_losses + close_gap + extra_loss
        self.state.scenario_nine_long_fill = long_fill
        self.state.scenario_nine_short_fill = short_fill
        self.state.net_cycle_result = -self.state.scenario_nine_total_loss
        self.state.events.append(f"scenario 9 closed; long={long_fill}; short={short_fill}")
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
            if leg.size <= 0 or leg.stop_distance <= 0:
                raise RuntimeError(f"{leg.direction} size/SL distance is unknown")
            leg.stop, leg.take_profit = protection_levels(
                leg.direction, leg.current_entry, leg.stop_distance,
                self.state.general_recovery, leg.size,
                scenario=self.state.scenario,
            )
        self._clear_legacy_leg_components()

    def refresh_targets(self) -> None:
        self._targets_from_entries()

    def recovery_distance_for(self, leg: Leg) -> Decimal | None:
        """Return the displayed/current TP recovery component for this scenario."""
        if self.state.scenario >= self.cfg.max_scenarios:
            return None
        if self.state.scenario == 1:
            return recovery_distance(self.state.general_recovery, leg.size)
        return remaining_recovery_distance(
            self.state.general_recovery, leg.size, leg.stop_distance
        )

    def projected_reopen(self, direction: str) -> tuple[Decimal, Decimal, Decimal]:
        pending = self._pending_for(direction)
        scenario = self.state.scenario + 1
        size, distance = self.cfg.size_for(scenario), self.cfg.stop_for(scenario)
        d_to_add = (Decimal(str(pending.get("d_value", pending["pending_d_value"])))
                    if not pending.get("d_accounted") else Decimal("0"))
        projected_general = self.state.general_recovery + d_to_add
        projected_distance = (
            recovery_distance(projected_general, size)
            if scenario == 1
            else remaining_recovery_distance(projected_general, size, distance)
        )
        return size, distance, projected_distance

    def _leg(self, direction: str) -> Leg:
        leg = self.state.long if direction == "BUY" else self.state.short
        if leg is None:
            raise RuntimeError("Cycle has no such leg")
        if leg.size <= 0 or leg.stop_distance <= 0:
            raise RuntimeError(f"{direction} position parameters are unknown")
        return leg
