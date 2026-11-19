"""Offline parity oracle; no terminal client, order submission, or routing."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

Side = Literal["BUY", "SELL"]

@dataclass(frozen=True)
class AdoptPlan:
    observed_sl: Decimal | None
    position_sl_write: Decimal | None
    preserved_tp: Decimal | None
    reverse_side: Side
    reverse_price: Decimal
    reverse_sl: Decimal


def plan_adoption(*, side: Side, entry: Decimal, existing_sl: Decimal | None,
                  existing_tp: Decimal | None, distance: Decimal,
                  tick_size: Decimal) -> AdoptPlan:
    """Price intent only. Live broker, market bounds, and identity checks are separate."""
    def positive(value: Decimal) -> bool:
        return type(value) is Decimal and value.is_finite() and value > 0
    if side not in ("BUY", "SELL") or not all(map(positive, (entry, distance, tick_size))):
        raise ValueError("invalid inputs")
    if distance < Decimal("0.60"):
        raise ValueError("configured distance below minimum")
    for price in (existing_sl, existing_tp):
        if price is not None and not positive(price):
            raise ValueError("invalid observed SL/TP")
    if distance % tick_size != 0:
        raise ValueError("price or distance not on tick grid")
    sign = 1 if side == "BUY" else -1
    reverse = "SELL" if side == "BUY" else "BUY"
    anchor = existing_sl if existing_sl is not None else entry - sign * distance
    reverse_sl = anchor + sign * distance
    if not positive(anchor) or not positive(reverse_sl) or anchor % tick_size != 0:
        raise ValueError("invalid or off-grid trigger; do not shift it")
    return AdoptPlan(existing_sl, None if existing_sl is not None else anchor,
                     existing_tp, reverse, anchor, reverse_sl)
