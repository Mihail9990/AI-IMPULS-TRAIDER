"""Read-only panel projection of already-booked basket accounting.

Never changes the ledger, reconstructs broker deals, or marks positions to market.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation


def accounting_view(state):
    cycle=state.get('cycle') or {}
    currency=cycle.get('currency') or state.get('currency')
    cycle_net=None
    if cycle:
        try:
            amounts=[]
            for ticket in cycle.get('tickets',{}).values():
                amounts.append(Decimal(str(ticket['opening_net'])))
                if ticket.get('closed'):
                    amounts.append(Decimal(str(ticket['exit_net'])))
            if all(amount.is_finite() for amount in amounts):
                cycle_net=str(sum(amounts,Decimal(0)))
        except (KeyError,TypeError,ValueError,InvalidOperation):
            pass  # Older/incomplete records are unknown, not zero profit.
    keys=('id','contract','currency','realized','recovery','recovery_retained',
          'written_off_recovery','completion')
    history=[{key:deepcopy(row[key]) for key in keys if key in row}
             for row in state.get('completed',[])[-100:]]
    return dict(metrics=dict(recovery_total=cycle.get('recovery',state.get('basket_carry','0')),
                cycle_net=cycle_net,currency=currency,
                cycle_net_basis='BOOKED_TICKET_NET_EXCLUDING_CARRY_AND_FLOATING'),
                cycle_history=history)
