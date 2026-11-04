"""Pure ticket-basket configuration and exact, account-currency TP ladder."""
from dataclasses import dataclass,asdict
from decimal import Decimal as D
from .settings import number

@dataclass(frozen=True)
class BasketSettings:
    INITIAL_SIZE: str='0.09'
    INITIAL_ENTRY_DISTANCE: str='1.00'
    INITIAL_SL_DISTANCE: str='3.00'
    INITIAL_TP_DISTANCE: str='0.25'
    BASKET_COUNT: int=6
    TICKET_SIZE: str='0.01'
    RECOVERY_SL_DISTANCE: str='1.50'
    EXPANSION_DISTANCE: str='1.00'
    COST_RESERVE: str='0.00'
    TP_STEP_MULTIPLIER: str='1.00'
    CYCLE_COOLDOWN_SECONDS: str='0'
    def dump(self):return asdict(self)
    @classmethod
    def load(cls,data):
        if not isinstance(data,dict) or set(data)!=set(cls.__dataclass_fields__):raise ValueError('Нужны все параметры, без неизвестных ключей')
        count=data['BASKET_COUNT']
        if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=20:raise ValueError('Число билетов: целое от 1 до 20')
        values={k:number(v) for k,v in data.items() if k!='BASKET_COUNT'}
        for k,v in values.items():
            if v<0 or (v==0 and k not in {'COST_RESERVE','CYCLE_COOLDOWN_SECONDS'}):raise ValueError('Параметры должны быть положительными; резерв и пауза могут быть нулём')
        if values['TP_STEP_MULTIPLIER']<1:raise ValueError('Множитель TP не меньше 1')
        if values['CYCLE_COOLDOWN_SECONDS']%1:raise ValueError('Пауза задаётся целыми секундами')
        return cls(BASKET_COUNT=count,**{k:str(v) for k,v in values.items()})
    def validate(self,spec):
        for key in ('INITIAL_SIZE','TICKET_SIZE'):
            v=number(getattr(self,key))
            if not spec.volume_min<=v<=spec.volume_max or v%spec.volume_step:raise ValueError(key+': объём не соответствует min/step/max брокера; округления нет')
        for key in ('INITIAL_ENTRY_DISTANCE','INITIAL_SL_DISTANCE','INITIAL_TP_DISTANCE','RECOVERY_SL_DISTANCE','EXPANSION_DISTANCE'):
            v=number(getattr(self,key))
            if v%spec.tick_size or v<spec.point*spec.stops_level:raise ValueError(key+': расстояние не соответствует tick/stops брокера; расширения нет')

def ladder(broker,spec,side,entries,sizes,debt,*,reserve='0',multiplier='1'):
    """One common whole-tick step; distinct TP prices cover the whole remaining debt.

    The favorable-most fill anchors the ladder so no ticket's TP starts at a loss.
    Actual fill profits use OrderCalcProfit. Future costs cannot be known in advance;
    reserve is explicit and confirmed residual debt is never forgiven.
    """
    if not entries or len(entries)!=len(sizes):raise ValueError('Нужны фактические входы и объёмы')
    entries=list(map(number,entries));sizes=list(map(number,sizes));sign=1 if side=='BUY' else -1
    anchor=max(entries) if side=='BUY' else min(entries);spec.price(anchor)
    goal=max(D(0),number(debt))+number(reserve)
    def prices(ticks):return [spec.price(anchor+sign*spec.tick_size*ticks*i) for i in range(1,len(entries)+1)]
    def profit(ticks):return sum((number(broker.profit(side,v,e,p)) for e,v,p in zip(entries,sizes,prices(ticks))),D(0))
    low=0;high=1
    while profit(high)<goal:
        high*=2
        if high>2**28:raise ValueError('Невозможно рассчитать покрытие долга')
    while high-low>1:
        mid=(low+high)//2
        if profit(mid)>=goal:high=mid
        else:low=mid
    from decimal import ROUND_CEILING
    ticks=int((D(high)*number(multiplier)).to_integral_value(rounding=ROUND_CEILING))
    return [str(p) for p in prices(ticks)]

def expanded_levels(side,entry,sl,distance):
    entry,sl,d=map(number,(entry,sl,distance))
    return {'BUY':str(sl+d),'SELL':str(entry-d)} if side=='SELL' else {'BUY':str(entry+d),'SELL':str(sl-d)}


def net_money(deals):
    if not deals:raise ValueError('Нет подтверждённых денежных фактов')
    total=D(0)
    for deal in deals:
        if any(deal.get(k) is None for k in ('profit','commission','swap','fee')):raise ValueError('Неизвестны комиссии/своп/прибыль')
        total+=sum((number(deal[k]) for k in ('profit','commission','swap','fee')),D(0))
    return total

def recovery_range_after_sl(side,entry,sl,distance,*,initial):
    """Confirmed v2 prices. sl is the LAST actual closing execution price.

    Initial stop retains actual initial entry on its original side. Subsequent
    recovery stops use last exit +/- distance, regardless of prior entry/side.
    Does not authorize submission or silently round to a broker tick.
    """
    if side not in {'BUY','SELL'} or not isinstance(initial,bool):
        raise ValueError('Нужны BUY/SELL и явный признак начальной позиции')
    entry,sl,d=map(number,(entry,sl,distance))
    if min(entry,sl,d)<=0:raise ValueError('Цены и отступ должны быть положительными')
    buy=entry if initial and side=='BUY' else sl+d
    sell=entry if initial and side=='SELL' else sl-d
    if sell<=0 or buy<=sell:raise ValueError('Диапазон должен иметь положительный SELL ниже BUY')
    return {'BUY':str(buy),'SELL':str(sell)}


def last_closed_sl_price(tickets):
    """Read already reconciled, durable exit facts; reject ambiguous chronology.

    Normalized adapter `time` preserves its native precision (native MT5 ms).
    Never infer chronology from returned list order or lexical/numeric deal IDs.
    """
    if not tickets:raise ValueError('Нет закрытых билетов')
    facts=[];seen=set()
    for ticket in tickets:
        if ticket.get('closed') is not True:raise ValueError('Не все остатки закрыты')
        rows=ticket.get('exit_facts',[])
        if not rows or sum((number(r.get('volume')) for r in rows),D(0))!=number(ticket['size']):raise ValueError('Нет полных подтверждённых закрытий')
        for row in rows:
            did=row.get('id')
            if not isinstance(did,str) or not did or did in seen:raise ValueError('Повторная или неизвестная сделка')
            seen.add(did)
            stamp,price,volume=map(number,(row.get('time'),row.get('price'),row.get('volume')))
            if stamp<0 or price<=0 or volume<=0 or row.get('reason') not in ('SL','TP'):raise ValueError('Некорректные факты закрытия')
            facts.append((stamp,price,row['reason']))
    last=max(t for t,p,r in facts);final=[(p,r) for t,p,r in facts if t==last]
    if len(set(final))!=1:raise ValueError('Порядок последних закрытий неоднозначен')
    price,reason=final[0]
    if reason!='SL':raise ValueError('Последнее закрытие не SL; нужен отдельный подтверждённый переход')
    return str(price)
