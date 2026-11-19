"""Account-bound native lot table; no broker dispatch or first-entry changes."""
import re
from decimal import Decimal

from .volume_schedule import VOLUMES

DEFAULT_LOT_SCHEDULE = tuple(str(volume) for volume in VOLUMES)
_LOT = re.compile(r'(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,8})?\Z', re.ASCII)


def validate_lot_schedule(values):
    if type(values) is not list or len(values) != len(VOLUMES):
        raise ValueError('Lot schedule must have 24 decimal strings')
    if any(type(x) is not str or not _LOT.fullmatch(x) or Decimal(x) <= 0 for x in values):
        raise ValueError('Lot schedule contains an invalid positive decimal')
    if values[0] != DEFAULT_LOT_SCHEDULE[0]:
        raise ValueError('Initial lot is fixed at 0.01')
    return list(values)


def validate_broker_lots(values, info):
    def field(key):
        try:
            value = Decimal(str(getattr(info, key, None)))
        except ArithmeticError as exc:
            raise ValueError('Broker volume policy unavailable') from exc
        if not value.is_finite() or value <= 0 or value.adjusted() > 20 or value.as_tuple().exponent < -18:
            raise ValueError('Broker volume policy unavailable')
        return value

    low, high, step = (field(k) for k in ('volume_min', 'volume_max', 'volume_step'))
    if low > high:
        raise ValueError('Broker volume policy unavailable')
    for slot, amount in enumerate(map(Decimal, values), 1):
        if not low <= amount <= high:
            raise ValueError(f'Lot for slot {slot} is outside broker limits')
        # Compare exact integer ratios rather than floating point or Decimal
        # remainder affected by the process-wide context precision.
        n, d = amount.as_integer_ratio()
        sn, sd = step.as_integer_ratio()
        if (n * sd) % (d * sn):
            raise ValueError(f'Lot for slot {slot} is outside broker volume step')
