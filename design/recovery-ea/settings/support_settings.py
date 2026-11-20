"""Pure validation for two editable RECOVERY support activation distances."""
from dataclasses import dataclass
from decimal import Decimal
import re


_DEFAULT_FIRST = "0.25"
_DEFAULT_SECOND = "0.40"
_STOP_OFFSET = "0.25"
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
    support_first: str = _DEFAULT_FIRST
    support_second: str = _DEFAULT_SECOND

    def to_ticks(self, symbol_tick_size: str) -> SupportTicks:
        first = _positive_decimal(self.support_first, "support_first")
        second = _positive_decimal(self.support_second, "support_second")
        if not first < second:
            raise ValueError("support_first must be below support_second")
        tick = _positive_decimal(symbol_tick_size, "symbol_tick_size")
        return SupportTicks(
            _ticks(first, tick, "support_first"),
            _ticks(second, tick, "support_second"),
            _ticks(Decimal(_STOP_OFFSET), tick, "support_offset"),
        )


def validate_support_edit(body, *, generation_active, symbol_tick_size: str) -> SupportSettings:
    """Validate both fields as one idle-state edit; never mutate an existing generation.

    Caller supplies authoritative runtime state and tick metadata; the HTTP body
    cannot override either. In web integration use the same serialized section
    for idle-check, saving and arming. This function itself cannot persist or arm.
    """
    if generation_active is not False:
        raise ValueError("cannot edit activation thresholds during or without verified idle state")
    if not isinstance(body, dict) or body.keys() != {"support_first", "support_second"}:
        raise ValueError("edit requires exactly support_first and support_second")
    candidate = SupportSettings(**body)
    candidate.to_ticks(symbol_tick_size)
    return candidate
