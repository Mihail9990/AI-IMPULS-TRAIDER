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
    """Proposed v2 range, pure calculation only; not an execution authorization.

    Initial stop: keep the former entry on the same side, opposite beyond SL.
    Subsequent stop: move the same side beyond its former entry too. Recompute
    for each stopped basket; never reuse the v1 once-frozen expanded range.

    Entry uses volume-weighted actual fills, SL uses actual closing executions.
    Mixed closing-price aggregation and the v2 TP rules await clarification.
    Caller must also reconcile flatness and OCO obligations before submission, so
    this planner is deliberately not wired into the current v1 controller.
    """
    if side not in {'BUY','SELL'} or not isinstance(initial,bool):
        raise ValueError('Нужны BUY/SELL и явный признак начальной позиции')
    entry,sl,d=map(number,(entry,sl,distance))
    if min(entry,sl,d)<=0:raise ValueError('Цены и отступ должны быть положительными')
    if side=='BUY':
        buy=entry if initial else entry+d
        sell=sl-d
    else:
        buy=sl+d
        sell=entry if initial else entry-d
    if sell<=0 or buy<=sell:raise ValueError('Диапазон должен иметь положительный SELL ниже BUY')
    return {'BUY':str(buy),'SELL':str(sell)}

def confirmed_stop_anchors(tickets,deals,proofs):
    """Derive v2 anchors from normalized broker facts, never requested prices.

    `proofs` must be the adapter's reconciled per-command evidence, not UI data.
    A caller still must reconcile all pending/UNKNOWN obligations and flatness.
    Until clarified, mixed TP/SL baskets or distinct SL execution prices refuse
    planning rather than choosing a hidden aggregation rule.
    """
    if not tickets or not isinstance(proofs,dict):raise ValueError('Нет подтверждённых билетов')
    ids=[t.get('id') for t in tickets];commands=[t.get('command_id') for t in tickets]
    if any(not isinstance(v,str) or not v for v in ids+commands) or len(set(ids))!=len(ids) or len(set(commands))!=len(commands):raise ValueError('Неоднозначная принадлежность билетов')
    sides={t.get('side') for t in tickets}
    if len(sides)!=1 or not sides<={'BUY','SELL'}:raise ValueError('Нужна одна подтверждённая сторона корзины')
    side=next(iter(sides));volume=D(0);weighted_entry=D(0);exit_prices=set();seen=set()
    for t in tickets:
        if proofs.get(t['command_id'],{}).get('status')!='CLOSED_EVIDENCE':raise ValueError('Закрытие билета ещё не подтверждено')
        expected=number(t['size'])
        if expected<=0:raise ValueError('Неверный ожидаемый объём билета')
        rows=[d for d in deals if d.get('position_id')==t['id']]
        ins=[];outs=[]
        for d in rows:
            did=d.get('id')
            if not isinstance(did,str) or not did or did in seen:raise ValueError('Неизвестный или повторный ID сделки')
            seen.add(did)
            size,price=number(d.get('volume')),number(d.get('price'))
            if size<=0 or price<=0:raise ValueError('Неверные фактические цена или объём')
            if d.get('entry')=='IN':
                if d.get('command_id')!=t['command_id'] or d.get('side')!=side:raise ValueError('Вход не соответствует билету')
                ins.append((size,price))
            elif d.get('entry')=='OUT':
                if d.get('reason')!='SL':raise ValueError('Для смешанных TP/SL нужен подтверждённый выбор якорей')
                outs.append((size,price));exit_prices.add(price)
            else:raise ValueError('Неподдерживаемый тип исполнения')
        if not ins or not outs or sum((v for v,p in ins),D(0))!=expected or sum((v for v,p in outs),D(0))!=expected:
            raise ValueError('Неполные или противоречивые объёмы входа/закрытия')
        volume+=expected;weighted_entry+=sum((v*p for v,p in ins),D(0))
    if len(exit_prices)!=1:raise ValueError('SL исполнились по разным ценам: правило объединения ещё не подтверждено')
    return {'side':side,'entry':str(weighted_entry/volume),'sl_exit':str(next(iter(exit_prices))),'volume':str(volume)}
