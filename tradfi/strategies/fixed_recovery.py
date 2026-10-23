"""Pure money-based recovery. No broker sends, price guesses or forgiven debt."""
from dataclasses import dataclass
from decimal import Decimal as D
from ..settings import number


@dataclass(frozen=True)
class RecoveryConfig:
    parts: int = 3
    after_sl: str = '3.00'
    continuation: str = '1.00'
    opposite: str = '3.00'
    fixed_sl: str = '3.00'

    def dump(self): return dict(parts=self.parts,after_sl=self.after_sl,continuation=self.continuation,opposite=self.opposite,fixed_sl=self.fixed_sl)

    @classmethod
    def load(cls, data):
        if set(data)!=set(cls.__dataclass_fields__): raise ValueError('Complete recovery configuration required')
        n=data['parts']
        if isinstance(n,bool) or not isinstance(n,int) or not 1<=n<=100:
            raise ValueError('Recovery N must be an integer from 1 to 100')
        distances={k:str(number(v)) for k,v in data.items() if k!='parts'}
        if any(number(v)<=0 for v in distances.values()): raise ValueError('Positive distances required')
        return cls(parts=n,**distances)


def initial(config):
    return dict(debt='0',parts_remaining=config.parts,parts=config.parts,realized_net='0',active=False,rounds=[])


def installment(state):
    debt=number(state['debt'])
    return max(D(0),debt)/max(1,int(state['parts_remaining']))


def resize(state, parts):
    if isinstance(parts,bool) or not isinstance(parts,int) or not 1<=parts<=100: raise ValueError('Invalid N')
    return dict(state,parts=parts,parts_remaining=parts)  # paid profit and outstanding debt unchanged


def net_money(deals):
    """All confirmed costs are mandatory; unknown costs must not become zero."""
    if not deals: raise ValueError('Confirmed deal facts required')
    total=D(0)
    for deal in deals:
        if any(key not in deal or deal[key] is None for key in ('profit','commission','swap','fee')):
            raise ValueError('Complete confirmed profit/commission/swap/fee required')
        total+=sum((number(deal[k]) for k in ('profit','commission','swap','fee')),D(0))
    return total


def settle(state, *, round_id, reason, net):
    if reason not in {'TP','SL','MANUAL'}: raise ValueError('Explicit confirmed closure classification required')
    if round_id in {r['id'] for r in state['rounds']}: raise ValueError('Round already settled')
    net=number(net); debt=max(D(0),number(state['debt'])-net)
    remaining=max(1,state['parts_remaining']-1) if reason=='TP' else state['parts']
    return dict(state,debt=str(debt),parts_remaining=remaining,
        active=bool(debt>0),realized_net=str(number(state['realized_net'])+net),
        rounds=[*state['rounds'],dict(id=round_id,reason=reason,net=str(net))])
