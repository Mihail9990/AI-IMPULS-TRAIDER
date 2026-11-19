"""Read-only closing-order evidence; deliberately NOT an SL classifier.

Python TradeDeal does not expose MQL5 DEAL_SL. ORDER_SL/ORDER_PRICE_OPEN
are not documented substitutes for the position SL at exit. An exact matching
closing order therefore resolves provenance only, never initial/support status.
No imports of MetaTrader5, initialization, login or trade requests.
"""


class EvidenceUnavailable(ValueError):
    pass


def resolve_closing_order(api, deal, *, account, server, symbol, position_identifier, position_side):
    """Read an exact historical ticket with session checks before/after the call.

    Returns unresolved stop-level evidence even for a proven matching SL order.
    Caller must not advance a recovery counter or create orders from this result.
    Magic is intentionally not used: a user/server-created closing order can have
    magic zero. Authority comes from the caller's persisted position binding.
    """
    def check_session():
        a=api.account_info();t=api.terminal_info()
        if a is None or t is None or not t.connected:
            raise EvidenceUnavailable('Account/terminal unavailable')
        if a.login!=account or a.server!=server:
            raise EvidenceUnavailable('Account/server changed')

    if position_side not in ('BUY','SELL'):
        raise EvidenceUnavailable('Invalid bound side')
    if type(position_identifier) is not int or position_identifier<=0:
        raise EvidenceUnavailable('Invalid bound position identifier')
    if type(getattr(deal,'order',None)) is not int or deal.order<=0:
        raise EvidenceUnavailable('Invalid closing order ticket')
    opposite=api.DEAL_TYPE_SELL if position_side=='BUY' else api.DEAL_TYPE_BUY
    if (deal.entry!=api.DEAL_ENTRY_OUT or deal.reason!=api.DEAL_REASON_SL
            or deal.position_id!=position_identifier or deal.symbol!=symbol or deal.type!=opposite):
        raise EvidenceUnavailable('Deal does not match bound SL exit')
    check_session()
    rows=api.history_orders_get(ticket=deal.order)
    check_session()
    if rows is None or len(rows)!=1:
        raise EvidenceUnavailable('Closing order absent/ambiguous/unavailable')
    order=rows[0]
    expected_type=api.ORDER_TYPE_SELL if position_side=='BUY' else api.ORDER_TYPE_BUY
    if (order.ticket!=deal.order or order.position_id!=position_identifier
            or order.symbol!=symbol or order.reason!=api.ORDER_REASON_SL
            or order.type!=expected_type or order.state!=api.ORDER_STATE_FILLED):
        raise EvidenceUnavailable('Historical order does not prove matching completed SL exit')
    return {'status':'STOP_LEVEL_UNRESOLVED','order_ticket':order.ticket,
            'position_identifier':position_identifier,'symbol':symbol,
            'reason':'Exact closing order linked; Python order fields do not prove position SL at exit',
            'stop_level':None,'close_classification':None}
