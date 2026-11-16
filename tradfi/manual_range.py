"""Pure preparation for user-confirmed asymmetric manual-close ranges.

No controller wiring or execution authority. The caller must first resolve the
final actual closing execution; entry prices are never used. Range
lifetime (one-shot / permanent) is deliberately not inferred here.
"""
from .settings import number


def manual_close_range(side, last_exit, same_side_distance, opposite_distance):
    """Both sides from the last actual exit; asymmetric distances by closed side.

    BUY: BUY=exit+same, SELL=exit-opposite.
    SELL: SELL=exit-same, BUY=exit+opposite.
    Distances are explicit settings, not silently inherited from SL recovery.
    This function does not infer which closing execution was last.
    """
    if side not in ('BUY','SELL'):
        raise ValueError('Нужна подтверждённая сторона закрытой корзины')
    exit_price,same,opposite=map(number,(last_exit,same_side_distance,opposite_distance))
    if min(exit_price,same,opposite)<=0:
        raise ValueError('Цены и оба расстояния должны быть положительными')
    if side=='BUY':buy,sell=exit_price+same,exit_price-opposite
    else:buy,sell=exit_price+opposite,exit_price-same
    if sell<=0 or buy<=sell:
        raise ValueError('SELL должен быть положительным и ниже BUY; уровни не изменены')
    return {'BUY':str(buy),'SELL':str(sell)}


def validate_manual_stops(spec, levels, quote):
    """Validate the entire pair before any future intent, with no price repair.

    Both must be pending STOPs valid at the current Bid/Ask. No one-sided partial
    placement, rounding, widened ranges or market conversion is authorized.
    SL/TP, volumes, ownership and time still require controller preflight.
    """
    if not isinstance(levels,dict) or set(levels)!={'BUY','SELL'}:
        raise ValueError('Нужна полная пара BUY/SELL')
    buy,sell=spec.price(levels['BUY']),spec.price(levels['SELL'])
    bid,ask=number(quote['bid']),number(quote['ask'])
    if bid<=0 or ask<bid or buy<=sell:
        raise ValueError('Некорректный диапазон или Bid/Ask')
    minimum=spec.point*spec.stops_level
    if buy<=ask or buy-ask<minimum or sell>=bid or bid-sell<minimum:
        raise ValueError('Диапазон пересечён или нарушает stops; MARKET не создаётся')
    return {'BUY':str(buy),'SELL':str(sell)}
