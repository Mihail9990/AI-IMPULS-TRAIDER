from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
import json
from pathlib import Path


D = Decimal


@dataclass
class Leg:
    direction: str
    original_trigger_level: Decimal
    current_entry: Decimal
    deal_id: str = ""
    deal_reference: str = ""
    open: bool = True
    trigger_id: str = ""
    trigger_reference: str = ""
    # A MARKET fallback with a known dealReference but delayed confirmation is durable state, not
    # permission to submit another order. Subsequent ticks resolve this same reference first.
    pending_market_reference: str = ""
    pending_market_reason: str = ""
    pending_market_kind: str = ""
    pending_market_unknown_post: bool = False
    pending_market_preexisting_ids: list[str] = field(default_factory=list)
    stop: Decimal | None = None
    take_profit: Decimal | None = None
    confirmed_stop: Decimal | None = None
    confirmed_take_profit: Decimal | None = None
    protection_sent_stop: Decimal | None = None
    protection_sent_take_profit: Decimal | None = None
    confirmation_stop: Decimal | None = None
    confirmation_take_profit: Decimal | None = None
    protection_confirmation: str = ""
    protection_readback: str = ""
    size: Decimal = D("0")
    stop_distance: Decimal = D("0")
    recovery: Decimal = D("0")
    temporary_stop_compensation: Decimal = D("0")
    temporary_spread_compensation: Decimal = D("0")
    temporary_slippage_compensation: Decimal = D("0")
    # Field names absent from a legacy JSON object.  A saved numeric zero is not missing.
    legacy_missing_fields: list[str] = field(default_factory=list)

    @property
    def effective_recovery(self) -> Decimal:
        return (self.recovery + self.temporary_stop_compensation
                + self.temporary_spread_compensation + self.temporary_slippage_compensation)

    @property
    def temporary_recovery(self) -> Decimal:
        return (self.temporary_stop_compensation + self.temporary_spread_compensation
                + self.temporary_slippage_compensation)

    def json(self) -> dict:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in asdict(self).items()
        }


