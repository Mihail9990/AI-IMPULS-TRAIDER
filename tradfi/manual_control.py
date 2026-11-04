"""Durable, bounded manual close and cycle-local range overrides."""
from copy import deepcopy
from .settings import number
from .broker import Spec
from .control import ActionRejected
from .manual_range import manual_close_range,validate_manual_stops

class ManualRangeControl:
    def request_manual_close(self,a):
        if set(a)!={'type','cycle_id','expected_positions','mode','duration','same_distance','opposite_distance','levels'}:
            raise ActionRejected('Неполные параметры ручного закрытия')
        snap=self._guard(entry=True);c=self.state.get('cycle')
        if not c or a['cycle_id']!=c['id']:raise ActionRejected('Цикл изменился')
        if a['mode'] not in ('main','from_close','manual') or a['duration'] not in ('once','permanent'):
            raise ActionRejected('Неверный режим диапазона')
        if c.get('manual_close') or c.get('edit') or self._unresolved() or snap['orders'] or self.state['stop']:
            raise ActionRejected('Сначала сверка ордеров и предыдущих операций')
        if any(g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') or g.get('actual_ladder_tx',{}).get('status')=='PENDING' for g in c['groups']):
            raise ActionRejected('Сначала подтверждение защиты')
        active=[t for t in c['tickets'].values() if not t['closed']]
        try:
            ids=sorted(t['id'] for t in active)
            if not ids or not isinstance(a['expected_positions'],list) or sorted(a['expected_positions'])!=ids or sorted(p['id'] for p in snap['positions'])!=ids:raise ValueError()
            if len({t['group'] for t in active})!=1 or len({t['side'] for t in active})!=1:raise ValueError()
            g=next(g for g in c['groups'] if g['id']==active[0]['group'])
            selected=[cid for cid in g['commands'] if self.state['commands'][cid]['side']==active[0]['side']]
            if len([t for t in c['tickets'].values() if t['group']==g['id']])!=len(selected):raise ValueError()
            spec=Spec.load(snap['spec']);same=spec.price(a['same_distance']);opposite=spec.price(a['opposite_distance'])
            prior=deepcopy(c.get('range_override'));main=deepcopy(prior['main'] if prior and prior['duration']=='once' else c.get('expanded'))
            if prior and prior['duration']=='once':prior=deepcopy(prior.get('restore_override'))
            if (a['mode']=='main' or a['duration']=='once') and not main:raise ValueError('No existing main range')
            levels=None
            if a['mode']=='main':levels=main
            if a['mode']=='manual':levels=validate_manual_stops(spec,a['levels'],snap['quote'])
            elif a['levels'] is not None:raise ValueError()
            if a['mode']=='main':levels=validate_manual_stops(spec,levels,snap['quote'])
            if a['mode']=='from_close':
                # Preview approximation only; actual fills are checked again after closure.
                quote=snap['quote']['bid' if active[0]['side']=='BUY' else 'ask']
                validate_manual_stops(spec,manual_close_range(active[0]['side'],quote,same,opposite),snap['quote'])
            for t in active:
                p=next(p for p in snap['positions'] if p['id']==t['id'])
                if number(p['size'])!=number(t['size']):raise ValueError()
        except (ValueError,TypeError,KeyError,StopIteration,ArithmeticError):
            raise ActionRejected('Закрытие отклонено: состав корзины, диапазон или расстояния недопустимы. Для разового режима нужен прежний основной диапазон.')
        c['manual_close']=dict(stage='PREPARED',ids=ids,group=g['id'],side=active[0]['side'],mode=a['mode'],duration=a['duration'],
            same_distance=str(same),opposite_distance=str(opposite),levels=levels,main=main,prior_override=prior,close_ids=[])
        self.state['manual_close_settings']={k:c['manual_close'][k] for k in ('mode','duration','same_distance','opposite_distance')}
        self.save('manual_close_prepared')

    def _advance_manual_close(self,snap):
        c=self.state['cycle'];tx=c['manual_close']
        targets=[c['tickets'][pid] for pid in tx['ids']]
        if any(t['closed'] and t['reason']!='MANUAL' for t in targets):
            self._halt(snap,'MANUAL_CLOSE_NATURAL_EXIT_RACE');return
        if tx['stage']=='PREPARED':
            if self.state['stop']:
                c.pop('manual_close');self.save('manual_close_stopped_before_send');return
            if snap['orders'] or self._unresolved():return
            if sorted(p['id'] for p in snap['positions'])!=tx['ids'] or any(t['closed'] for t in targets):
                self._halt(snap,'MANUAL_CLOSE_EXPOSURE_CHANGED');return
            if any(number(next(p for p in snap['positions'] if p['id']==t['id'])['size'])!=number(t['size']) for t in targets):
                self._halt(snap,'MANUAL_CLOSE_VOLUME_CHANGED');return
            for t in targets:
                cid=self._intent('CLOSE',position_id=t['id'],size=t['size'],purpose='MANUAL')
                tx['close_ids'].append(cid);c.setdefault('cleanup_ids',[]).append(cid)
            tx['stage']='WAIT';self.save('manual_close_intents');return
        if any(self.state['commands'][cid]['status']=='REJECTED' for cid in tx['close_ids']):
            self._halt(snap,'MANUAL_CLOSE_PARTIAL_FAILURE');return
        if not all(t['closed'] for t in targets) or not self._resolved(tx['close_ids']) or self._unresolved():return
        if snap['positions'] or snap['orders']:
            self._halt(snap,'MANUAL_CLOSE_RESIDUAL_EXPOSURE');return
        facts=[f for t in targets for f in t.get('exit_facts',[])]
        try:
            if not facts:raise ValueError()
            stamp=max(number(f['time']) for f in facts)
            prices={number(f['price']) for f in facts if number(f['time'])==stamp}
            if len(prices)!=1:raise ValueError()
            last=str(prices.pop())
            if number(last)<=0:raise ValueError()
        except (ValueError,KeyError,ArithmeticError):
            self._halt(snap,'MANUAL_CLOSE_LAST_EXIT_AMBIGUOUS');return
        if number(c['recovery'])<=0:
            tx['stage']='DEBT_COVERED';self.save('manual_close_debt_covered');self._finish(snap);return
        try:
            levels=manual_close_range(tx['side'],last,tx['same_distance'],tx['opposite_distance']) if tx['mode']=='from_close' else tx['levels']
        except (ValueError,ArithmeticError):
            self._halt(snap,'MANUAL_CLOSE_RANGE_INVALID_AFTER_FILL');return
        # _arm validates the whole STOP pair and SL/TP at fresh quotes. Crossed
        # levels wait; no MARKET or implicit widening, even after a gap.
        c['range_override']=dict(duration=tx['duration'],levels=deepcopy(levels),main=tx['main'],restore_override=tx['prior_override'],group=None)
        c['expanded']=deepcopy(levels)
        for g in c['groups']:g['retired']=True
        c.setdefault('manual_close_history',[]).append(dict(tx,last_exit=last,stage='CONFIRMED'))
        c.pop('manual_close');self.save('manual_close_range_confirmed')

    def _on_group_armed(self,group):
        override=self.state['cycle'].get('range_override')
        if override and group['role']!='initial' and override['group'] is None:
            override['group']=group['id']

    def _restore_manual_range(self,snap,g,tickets):
        """Return True when a confirmed override owns the next range."""
        c=self.state['cycle'];override=c.get('range_override')
        if not override:return False
        if override['duration']=='permanent':
            c['expanded']=deepcopy(override['levels']);override['group']=None
        elif override['group']==g['id']:
            c['expanded']=deepcopy(override['main'])
            prior=override.get('restore_override')
            if prior:
                c['range_override']=deepcopy(prior);c['range_override']['group']=None
            else:c.pop('range_override')
        else:
            self._halt(snap,'MANUAL_RANGE_GROUP_MISMATCH');return True
        self.save('manual_range_after_closed_basket');return True
