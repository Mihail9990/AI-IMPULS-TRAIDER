from __future__ import annotations

from decimal import Decimal

from .config import Settings
from .model import (
    CycleState, Leg, protection_levels, recovery_distance, stop_slippage, trigger_slippage,
)


class Strategy:
    """Deterministic bookkeeping for the monetary GENERAL_RECOVERY model."""

    MODEL_VERSION = 2

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
        if old == amount:
            return
        before = self.state.general_recovery
        self.state.general_recovery += amount - old
        existing.update({
            "before": str(before), "amount": str(amount),
            "after": str(self.state.general_recovery),
            **{name: str(value) if isinstance(value, Decimal) else value
               for name, value in details.items()},
        })
        self.state.events.append(
            f"GENERAL_RECOVERY {kind} confirmed fills: key={key}; before={before}; "
            f"replace={old}->{amount}; after={self.state.general_recovery}"
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
        self.state.initial_position_size = size
        self.state.target_value = self.state.cycle_target_profit * size
        self.state.long = Leg("BUY", ask, ask, size=size, stop_distance=distance)
        self.state.short = Leg("SELL", bid, bid, size=size, stop_distance=distance)
        self.confirm_initial_fills(ask, bid)

    def confirm_initial_fills(self, long_fill: Decimal, short_fill: Decimal) -> None:
        if not self.state.long or not self.state.short or self.state.scenario != 1:
            raise RuntimeError("Initial legs have not been prepared")
        if self.state.initial_position_size <= 0:
            raise RuntimeError("Initial position size is unknown")
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
        self.confirm_continuation_fills(ask, bid)

    def confirm_continuation_fills(self, long_fill: Decimal, short_fill: Decimal) -> None:
        if not self.state.long or not self.state.short:
            raise RuntimeError("Continuation legs have not been prepared")
        self.state.long.current_entry = self.state.long.original_trigger_level = long_fill
        self.state.short.current_entry = self.state.short.original_trigger_level = short_fill
        spread = abs(long_fill - short_fill)
        self.state.entry_spread = spread
        # Both confirmed fills are required. A repeated confirmation has the same stable key.
        size = self.state.long.size
        if size <= 0 or self.state.short.size <= 0:
            raise RuntimeError("Continuation pair size is unknown")
        key = f"continuation-pair:{self.state.cycle_id}:{self.state.cycle_attempt}"
        self._set_pair_component(key, "CONTINUATION_SPREAD", spread * size,
                                 spread_distance=spread, size=size)
        for leg in (self.state.long, self.state.short):
            self.state.remember_deal(leg, self.state.scenario)
        self._targets_from_entries()
        self.state.phase = "BOTH_OPEN"

    def stopped(self, direction: str, fill: Decimal, event_id: str = "") -> Leg:
        leg = self._leg(direction)
        if event_id and event_id in self.state.processed_events:
            return leg
        if not leg.open or leg.stop is None:
            raise RuntimeError(f"{direction} is not an open protected leg")
        if leg.size <= 0:
            raise RuntimeError("Closed position size is unknown")
        expected_stop = leg.confirmed_stop
        if expected_stop is None:
            # The calculated stop is usable only when no conflicting/unconfirmed revision exists.
            if leg.protection_sent_stop is not None and leg.protection_readback != "ПОДТВЕРЖДЕНО":
                raise RuntimeError("Cannot calculate SL slippage from an unconfirmed protection")
            expected_stop = leg.stop
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
        if not any(item.get("close_key") == close_key for item in self.state.pending_recovery):
            self.state.pending_recovery.append({
                "close_key": close_key, "deal_id": leg.deal_id, "direction": direction,
                "entry": str(leg.current_entry), "size": str(leg.size),
                "stop_distance": str(leg.stop_distance), "confirmed_stop": str(expected_stop),
                "close_fill": str(fill), "sl_slippage_distance": str(slip_distance),
                "sl_slippage_value": str(slip_value),
                "pending_d_value": str(leg.stop_distance * leg.size),
                "d_accounted": False, "reopen_event_id": "", "trigger_slippage_accounted": False,
            })
        survivor = self._leg("SELL" if direction == "BUY" else "BUY")
        if survivor.open:
            survivor.stop, survivor.take_profit = protection_levels(
                survivor.direction, survivor.current_entry, survivor.stop_distance,
                self.state.general_recovery, survivor.size,
            )
        self._clear_legacy_leg_components()
        self.state.phase = "LONG_ONLY" if survivor.direction == "BUY" else "SHORT_ONLY"
        if event_id:
            self.state.processed_events.append(event_id)
        return leg

    def _pending_for(self, direction: str) -> dict:
        candidates = [item for item in self.state.pending_recovery
                      if item.get("direction") == direction and not item.get("d_accounted")]
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
        slip_distance = trigger_slippage(direction, leg.original_trigger_level, fill)
        slip_value = slip_distance * new_size
        reopen_key = event_id or f"reopen:{pending['close_key']}:{deal_id}:{fill}"
        amount = Decimal(pending["pending_d_value"]) + slip_value
        if self._record_recovery(
            f"reopen:{reopen_key}", "PENDING_D_AND_TRIGGER_SLIPPAGE", amount,
            closed_deal_id=pending["deal_id"], reopened_deal_id=deal_id,
            pending_d=Decimal(pending["pending_d_value"]), trigger_slippage_distance=slip_distance,
            trigger_slippage_value=slip_value, new_size=new_size,
        ):
            pending["d_accounted"] = True
            pending["reopen_event_id"] = reopen_key
            pending["trigger_slippage_accounted"] = True
        self.state.scenario = next_scenario
        # New scenario D applies to every position that is actually still open; survivor size and
        # entry never change merely because the scenario advanced.
        for candidate in (self.state.long, self.state.short):
            if candidate and (candidate.open or candidate is leg):
                candidate.stop_distance = new_distance
        leg.size = new_size
        leg.current_entry = fill
        leg.deal_id = deal_id
        leg.open = True
        leg.trigger_id = leg.trigger_reference = ""
        leg.confirmed_stop = leg.confirmed_take_profit = None
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
            )
        self._clear_legacy_leg_components()

    def refresh_targets(self) -> None:
        self._targets_from_entries()

    def projected_reopen(self, direction: str) -> tuple[Decimal, Decimal, Decimal]:
        pending = self._pending_for(direction)
        scenario = self.state.scenario + 1
        size, distance = self.cfg.size_for(scenario), self.cfg.stop_for(scenario)
        projected_general = self.state.general_recovery + Decimal(str(pending["pending_d_value"]))
        return size, distance, recovery_distance(projected_general, size)

    def _leg(self, direction: str) -> Leg:
        leg = self.state.long if direction == "BUY" else self.state.short
        if leg is None:
            raise RuntimeError("Cycle has no such leg")
        if leg.size <= 0 or leg.stop_distance <= 0:
            raise RuntimeError(f"{direction} position parameters are unknown")
        return leg
