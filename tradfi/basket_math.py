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

def configured_ladder(broker,spec,side,entries,sizes,debt,distances,*,quote=None,modify=False,active_mask=None):
    """Interpolate distances between fixed slots; solve a uniform trailing step.

    Every price is based on its own actual entry. Manual distances never move.
    Interpolation must be exact on the broker tick, otherwise reject. The tail
    uses the smallest admissible whole-tick step covering actual recovery debt.
    Optional fresh quote validates TP-side stop/freeze; SL needs separate checks.
    """
    from decimal import ROUND_FLOOR,ROUND_CEILING
    if side not in ('BUY','SELL') or not entries or len(entries)!=len(sizes) or not isinstance(distances,list) or len(distances)!=len(entries):raise ValueError('Нужен один параметр ступени на билет')
    if active_mask is None:active_mask=[True]*len(entries)
    if not isinstance(active_mask,list) or len(active_mask)!=len(entries) or any(type(v) is not bool for v in active_mask) or not any(active_mask):raise ValueError('Нужна маска подтверждённых остатков')
    if distances[0] is None:raise ValueError('Первая TP-ступень обязательна')
    if modify and quote is None:raise ValueError('Для изменения TP нужна свежая котировка')
    tick=number(spec.tick_size);goal=number(debt)
    entries=[spec.price(e) for e in entries];sizes=list(map(number,sizes))
    if goal<0 or any(v<spec.volume_min or v>spec.volume_max or v%spec.volume_step for v in sizes):raise ValueError('Некорректные фактические объёмы/долг')
    fixed={i:number(d) for i,d in enumerate(distances) if d is not None}
    if any(d<=0 or d%tick for d in fixed.values()):raise ValueError('Расстояния должны быть положительными и точными по tick')
    sign=1 if side=='BUY' else -1;last=max(fixed)
    offsets=[None]*len(entries)
    for i,d in fixed.items():offsets[i]=d
    indexes=sorted(fixed)
    for left,right in zip(indexes,indexes[1:]):
        step=(fixed[right]-fixed[left])/D(right-left)
        if step<=0 or step%tick:raise ValueError('Равномерная интерполяция между заданными ступенями невозможна по tick; ручные уровни сохранены')
        for i in range(left+1,right):offsets[i]=fixed[left]+D(i-left)*step
    minimum=spec.point*(max(spec.stops_level,spec.freeze_level) if modify else spec.stops_level)
    executable=number(quote['bid' if side=='BUY' else 'ask']) if quote is not None else None
    if executable is not None and executable<=0:raise ValueError('Некорректная котировка')
    def validate(values):
        if any(sign*(b-a)<=0 for a,b in zip(values,values[1:])):raise ValueError('Фактические TP нарушают порядок ступеней; ручные уровни сохранены')
        for i,(entry,price) in enumerate(zip(entries,values)):
            if not active_mask[i]:continue
            gap=sign*(price-(executable if executable is not None else entry))
            if gap<=0 or gap<minimum:raise ValueError('TP пересечён или нарушает stop/freeze; заданные уровни не сдвинуты')
        return values
    prefix=[spec.price(entries[i]+sign*offsets[i]) for i in range(last+1)]
    validate(prefix)
    lower=1;upper=2**28
    for i in range(last+1,len(entries)):
        k=D(i-last)
        lower=max(lower,int((-D(sign)*(entries[i]-entries[i-1])/tick).to_integral_value(rounding=ROUND_FLOOR))+1)
        q=executable if executable is not None else entries[i]
        gap=D(sign)*(entries[i]-q)+fixed[last]
        if active_mask[i]:lower=max(lower,int(((minimum-gap)/(k*tick)).to_integral_value(rounding=ROUND_CEILING)),int((-gap/(k*tick)).to_integral_value(rounding=ROUND_FLOOR))+1)
        if side=='SELL':upper=min(upper,int(((entries[i]-fixed[last])/(k*tick)).to_integral_value(rounding=ROUND_CEILING))-1)
    if lower>upper:raise ValueError('Нет положительной упорядоченной сетки в ограничениях брокера')
    def prices(step):
        ds=[offsets[i] if i<=last else fixed[last]+D(i-last)*tick*step for i in range(len(entries))]
        return validate([spec.price(e+sign*d) for e,d in zip(entries,ds)])
    def profit(step):return sum((number(broker.profit(side,v,e,p)) for e,v,p,active in zip(entries,sizes,prices(step),active_mask) if active),D(0))
    if last==len(entries)-1:
        if profit(lower)<goal:raise ValueError('Заданные и интерполированные ступени не покрывают RECOVERY; свободного продолжения нет')
        return [str(p) for p in prices(lower)]
    low=lower;high=lower
    if profit(low)>=goal:return [str(p) for p in prices(low)]
    while profit(high)<goal:
        if high==upper:raise ValueError('Не удалось покрыть RECOVERY в допустимом диапазоне цен')
        high=min(upper,high*2)
    while high-low>1:
        mid=(low+high)//2
        if profit(mid)>=goal:high=mid
        else:low=mid
    return [str(p) for p in prices(high)]
