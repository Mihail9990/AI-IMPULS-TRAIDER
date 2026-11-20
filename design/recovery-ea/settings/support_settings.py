"""Pure validation for the one editable RECOVERY breakeven threshold."""
from dataclasses import dataclass
from decimal import Decimal
import re


_DEFAULT_THRESHOLD = "0.40"
_MAX_UNITS = 10**12  # RecoveryCycle.mqh: RC_MAX_UNITS
_PLAIN_DECIMAL = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,16})?")


def _positive_decimal(raw, field):
    if not isinstance(raw, str):
        raise ValueError(f"{field}: expected a decimal string")
    if not _PLAIN_DECIMAL.fullmatch(raw):
        raise ValueError(f"{field}: expected a plain decimal with a dot")
    value = Decimal(raw)
    if value <= 0:
        raise ValueError(f"{field}: must be positive")
    return value


def _ticks(value, tick, field):
    # Exact integer arithmetic; no rounding or dependency on decimal context.
    numerator, denominator = value.as_integer_ratio()
    tick_numerator, tick_denominator = tick.as_integer_ratio()
    count, remainder = divmod(numerator * tick_denominator, denominator * tick_numerator)
    if remainder:
        raise ValueError(f"{field}: does not match the symbol tick grid")
    if not 0 < count <= _MAX_UNITS:
        raise ValueError(f"{field}: outside supported tick range")
    return count


@dataclass(frozen=True)
class SupportTicks:
    support_first_ticks: int
    support_second_ticks: int
    support_offset_ticks: int


@dataclass(frozen=True)
class SupportSettings:
    support_threshold: str = _DEFAULT_THRESHOLD

    def to_ticks(self, symbol_tick_size: str) -> SupportTicks:
        threshold = _positive_decimal(self.support_threshold, "support_threshold")
        tick = _positive_decimal(symbol_tick_size, "symbol_tick_size")
        return SupportTicks(0, _ticks(threshold, tick, "support_threshold"), 0)


def validate_support_edit(body, *, generation_active, symbol_tick_size: str) -> SupportSettings:
    """Validate one idle-state edit; runtime owns actual state and tick size."""
    if generation_active is not False:
        raise ValueError("cannot edit activation threshold during or without verified idle state")
    if not isinstance(body, dict) or body.keys() != {"support_threshold"}:
        raise ValueError("edit requires exactly one support_threshold")
    candidate = SupportSettings(**body)
    candidate.to_ticks(symbol_tick_size)
    return candidate
