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
    stop: Decimal | None = None
    take_profit: Decimal | None = None

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
    gross_take_profit: Decimal = D("0")
    net_cycle_result: Decimal = D("0")
    scenario_nine_prior_losses: Decimal = D("0")
    scenario_nine_close_gap: Decimal = D("0")
    scenario_nine_total_loss: Decimal = D("0")
    scenario_nine_long_fill: Decimal | None = None
    scenario_nine_short_fill: Decimal | None = None
    cycle_target_profit: Decimal = D("0")
    profit_override: Decimal | None = None
    profit_override_remaining: int = 0
    long: Leg | None = None
    short: Leg | None = None
    phase: str = "IDLE"
    telegram_offset: int = 0
    completed_cycles: int = 0
    diagnostic_cleanup_cycle: int = 0
    processed_events: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def save(self, path: str) -> None:
        payload = asdict(self)
        payload["recovery"] = str(self.recovery)
        payload["entry_spread"] = str(self.entry_spread)
        payload["realized_losses"] = str(self.realized_losses)
        payload["gross_take_profit"] = str(self.gross_take_profit)
        payload["net_cycle_result"] = str(self.net_cycle_result)
        payload["scenario_nine_prior_losses"] = str(self.scenario_nine_prior_losses)
        payload["scenario_nine_close_gap"] = str(self.scenario_nine_close_gap)
        payload["scenario_nine_total_loss"] = str(self.scenario_nine_total_loss)
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
        for name in ("long", "short"):
            leg = raw.get(name)
            if leg:
                legacy_entry = leg.pop("entry", None)
                if legacy_entry is not None:
                    leg.setdefault("original_trigger_level", legacy_entry)
                    leg.setdefault("current_entry", legacy_entry)
                for key in ("original_trigger_level", "current_entry", "stop", "take_profit"):
                    if leg.get(key) is not None:
                        leg[key] = D(str(leg[key]))
                raw[name] = Leg(**leg)
        raw["recovery"] = D(str(raw.get("recovery", "0")))
        for name in (
            "entry_spread", "realized_losses", "gross_take_profit", "net_cycle_result",
            "scenario_nine_prior_losses", "scenario_nine_close_gap",
            "scenario_nine_total_loss", "cycle_target_profit", "profit_override",
            "scenario_nine_long_fill", "scenario_nine_short_fill",
        ):
            if name in {
                "profit_override", "scenario_nine_long_fill", "scenario_nine_short_fill",
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
        self.realized_losses = self.gross_take_profit = self.net_cycle_result = D("0")
        self.scenario_nine_prior_losses = self.scenario_nine_close_gap = D("0")
        self.scenario_nine_total_loss = self.cycle_target_profit = D("0")
        self.scenario_nine_long_fill = self.scenario_nine_short_fill = None
        self.long = self.short = None
        self.phase = "IDLE"
        self.processed_events.clear()


def stop_for(direction: str, entry: Decimal, distance: Decimal) -> Decimal:
    return entry - distance if direction == "BUY" else entry + distance


def stop_slippage(direction: str, expected: Decimal, actual: Decimal) -> Decimal:
    """Return the unsigned deviation between planned and actual stop execution."""
    del direction  # Direction does not change the configured absolute-distance rule.
    return abs(expected - actual)


def trigger_slippage(direction: str, trigger: Decimal, actual: Decimal) -> Decimal:
    """Return the unsigned deviation between saved trigger level and actual fill."""
    del direction
    return abs(trigger - actual)
