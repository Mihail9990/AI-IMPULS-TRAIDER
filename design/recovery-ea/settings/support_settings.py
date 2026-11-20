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
    support_first_enabled: bool = True

    def to_ticks(self, symbol_tick_size: str) -> SupportTicks:
        if type(self.support_first_enabled) is not bool:
            raise ValueError("support_first_enabled: expected a boolean")
        first = _positive_decimal(self.support_first, "support_first")
        second = _positive_decimal(self.support_second, "support_second")
        tick = _positive_decimal(symbol_tick_size, "symbol_tick_size")
        second_ticks = _ticks(second, tick, "support_second")
        if not self.support_first_enabled:
            # Keep the saved first distance as text, but stage 1 cannot trigger
            # and its unused offset need not fit the selected symbol grid.
            return SupportTicks(0, second_ticks, 0)
        if not first < second:
            raise ValueError("support_first must be below support_second")
        return SupportTicks(
            _ticks(first, tick, "support_first"),
            second_ticks,
            _ticks(Decimal(_STOP_OFFSET), tick, "support_offset"),
        )


def validate_support_edit(body, *, generation_active, symbol_tick_size: str,
                          current: SupportSettings | None = None) -> SupportSettings:
    """Validate both fields as one idle-state edit; never mutate an existing generation.

    Caller supplies authoritative runtime state and tick metadata; the HTTP body
    cannot override either. In web integration use the same serialized section
    for idle-check, saving and arming. This function itself cannot persist or arm.
    """
    if generation_active is not False:
        raise ValueError("cannot edit activation thresholds during or without verified idle state")
    if not isinstance(body, dict) or not ({"support_first", "support_second"} <= body.keys()
                                                <= {"support_first", "support_second", "support_first_enabled"}):
        raise ValueError("edit requires two thresholds and an optional first-stage switch")
    if current is None:
        current = SupportSettings()
    if not isinstance(current, SupportSettings):
        raise TypeError("authoritative current support settings required")
    enabled = body.get("support_first_enabled", current.support_first_enabled)
    if type(enabled) is not bool:
        raise ValueError("support_first_enabled: expected a boolean")
    # One-stage controls send the prior first value. Do not mutate it while
    # disabled, even if a forged/stale browser tried to change a hidden input.
    first = body["support_first"] if enabled else current.support_first
    candidate = SupportSettings(first, body["support_second"], enabled)
    candidate.to_ticks(symbol_tick_size)
    return candidate
