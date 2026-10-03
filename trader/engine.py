from __future__ import annotations

from decimal import Decimal

from .config import Settings
from .model import CycleState, Leg, recovery_distance, stop_for, target_for

D = Decimal


class Strategy:
    """V3.3 monetary strategy.

    Broker mutations live in :mod:`trader.app`; this class is the deterministic, replay-safe
    owner of scenario and money calculations.  Confirmed loss, expected loss and target money are
    deliberately distinct persisted components.
    """

    MODEL_VERSION = 4
    STRATEGY_VERSION = "V3.3"

    def __init__(self, settings: Settings, state: CycleState):
        self.cfg, self.state = settings, state

    def _size_for(self, scenario: int) -> D:
        values = self.state.cycle_scenario_sizes
        return D(values[scenario - 1]) if values else self.cfg.size_for(scenario)

    def _stop_for(self, scenario: int) -> D:
        values = self.state.cycle_stop_distances
        return D(values[scenario - 1]) if values else self.cfg.stop_for(scenario)

    def begin(self, ask: D, bid: D) -> None:
        if self.state.active:
            raise RuntimeError("A cycle is already active")
        s = self.state
        s.active, s.armed, s.waiting_current_candle = True, False, False
        s.manual = s.paused = False
        s.scenario = 1
        s.strategy_version = s.cycle_strategy_version = self.STRATEGY_VERSION
        s.cycle_scenario_sizes = [str(self.cfg.size_for(i)) for i in range(1, 10)]
        s.cycle_stop_distances = [str(self.cfg.stop_for(i)) for i in range(1, 10)]
        s.recovery_model_version = self.MODEL_VERSION
        s.actual_cycle_loss = s.actual_cycle_pnl = D("0")
        s.general_recovery = s.recovery = D("0")
        s.realized_losses = s.realized_loss_money = D("0")
        s.gross_take_profit = s.net_cycle_result = s.net_cycle_money = D("0")
        s.projected_losses.clear(); s.recovery_events.clear(); s.pending_recovery.clear()
        s.scenario_transitions.clear(); s.trigger_race_results.clear()
        s.deal_history.clear(); s.attempt_history.clear()
        s.completed_cycle_report = s.recovery_migration_error = ""
        s.transition_retry.clear(); s.completion_intent.clear()
        s.cycle_target_profit = (s.profit_override if s.profit_override is not None
                                 and s.profit_override_remaining > 0 else self.cfg.target_profit)
        s.phase = "BOTH_OPEN"
        size, distance = self._size_for(1), self._stop_for(1)
        s.long = Leg("BUY", ask, ask, size=size, stop_distance=distance)
        s.short = Leg("SELL", bid, bid, size=size, stop_distance=distance)
        for leg in (s.long, s.short):
            leg.stop = stop_for(leg.direction, leg.current_entry, distance)

    def _expected(self, leg: Leg) -> D:
        stop = leg.confirmation_stop or leg.confirmed_stop or leg.stop
        if stop is None or leg.size <= 0:
            raise RuntimeError("Expected loss requires confirmed/calculated SL and actual size")
        return max(D("0"), (leg.current_entry - stop if leg.direction == "BUY"
                             else stop - leg.current_entry) * leg.size)

    def _projection(self, deal_id: str) -> dict | None:
        return next((p for p in self.state.projected_losses
                     if p.get("deal_id") == deal_id and not p.get("replaced")), None)

    def _remember_projection(self, leg: Leg, scenario: int) -> dict:
        existing = self._projection(leg.deal_id)
        amount = self._expected(leg)
        if existing:
            if D(str(existing["amount"])) != amount:
                raise RuntimeError("Confirmed protection conflicts with saved expected loss")
            return existing
        item = {"key": f"expected:{self.state.cycle_id}:{leg.deal_id}",
                "cycle_id": self.state.cycle_id, "attempt_id": self.state.active_attempt_id,
                "deal_id": leg.deal_id, "direction": leg.direction, "scenario": scenario,
                "entry": str(leg.current_entry), "size": str(leg.size),
                "confirmed_stop": str(leg.confirmation_stop or leg.confirmed_stop or leg.stop),
                "amount": str(amount), "replaced": False, "actual_loss": None,
                "close_event_id": ""}
        self.state.projected_losses.append(item)
        return item

    def _outstanding(self, *, exclude_deal_id: str = "") -> D:
        return sum((D(str(p["amount"])) for p in self.state.projected_losses
                    if not p.get("replaced") and p.get("deal_id") != exclude_deal_id), D("0"))

    def _tp_money_for(self, leg: Leg) -> D:
        # An open position never covers its own future SL; it covers only confirmed losses and
        # unresolved projections of earlier outgoing positions.
        return self.state.actual_cycle_loss + self._outstanding(exclude_deal_id=leg.deal_id) + self.state.target_value

    def _set_protection(self, leg: Leg) -> None:
        if leg.size <= 0:
            raise RuntimeError("Actual position size is unknown")
        leg.stop = stop_for(leg.direction, leg.current_entry, leg.stop_distance)
        tp_distance = self._tp_money_for(leg) / leg.size
        leg.take_profit = target_for(leg.direction, leg.current_entry, D("0"), tp_distance)

    def confirm_initial_fills(self, long_fill: D, short_fill: D) -> None:
        if not self.state.long or not self.state.short or self.state.scenario != 1:
            raise RuntimeError("Initial legs have not been prepared")
        if self.state.long.size <= 0 or self.state.long.size != self.state.short.size:
            raise RuntimeError("Initial hedge requires equal confirmed non-zero sizes")
        self.state.initial_position_size = self.state.long.size
        self.state.target_value = self.state.cycle_target_profit * self.state.initial_position_size
        self.state.entry_spread = abs(long_fill - short_fill)  # diagnostic only
        for leg, fill in ((self.state.long, long_fill), (self.state.short, short_fill)):
            leg.original_trigger_level = leg.current_entry = fill
            leg.entry_confirmation = "broker"
            if leg.size_confirmation == "requested": leg.size_confirmation = "accepted_request"
            leg.stop_distance = self._stop_for(1)
            leg.stop = stop_for(leg.direction, fill, leg.stop_distance)
            self.state.remember_deal(leg, 1)
            self._remember_projection(leg, 1)
        # S1 TP covers the opposite side's expected loss plus T.
        for leg, opposite in ((self.state.long, self.state.short), (self.state.short, self.state.long)):
            distance = (self._expected(opposite) + self.state.target_value) / leg.size
            leg.take_profit = target_for(leg.direction, leg.current_entry, D("0"), distance)
        self.state.events.append(f"V3.3 target money={self.state.target_value}; initial spread diagnostic={self.state.entry_spread}")

    def stopped(self, direction: str, fill: D, event_id: str = "", *,
                scenario_at_close: int | None = None, broker_execution_time: str = "",
                actual_closed_size: D | None = None, fully_closed: bool = True) -> Leg:
        leg = self._leg(direction)
        if event_id and event_id in self.state.processed_events:
            return leg
        if not leg.open or leg.stop is None:
            raise RuntimeError(f"{direction} is not an open protected leg")
        size = actual_closed_size if actual_closed_size is not None else leg.size
        if size <= 0 or size > leg.size:
            raise RuntimeError("Actual closed size is invalid")
        signed = ((fill - leg.current_entry) if direction == "BUY"
                  else (leg.current_entry - fill)) * size
        loss = max(D("0"), -signed)
        close_key = event_id or f"stop:{leg.deal_id}:{fill}:{size}"
        if close_key not in self.state.processed_events:
            self.state.actual_cycle_pnl += signed
            self.state.actual_cycle_loss += loss
            self.state.general_recovery = self.state.actual_cycle_loss
            self.state.realized_loss_money = self.state.actual_cycle_loss
            self.state.realized_losses += loss / size if size else D("0")
            projection = self._projection(leg.deal_id)
            projected = D(str(projection["amount"])) if projection else D("0")
            if projection:
                projection.update({"replaced": True, "actual_loss": str(loss),
                                   "close_event_id": close_key, "actual_fill": str(fill),
                                   "actual_size": str(size), "broker_execution_time": broker_execution_time,
                                   "correction_money": str(loss - projected)})
            self.state.recovery_events.append({"key": close_key, "kind": "ACTUAL_LOSS",
                "deal_id": leg.deal_id, "scenario_at_close": scenario_at_close or self.state.scenario,
                "actual_loss": str(loss), "projected_loss": str(projected),
                "correction_money": str(loss - projected), "size": str(size), "fill": str(fill)})
        self.state.remember_deal(leg)
        if fully_closed:
            self.state.remember_close(leg.deal_id, "SL", fill, close_size=size)
            leg.open = False
        else:
            leg.size -= size
        if event_id and event_id not in self.state.processed_events:
            self.state.processed_events.append(event_id)
        if not fully_closed:
            self.refresh_targets(); return leg
        survivor = self._leg("SELL" if direction == "BUY" else "BUY")
        if survivor.open:
            self._set_protection(survivor)
            # The closed-side STOP entry is exactly the confirmed SL of the survivor.
            anchor = survivor.confirmation_stop or survivor.confirmed_stop or survivor.stop
            if anchor is None: raise RuntimeError("Survivor SL is not confirmed")
            leg.original_trigger_level = anchor
            self.state.phase = "LONG_ONLY" if survivor.direction == "BUY" else "SHORT_ONLY"
        return leg

    def _pending_for(self, direction: str, *, working_order_id: str = "", close_key: str = "") -> dict:
        candidates = [p for p in self.state.pending_recovery if p.get("direction") == direction
                      and not p.get("reentry_accounted")]
        if working_order_id:
            linked = [p for p in candidates if p.get("trigger_id") == working_order_id]
            if linked: candidates = linked
        if close_key: candidates = [p for p in candidates if p.get("close_key") == close_key]
        # V3.3 can derive ownership from the immutable projected component when legacy pending D
        # is absent; no monetary D/slippage is transferred.
        if not candidates:
            projection = next((p for p in self.state.projected_losses
                               if p.get("direction") == direction), None)
            if projection: return projection
            raise RuntimeError(f"No owned outgoing position for {direction}")
        if len(candidates) != 1: raise RuntimeError("Ambiguous outgoing position ownership")
        return candidates[0]

    def reopened(self, direction: str, fill: D, deal_id: str = "", event_id: str = "",
                 actual_size: D | None = None, *, working_order_id: str = "",
                 close_key: str = "", broker_execution_time: str = "") -> None:
        if event_id and event_id in self.state.processed_events: return
        if self.state.scenario >= 9: raise RuntimeError("Scenario 10 does not exist")
        leg = self._leg(direction)
        next_scenario = self.state.scenario + 1
        size = actual_size if actual_size is not None else self._size_for(next_scenario)
        if size <= 0: raise RuntimeError("Actual reopened size is unknown")
        self.state.scenario = next_scenario
        leg.size, leg.stop_distance = size, self._stop_for(next_scenario)
        leg.current_entry, leg.deal_id, leg.open = fill, deal_id, True
        leg.entry_confirmation = "broker"; leg.size_confirmation = "broker"
        leg.trigger_id = leg.trigger_reference = ""
        leg.confirmed_stop = leg.confirmed_take_profit = None
        leg.confirmation_stop = leg.confirmation_take_profit = None
        leg.protection_sent_stop = leg.protection_sent_take_profit = None
        leg.protection_confirmation = leg.protection_readback = ""
        self._set_protection(leg)
        self.state.remember_deal(leg, next_scenario)
        record = next((r for r in self.state.deal_history if r.get("deal_id") == deal_id), None)
        if record is not None:
            record.update({"trigger_id": working_order_id, "open_working_order_id": working_order_id,
                           "broker_open_execution_time": broker_execution_time})
        self._remember_projection(leg, next_scenario)
        opposite = self._leg("SELL" if direction == "BUY" else "BUY")
        if not opposite.open:
            opposite.original_trigger_level = leg.stop
        key = event_id or f"reopen:{deal_id}:{fill}"
        self.state.scenario_transitions.append({"key": key, "cycle_id": self.state.cycle_id,
            "attempt_id": self.state.active_attempt_id, "scenario": next_scenario,
            "direction": direction, "deal_id": deal_id, "working_order_id": working_order_id,
            "fill": str(fill), "size": str(size), "broker_execution_time": broker_execution_time})
        self.state.phase = ("TRANSITION_RECONCILING" if opposite.open else
                            ("LONG_ONLY" if direction == "BUY" else "SHORT_ONLY"))
        if event_id: self.state.processed_events.append(event_id)

    def account_double_sl_pending(self) -> D:
        return D("0")

    def complete(self, direction: str, fill: D | None = None) -> None:
        leg = self._leg(direction); close = fill if fill is not None else leg.take_profit
        if close is not None:
            signed = ((close - leg.current_entry) if direction == "BUY"
                      else (leg.current_entry - close)) * leg.size
            key = f"tp:{leg.deal_id}:{close}:{leg.size}"
            if key not in self.state.processed_events:
                self.state.actual_cycle_pnl += signed
                self.state.processed_events.append(key)
            self.state.remember_deal(leg); self.state.remember_close(leg.deal_id, "TP", close, close_size=leg.size)
            self.state.gross_take_profit = max(D("0"), signed / leg.size)
            self.state.net_cycle_money = self.state.actual_cycle_pnl
            self.state.net_cycle_result = self.state.actual_cycle_pnl
        self.state.completed_cycles += 1; self._consume_profit_override()
        self.state.active = False; self.state.phase = "COMPLETED"

    def complete_scenario_nine(self, long_fill: D, short_fill: D, extra_loss: D = D("0")) -> None:
        raise RuntimeError("V3.3 S9 is completed only by its own confirmed SL or TP")

    def _consume_profit_override(self) -> None:
        if self.state.profit_override is not None and self.state.profit_override_remaining > 0 \
                and self.state.cycle_target_profit == self.state.profit_override:
            self.state.profit_override_remaining -= 1
            if not self.state.profit_override_remaining: self.state.profit_override = None

    def refresh_targets(self) -> None:
        for leg in (self.state.long, self.state.short):
            if leg and leg.open: self._set_protection(leg)

    def recovery_distance_for(self, leg: Leg) -> D | None:
        return self._tp_money_for(leg) / leg.size if leg.size > 0 else None

    def projected_reopen(self, direction: str) -> tuple[D, D, D]:
        scenario = self.state.scenario + 1
        if scenario > 9: raise RuntimeError("Scenario 10 does not exist")
        size, distance = self._size_for(scenario), self._stop_for(scenario)
        current = self._leg("SELL" if direction == "BUY" else "BUY")
        projected = self.state.actual_cycle_loss + self._outstanding(exclude_deal_id="")
        # Ensure the current position's own expected loss is represented for the next position.
        if current.open and self._projection(current.deal_id) is None:
            projected += self._expected(current)
        tp_distance = (projected + self.state.target_value) / size
        return size, distance, tp_distance - distance

    def _leg(self, direction: str) -> Leg:
        leg = self.state.long if direction == "BUY" else self.state.short
        if leg is None: raise RuntimeError("Cycle has no such leg")
        return leg