@dataclass
class CycleState:
    active: bool = False
    armed: bool = False
    waiting_current_candle: bool = False
    paused: bool = False
    manual: bool = False
    scenario: int = 0
    recovery: Decimal = D("0")
    entry_spread: Decimal = D("0")
    realized_losses: Decimal = D("0")
    realized_loss_money: Decimal = D("0")
    gross_take_profit: Decimal = D("0")
    net_cycle_result: Decimal = D("0")
    net_cycle_money: Decimal = D("0")
    scenario_nine_prior_losses: Decimal = D("0")
    scenario_nine_close_gap: Decimal = D("0")
    scenario_nine_total_loss: Decimal = D("0")
    scenario_nine_extra_loss: Decimal = D("0")
    scenario_nine_triggers_verified: bool = False
    scenario_nine_long_fill: Decimal | None = None
    scenario_nine_short_fill: Decimal | None = None
    cycle_target_profit: Decimal = D("0")
    profit_override: Decimal | None = None
    profit_override_remaining: int = 0
    pending_tp_direction: str = ""
    pending_tp_fill: Decimal | None = None
    pending_close_direction: str = ""
    pending_close_reference: str = ""
    pending_close_reason: str = ""
    long: Leg | None = None
    short: Leg | None = None
    phase: str = "IDLE"
    telegram_offset: int = 0
    completed_cycles: int = 0
    diagnostic_cleanup_cycle: int = 0
    diagnostic_cycle_number: int = 0
    attempt_counter: int = 0
    active_attempt_id: int = 0
    cycle_id: int = 0
    cycle_attempt: int = 0
    continuation_pause_until: float = 0.0
    continuation_stopped_by_user: bool = False
    cycle_attempt_start_losses: Decimal = D("0")
    cycle_attempt_start_loss_money: Decimal = D("0")
    continuation_managed: bool = False
    continuation_stage: str = ""
    continuation_flat_checks: int = 0
    continuation_filter_reason: str = ""
    attempt_result_total: Decimal = D("0")
    attempt_history: list[dict] = field(default_factory=list)
    initial_submitted_directions: list[str] = field(default_factory=list)
    attempt_deal_ids: list[str] = field(default_factory=list)
    pending_actual_attempt_id: int = 0
    pending_actual_deal_ids: list[str] = field(default_factory=list)
    # Durable notification state is deliberately separate from processed trading events.  A
    # broker event may be fully accounted while its human-readable report is still waiting for
    # history or Telegram delivery.
    pending_notification_jobs: list[dict] = field(default_factory=list)
    report_outbox: list[dict] = field(default_factory=list)
    next_report_id: int = 1
    last_trigger_resolution: str = "Нет связанного Trigger."
    processed_events: list[str] = field(default_factory=list)
    cycle_trigger_ids: list[str] = field(default_factory=list)
    # Durable broker ledger.  Leg.deal_id necessarily changes after every trigger fill, while
    # Capital.com's deal-specific history remains addressable by every previous dealId.  Keep the
    # IDs instead of losing them when a Leg is reopened or the cycle is reset.
    deal_history: list[dict] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def remember_deal(self, leg: Leg, scenario: int | None = None) -> None:
        if not leg.deal_id:
            return
        record = next(
            (item for item in self.deal_history if item.get("deal_id") == leg.deal_id), None
        )
        values = {
            "deal_id": leg.deal_id,
            "deal_reference": leg.deal_reference,
            "direction": leg.direction,
            "entry": str(leg.current_entry),
            "scenario": self.scenario if scenario is None else scenario,
            "attempt_id": self.active_attempt_id or self.diagnostic_cycle_number,
            "cycle_id": self.cycle_id,
            "cycle_attempt": self.cycle_attempt,
            "trigger_id": leg.trigger_id,
            "size": str(leg.size),
            "close_source": "",
            "close_level": None,
        }
        if record is None:
            self.deal_history.append(values)
        else:
            # Preserve close information already learned from activity history.
            values["close_source"] = record.get("close_source", "")
            values["close_level"] = record.get("close_level")
            record.update(values)
        if leg.deal_id not in self.attempt_deal_ids:
            self.attempt_deal_ids.append(leg.deal_id)
        # This is diagnostic/recovery metadata rather than an unbounded transaction database.
        del self.deal_history[:-500]

    def remember_close(self, deal_id: str, source: str, level: Decimal) -> None:
        record = next(
            (item for item in self.deal_history if item.get("deal_id") == deal_id), None
        )
        if record is None:
            record = {"deal_id": deal_id}
            self.deal_history.append(record)
        record.update({"close_source": source.upper(), "close_level": str(level)})
        del self.deal_history[:-500]

    def remember_attempt(
        self, status: str, result: Decimal, *, include_in_total: bool = True, **details
    ) -> None:
        """Persist one unique trading attempt without feeding it into strategy recovery."""
        attempt_id = self.active_attempt_id or self.diagnostic_cycle_number
        if not attempt_id or any(item.get("attempt_id") == attempt_id for item in self.attempt_history):
            return
        self.attempt_history.append({
            "attempt_id": attempt_id,
            "cycle_id": self.cycle_id,
            "cycle_attempt": self.cycle_attempt,
            "status": status,
            "result": str(result),
            **{key: str(value) if isinstance(value, Decimal) else value
               for key, value in details.items()},
        })
        if include_in_total:
            self.attempt_result_total += result
        del self.attempt_history[:-500]

    def save(self, path: str) -> None:
        payload = asdict(self)
        payload["recovery"] = str(self.recovery)
        payload["entry_spread"] = str(self.entry_spread)
        payload["realized_losses"] = str(self.realized_losses)
        payload["realized_loss_money"] = str(self.realized_loss_money)
        payload["gross_take_profit"] = str(self.gross_take_profit)
        payload["net_cycle_result"] = str(self.net_cycle_result)
        payload["net_cycle_money"] = str(self.net_cycle_money)
        payload["attempt_result_total"] = str(self.attempt_result_total)
        payload["cycle_attempt_start_losses"] = str(self.cycle_attempt_start_losses)
        payload["cycle_attempt_start_loss_money"] = str(self.cycle_attempt_start_loss_money)
        payload["scenario_nine_prior_losses"] = str(self.scenario_nine_prior_losses)
        payload["scenario_nine_close_gap"] = str(self.scenario_nine_close_gap)
        payload["scenario_nine_total_loss"] = str(self.scenario_nine_total_loss)
        payload["scenario_nine_extra_loss"] = str(self.scenario_nine_extra_loss)
        payload["scenario_nine_long_fill"] = (
            str(self.scenario_nine_long_fill) if self.scenario_nine_long_fill is not None else None
        )
        payload["scenario_nine_short_fill"] = (
            str(self.scenario_nine_short_fill) if self.scenario_nine_short_fill is not None else None
        )
        payload["cycle_target_profit"] = str(self.cycle_target_profit)
        payload["profit_override"] = (
            str(self.profit_override) if self.profit_override is not None else None
        )
        payload["pending_tp_fill"] = (
            str(self.pending_tp_fill) if self.pending_tp_fill is not None else None
        )
        payload["long"] = self.long.json() if self.long else None
        payload["short"] = self.short.json() if self.short else None
        destination = Path(path)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)

    @classmethod
    def load(cls, path: str) -> "CycleState":
        file = Path(path)
        if not file.exists():
            return cls()
        raw = json.loads(file.read_text(encoding="utf-8"))
        # A permanent transport classification applies only to that process/request.  On a later
        # launch every non-delivered report part is eligible for recovery; acknowledged parts stay
        # delivered and are never repeated.
        for report in raw.get("report_outbox", []):
            for part in report.get("parts", []):
                if part.get("status") != "delivered":
                    part["status"] = "pending"
        for name in ("long", "short"):
            leg = raw.get(name)
            if leg:
                legacy_fields = (
                    "size", "stop_distance", "recovery", "temporary_stop_compensation",
                    "temporary_spread_compensation", "temporary_slippage_compensation",
                )
                missing = [key for key in legacy_fields if key not in leg]
                legacy_entry = leg.pop("entry", None)
                if legacy_entry is not None:
                    leg.setdefault("original_trigger_level", legacy_entry)
                    leg.setdefault("current_entry", legacy_entry)
                for key in ("original_trigger_level", "current_entry", "stop", "take_profit",
                            "confirmed_stop", "confirmed_take_profit", "protection_sent_stop",
                            "protection_sent_take_profit", "confirmation_stop",
                            "confirmation_take_profit",
                            "size", "stop_distance", "recovery", "temporary_stop_compensation",
                            "temporary_spread_compensation", "temporary_slippage_compensation"):
                    if leg.get(key) is not None:
                        leg[key] = D(str(leg[key]))
                leg.setdefault("legacy_missing_fields", missing)
                raw[name] = Leg(**leg)
        raw["recovery"] = D(str(raw.get("recovery", "0")))
        for name in (
            "entry_spread", "realized_losses", "realized_loss_money", "gross_take_profit", "net_cycle_result",
            "net_cycle_money",
            "attempt_result_total",
            "cycle_attempt_start_losses",
            "cycle_attempt_start_loss_money",
            "scenario_nine_prior_losses", "scenario_nine_close_gap",
            "scenario_nine_total_loss", "scenario_nine_extra_loss",
            "cycle_target_profit", "profit_override", "pending_tp_fill",
            "scenario_nine_long_fill", "scenario_nine_short_fill",
        ):
            if name in {
                "profit_override", "pending_tp_fill", "scenario_nine_long_fill",
                "scenario_nine_short_fill",
            } and raw.get(name) is None:
                continue
            raw[name] = D(str(raw.get(name, "0")))
        allowed = cls.__dataclass_fields__
        return cls(**{key: value for key, value in raw.items() if key in allowed})

    def reset(self) -> None:
        self.active = self.armed = self.waiting_current_candle = False
        self.paused = self.manual = False
        self.scenario = 0
        self.recovery = self.entry_spread = D("0")
        self.realized_losses = self.realized_loss_money = D("0")
        self.gross_take_profit = self.net_cycle_result = self.net_cycle_money = D("0")
        self.scenario_nine_prior_losses = self.scenario_nine_close_gap = D("0")
        self.scenario_nine_total_loss = self.scenario_nine_extra_loss = D("0")
        self.scenario_nine_triggers_verified = False
        self.scenario_nine_long_fill = self.scenario_nine_short_fill = None
        self.pending_tp_direction = ""
        self.pending_tp_fill = None
        self.pending_close_direction = self.pending_close_reference = self.pending_close_reason = ""
        self.last_trigger_resolution = "Нет связанного Trigger."
        self.long = self.short = None
        self.phase = "IDLE"
        self.processed_events.clear()
        self.cycle_trigger_ids.clear()
        self.initial_submitted_directions.clear()
        self.attempt_deal_ids.clear()
        self.active_attempt_id = 0
        self.cycle_id = self.cycle_attempt = 0
        self.continuation_pause_until = 0.0
        self.continuation_stopped_by_user = False
        self.cycle_attempt_start_losses = D("0")
        self.cycle_attempt_start_loss_money = D("0")
        self.continuation_managed = False
        self.continuation_stage = ""
        self.continuation_flat_checks = 0
        self.continuation_filter_reason = ""


def stop_for(direction: str, entry: Decimal, distance: Decimal) -> Decimal:
    return entry - distance if direction == "BUY" else entry + distance


def target_for(direction: str, entry: Decimal, distance: Decimal, recovery: Decimal) -> Decimal:
    """Return strategic TP from this position's own confirmed/projected entry."""
    total = distance + recovery
    return entry + total if direction == "BUY" else entry - total


def stop_slippage(direction: str, expected: Decimal, actual: Decimal) -> Decimal:
    """Return the unsigned deviation between planned and actual stop execution."""
    del direction  # Direction does not change the configured absolute-distance rule.
    return abs(expected - actual)


def trigger_slippage(direction: str, trigger: Decimal, actual: Decimal) -> Decimal:
    """Return the unsigned deviation between saved trigger level and actual fill."""
    del direction
    return abs(trigger - actual)
