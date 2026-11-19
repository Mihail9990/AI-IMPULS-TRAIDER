"""Exact grid recovery from an EA fixed-16-decimal binary64 price export.

Not a general price-rounding/tolerance function. Accept only an exact grid price
or its exact Python IEEE binary64 fixed-16 rendering. An untested difference in
MQL5 formatting is rejected rather than widening the acceptance interval.
"""
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_EVEN
import math


def canonical_exported_price(exported, tick_size):
    try:
        value=Decimal(str(exported));tick=Decimal(str(tick_size))
        if not value.is_finite() or not tick.is_finite() or value<=0 or tick<=0:
            raise ValueError('Positive finite price and tick required')
        # Bound arithmetic independently of caller Decimal context and hostile
        # exponents. Actual MT5 price metadata should be far inside this range.
        if abs(value.adjusted())>18 or abs(tick.adjusted())>18:
            raise ValueError('Unsupported price/tick magnitude')
        with localcontext() as ctx:
            ctx.prec=80
            units=(value/tick).to_integral_value(rounding=ROUND_HALF_EVEN)
            grid=units*tick
            if grid<=0:raise ValueError('Positive grid price required')
            binary=float(grid)
            if not math.isfinite(binary):raise ValueError('Nonfinite binary price')
            # Adjacent grid prices must remain distinct as binary64, otherwise
            # export bytes cannot establish which grid level the broker used.
            if float(grid-tick)==binary or float(grid+tick)==binary:
                raise ValueError('Tick grid is ambiguous at binary64 precision')
            rendered=Decimal(format(binary,'.16f'))
            if any(Decimal(format(float(neighbor),'.16f'))==rendered for neighbor in (grid-tick,grid+tick)):
                raise ValueError('Tick grid is ambiguous in fixed16 export')
            if value==grid:return grid
            if value!=rendered:
                raise ValueError('Price is neither exact tick-grid value nor exact binary64 export')
            return grid
    except (InvalidOperation,OverflowError,TypeError) as exc:
        raise ValueError('Invalid exported price or tick') from exc
