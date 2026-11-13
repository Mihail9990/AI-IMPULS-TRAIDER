"""Exact, saved recovery standards. A request owns a detached immutable snapshot.

Profiles never derive TP from debt, redistribute volume, or round user values.
The existing 20 independent-ticket safety limit remains in force.
"""
from copy import deepcopy
from decimal import Decimal as D
import re
from .settings import number


def defaults(sl_distance='1.50'):
    return dict(selected_profile_id='six', recovery_sl_distance=str(sl_distance), profiles=[
        dict(id='six', name='6 × 0.01', stages=[dict(volume='0.01', tp_distance=str(D('.25')*i)) for i in range(1,7)]),
        dict(id='mixed', name='0.05 + 0.02 + 0.01', stages=[dict(volume=v, tp_distance=t) for v,t in [('0.05','0.25'),('0.02','0.50'),('0.01','0.75')]]),
        dict(id='single', name='1 × 0.10', stages=[dict(volume='0.10', tp_distance='0.25')]),
    ])


def validate_profile(profile, spec):
    if not isinstance(profile,dict) or set(profile)!={'id','name','stages'}:
        raise ValueError('Профиль: нужны id, name и stages')
    if not isinstance(profile['id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',profile['id']):
        raise ValueError('Недопустимый ID профиля')
    if not isinstance(profile['name'],str) or not profile['name'].strip() or len(profile['name'])>80:
        raise ValueError('Название профиля: 1–80 символов')
    stages=profile['stages']
    if not isinstance(stages,list) or not 1<=len(stages)<=20:
        raise ValueError('Профиль: от 1 до 20 ступеней')
    result=[]
    for stage in stages:
        if not isinstance(stage,dict) or set(stage)!={'volume','tp_distance'}:
            raise ValueError('Каждая ступень: точный объём и расстояние TP')
        volume=number(stage['volume']);distance=spec.price(stage['tp_distance'])
        if not spec.volume_min<=volume<=spec.volume_max or volume%spec.volume_step:
            raise ValueError('Объём ступени не соответствует min/step/max брокера; округления нет')
        if distance<spec.point*spec.stops_level:
            raise ValueError('TP ступени меньше stops брокера')
        if result and distance<=number(result[-1]['tp_distance']):
            raise ValueError('Расстояния TP должны возрастать по номеру ступени')
        result.append(dict(volume=str(volume),tp_distance=str(distance)))
    return dict(id=profile['id'],name=profile['name'].strip(),stages=result)


def validate_settings(settings,spec):
    if not isinstance(settings,dict) or set(settings)!={'selected_profile_id','profiles','recovery_sl_distance'}:
        raise ValueError('Нужны профили, выбранный профиль и RECOVERY SL')
    profiles=settings['profiles']
    if not isinstance(profiles,list) or not 1<=len(profiles)<=20:
        raise ValueError('Сохраните от 1 до 20 профилей')
    profiles=[validate_profile(p,spec) for p in profiles]
    ids=[p['id'] for p in profiles]
    if len(set(ids))!=len(ids) or settings['selected_profile_id'] not in ids:
        raise ValueError('ID профилей должны быть уникальны; выберите существующий профиль')
    sl=spec.price(settings['recovery_sl_distance'])
    if sl<spec.point*spec.stops_level:raise ValueError('RECOVERY SL меньше stops брокера')
    return dict(selected_profile_id=settings['selected_profile_id'],profiles=profiles,recovery_sl_distance=str(sl))


def selected_snapshot(settings,spec=None):
    profile=next(p for p in settings['profiles'] if p['id']==settings['selected_profile_id'])
    if spec is not None:profile=validate_profile(profile,spec)
    profile=deepcopy(profile)
    sl=number(settings['recovery_sl_distance'])
    if spec is not None:
        spec.price(sl)
        if sl<spec.point*spec.stops_level:raise ValueError('RECOVERY SL меньше stops брокера')
    profile.update(total_volume=str(sum((number(s['volume']) for s in profile['stages']),D(0))),sl_distance=str(sl))
    return profile


def validate_snapshot(snapshot,spec):
    if not isinstance(snapshot,dict) or set(snapshot)!={'id','name','stages','total_volume','sl_distance'}:
        raise ValueError('Неполный снимок профиля RECOVERY')
    profile={k:snapshot[k] for k in ('id','name','stages')}
    result=selected_snapshot(dict(profiles=[profile],selected_profile_id=profile['id'],recovery_sl_distance=snapshot['sl_distance']),spec)
    if number(snapshot['total_volume'])!=number(result['total_volume']):raise ValueError('Объём снимка профиля не совпадает со ступенями')
    return result


def exact_targets(spec,snapshot,side,tickets,overrides=None):
    """Use original stage identities, even after out-of-order fills/cancellation."""
    snapshot=validate_snapshot(snapshot,spec)
    if side not in ('BUY','SELL'):raise ValueError('Нужна сторона BUY/SELL')
    sign=1 if side=='BUY' else -1;result=[];seen=set();overrides=overrides or {}
    for ticket in tickets:
        i=ticket['index']
        if type(i) is not int or i in seen or not 0<=i<len(snapshot['stages']):raise ValueError('Неверная идентичность ступени')
        seen.add(i);stage=snapshot['stages'][i]
        if number(ticket['size'])!=number(stage['volume']):raise ValueError('Объём исполнения не совпадает со ступенью')
        distance=spec.price(overrides.get(str(i),stage['tp_distance']))
        result.append(str(spec.price(spec.price(ticket['entry'])+sign*distance)))
    return result
