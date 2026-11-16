"""Provisional previous-position SL estimates, never realized accounting.

Callers persist returned rows in the same cycle transaction as their use. Removing
an estimate and booking its actual closing deals MUST be one atomic transaction.
This module neither infers a closure nor authorizes trading from missing evidence.
"""
from decimal import Decimal
from .settings import number

ZERO = Decimal(0)


def estimate_previous_close(broker, ticket, *, cycle_id, remaining_volume=None):
    """Calculate outstanding previous-position price P&L in account currency.

    Entry commissions are already in booked realized P&L. Unknown exit costs are
    not invented. A verified partial close may pass the *remaining* volume, with
    its confirmed portion already booked separately. Never use the new opposite
    position's lot, SL, or current quote here.
    """
    pid = str(ticket.get('id') or '')
    cid = str(ticket.get('command_id') or '')
    if not pid or not cid or not cycle_id:
        raise ValueError('Previous position, command and cycle identity required')
    if ticket.get('closed'):
        raise ValueError('Already closed position cannot be estimated')
    side = ticket.get('side')
    if side not in {'BUY', 'SELL'}:
        raise ValueError('Previous position side required')
    size, entry, sl = map(number, (ticket['size'], ticket['entry'], ticket['sl']))
    volume = size if remaining_volume is None else number(remaining_volume)
    if min(size, entry, sl, volume) <= 0 or volume > size:
        raise ValueError('Invalid previous position prices or remaining volume')
    net = number(broker.profit(side, volume, entry, sl))
    return dict(position_id=pid, command_id=cid, cycle_id=str(cycle_id),
                side=side, entry=str(entry), sl=str(sl), volume=str(volume),
                estimated_close_net=str(net), basis='PREVIOUS_POSITION_ENTRY_TO_SL',
                excludes_unknown_exit_costs=True)


def effective_recovery(realized, estimates, *, cycle_id, waived='0'):
    """Outstanding estimate + booked cash, without adding opening costs twice.

    Mapping keys are stable old position IDs. Reject foreign-cycle or duplicate
    command rows rather than accidentally charge another cycle's debt.
    """
    projected = ZERO
    commands = set()
    for pid, row in estimates.items():
        if str(pid) != row['position_id'] or row['cycle_id'] != str(cycle_id):
            raise ValueError('Provisional estimate identity mismatch')
        if row['command_id'] in commands:
            raise ValueError('Duplicate provisional command')
        commands.add(row['command_id'])
        projected += number(row['estimated_close_net'])
    waiver = number(waived)
    if waiver < 0:
        raise ValueError('Recovery waiver cannot be negative')
    return max(ZERO, -number(realized) - projected - waiver)


def without_settled_estimate(estimates, position_id, *, cycle_id, command_id):
    """Return a new mapping for the caller's atomic close-booking transaction.

    Does not add actual P&L itself. Idempotent repeated removal is intentional;
    closing-deal identity deduplication remains the durable ledger's obligation.
    """
    rows = dict(estimates)
    row = rows.get(str(position_id))
    if row is not None:
        if row['cycle_id'] != str(cycle_id) or row['command_id'] != str(command_id):
            raise ValueError('Cannot settle another obligation estimate')
        del rows[str(position_id)]
    return rows


def last_open_target(broker, spec, tickets, debt, *, reserve='0', quote=None):
    """Recalculate only highest-index open rung, upward OR downward.

    ``debt`` may be signed: negative means booked surplus, not zero debt.
    Callers supply proven CURRENT live sizes/SL/TP and handle manual overrides.
    Other open targets stay fixed. Final target stays favorable of entry, other
    TP steps and executable quote (including stops/freeze), on the tick grid.
    """
    from decimal import ROUND_CEILING, ROUND_FLOOR
    active = [t for t in tickets if not t.get('closed')]
    if not active:
        return None
    if any(isinstance(t['index'], bool) or not isinstance(t['index'], int) or t['index'] < 0 for t in active):
        raise ValueError('Invalid ladder index')
    active.sort(key=lambda t: t['index'])
    if len({t['index'] for t in active}) != len(active) or len({t['id'] for t in active}) != len(active):
        raise ValueError('Duplicate ladder identity')
    last = active[-1]
    side = last['side']
    if side not in {'BUY', 'SELL'} or any(t['side'] != side for t in active):
        raise ValueError('Mixed ladder sides')
    for t in active:
        if number(t['size']) <= 0 or number(t['entry']) <= 0:
            raise ValueError('Invalid remaining rung volume/entry')
        spec.price(t['tp'])
    size, entry = number(last['size']), number(last['entry'])
    sign = Decimal(1) if side == 'BUY' else Decimal(-1)
    tick = number(spec.tick_size)
    if tick <= 0:
        raise ValueError('Invalid price tick')
    # Oriented price rises with profit for both BUY and SELL.
    boundary = sign * entry + tick
    for t in active[:-1]:
        boundary = max(boundary, sign * number(t['tp']) + tick)
    if quote is not None:
        executable = number(quote['bid' if side == 'BUY' else 'ask'])
        if executable <= 0:
            raise ValueError('Invalid executable quote')
        distance = number(spec.point) * max(number(spec.stops_level), number(spec.freeze_level))
        boundary = max(boundary, sign * executable + max(distance, tick))
    oriented_ticks = (boundary / tick).to_integral_value(rounding=ROUND_CEILING)
    base = spec.price(sign * oriented_ticks * tick)
    other = sum((number(broker.profit(t['side'], number(t['size']), number(t['entry']), spec.price(t['tp']))) for t in active[:-1]), ZERO)
    cost = number(reserve)
    if cost < 0:
        raise ValueError('Negative reserve')
    goal = number(debt) + cost - other

    def profit(ticks):
        price = spec.price(base + sign * tick * ticks)
        return number(broker.profit(side, size, entry, price))

    if profit(0) >= goal:
        return dict(position_id=last['id'], tp=str(base))
    lo, hi = 0, 1
    max_ticks = int((base / tick)) - 1 if side == 'SELL' else 2**28
    while hi <= max_ticks and profit(hi) < goal:
        lo, hi = hi, min(hi * 2, max_ticks + 1)
    if hi > max_ticks:
        hi = max_ticks
        if hi <= lo or profit(hi) < goal:
            raise ValueError('No positive-price recovery target')
    while hi - lo > 1:
        mid = (hi + lo) // 2
        if profit(mid) >= goal:
            hi = mid
        else:
            lo = mid
    return dict(position_id=last['id'], tp=str(spec.price(base + sign * tick * hi)))
