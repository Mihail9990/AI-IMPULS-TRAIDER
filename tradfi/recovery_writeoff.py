"""Explicit flat-only recovery waiver. Never changes realized broker P/L."""
from copy import deepcopy
from decimal import Decimal as D
import uuid
from .control import ActionRejected,TransientDataWait
from .settings import number
from .basket_math import BasketSettings,net_money
from .broker import Spec


def _reject(message):
    # Only fixed, user-safe reasons reach the control receipt. Never expose a
    # broker/API exception, account details, or a raw persisted payload here.
    raise ActionRejected(message,public_message=message)


class RecoveryWriteoff:
    def _has_preservable_entry(self):
        c=self.state.get('cycle') or {}
        return bool(c.get('v3_request') or any(self.state['commands'][cid]['kind']=='STOP' and self.state['commands'][cid]['status']=='PENDING' for cid in c.get('entries',[])))

    def can_writeoff_recovery(self,snap):
        s=self.state;c=s.get('cycle') or {}
        try:debt=number(c.get('recovery',s.get('basket_carry','0')))
        except (ValueError,TypeError,ArithmeticError):return False
        return bool(debt>0 and not snap.get('positions') and not getattr(self.broker,'read_only',False)
            and not s.get('migration_block') and not s.get('time_block') and not c.get('manual')
            and s.get('phase') not in {'MANUAL','SAFE_PAUSE','MIGRATION_BLOCK'} and not self._uncertain()
            and not any(r.get('status')=='UNKNOWN' for r in s.get('control_receipts',{}).values())
            and not any(c.get(k) for k in ('edit','manual_close','initial_cancel','v3_edit','v3_rearm','v3_relink'))
            and (self._has_preservable_entry() or ((s.get('stop') or c.get('recovery_mode')=='V3') and not snap.get('orders') and not self._unresolved())))

    def _writeoff_keep_entries(self,debt):
        s=self.state;c=s['cycle']
        if c.get('recovery_mode')!='V3' or s.get('migration_block') or getattr(self.broker,'read_only',False):_reject('Списание недоступно')
        if s.get('phase') in {'MANUAL','SAFE_PAUSE','MIGRATION_BLOCK'} or c.get('manual') or s.get('time_block'):_reject('Сначала завершите сверку')
        if self._uncertain() or any(r.get('status')=='UNKNOWN' and rid!=getattr(self,'_active_control_id',None) for rid,r in s.get('control_receipts',{}).items()):_reject('Предыдущий исход не сверён')
        if any(c.get(k) for k in ('edit','manual_close','initial_cancel','v3_edit','v3_rearm','v3_relink')) or any(g.get('cancel_entry') or g.get('actual_ladder_tx',{}).get('status')=='PENDING' or g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') for g in c['groups']):_reject('Дождитесь завершения правки или отмены')
        if any((c.get('v3_link') or {}).get(k) for k in ('cancel','replace')):_reject('Дождитесь сверки связанных триггеров')
        request=c.get('v3_request')
        if request:
            required={'order_type','timing','side','count','distance','bar_id'}
            if not isinstance(request,dict) or not required<=set(request) or set(request)-required-{'recovery_profile'} or request.get('order_type') not in {'STOP','MARKET'} or request.get('timing') not in {'current','next'} or request.get('side') not in {'BUY','SELL'} or isinstance(request.get('bar_id'),bool) or not isinstance(request.get('bar_id'),int):_reject('Запланированная операция не сверена; сброс недоступен')
        def check(snap):
            if snap['positions']:raise ValueError()
            pending={}
            for cid in c['entries']:
                cmd=s['commands'][cid]
                if cmd['status']=='FILLED' and not any(t.get('command_id')==cid and t.get('closed') for t in c['tickets'].values()):raise ValueError()
                if cmd['status']=='PENDING':
                    matches=self._pending(snap,[cid])
                    if cmd['kind']!='STOP' or len(matches)!=1:raise ValueError()
                    pending[cid]=matches[0]
                elif cmd['status'] not in {'FILLED','CANCELLED','REJECTED','OBSOLETE'}:raise ValueError()
                if self.evidence_contract=='per_obligation_v1':
                    proof=snap.get('obligations',{}).get(cid,{})
                    if proof.get('status')=='CLOSED_EVIDENCE' and proof.get('position_id') not in c['tickets']:raise ValueError()
                    allowed={'CLOSED_EVIDENCE','TERMINAL_UNFILLED_EVIDENCE','REJECTED_RESPONSE'}
                    if cid in pending:allowed={'PENDING_OBSERVED'}
                    if proof.get('status') not in allowed and not (proof.get('status')=='NO_SEND_EVIDENCE' and cmd.get('never_sent')):raise ValueError()
            if {o['id'] for o in pending.values()}!={o['id'] for o in snap['orders']}:raise ValueError()
            total=-number(s.get('basket_carry','0'))
            for ticket in c['tickets'].values():
                if not ticket.get('closed') or not ticket.get('exit_facts'):raise ValueError()
                outs=[d for d in snap['deals'] if d['entry']=='OUT' and d['position_id']==ticket['id']]
                if {d['id'] for d in outs}!={d['id'] for d in ticket['exit_facts']} or sum((number(d['volume']) for d in outs),D(0))!=number(ticket['size']) or net_money(outs)!=number(ticket['exit_net']):raise ValueError()
                total+=number(ticket['opening_net'])+number(ticket['exit_net'])
            if total!=number(c['realized']) or max(D(0),-total-number(c.get('recovery_waived','0')))!=debt:raise ValueError()
            return deepcopy(snap['orders'])
        try:
            first=self._guard(entry=True);orders=check(first)
            # A second read catches fills occurring during the first snapshot.
            snap=self._guard(entry=True)
            if check(snap)!=orders:raise ValueError()
        except TransientDataWait:raise
        except (ActionRejected,ValueError,KeyError,TypeError,ArithmeticError,OSError,TimeoutError):_reject('Позиции, ордера или факты изменились; сброс не выполнен, обновите снимок')
        self._deadline_guard();self._time_guard()
        if s.get('time_block') or s.get('phase') in {'MANUAL','SAFE_PAUSE','MIGRATION_BLOCK'} or c.get('manual'):_reject('Активна защитная блокировка')
        before=deepcopy(s)
        c['recovery_waived']=str(number(c.get('recovery_waived','0'))+debt);c['recovery']='0';c['recovery_waived_keep_pending']=True
        s.setdefault('recovery_writeoffs',[]).append(dict(id=uuid.uuid4().hex,cycle_id=c['id'],amount=str(debt),currency=s['currency'],at_utc=snap['now'],realized_before=c['realized'],accounting='WAIVE_RECOVERY_KEEP_PENDING_NO_PNL_CHANGE'))
        try:self.save('recovery_waived_pending_preserved')
        except BaseException:
            self.state=self.store.load() or before;self._durable_state=deepcopy(self.state);raise

    def writeoff_recovery(self,a):
        if set(a)!={'type','cycle_id','expected_debt','confirmed'} or a.get('confirmed') is not True:
            _reject('Нужно явное подтверждение списания RECOVERY')
        s=self.state;c=s.get('cycle')
        try:expected=number(a['expected_debt']);debt=number(c['recovery'] if c else s.get('basket_carry','0'))
        except (ValueError,TypeError,KeyError,ArithmeticError):_reject('Неверная сумма RECOVERY')
        if a['cycle_id']!=(c or {}).get('id') or expected!=debt or debt<=0:
            _reject('Цикл или остаток изменился; обновите подтверждение')
        if self._has_preservable_entry():return self._writeoff_keep_entries(debt)
        if s.get('migration_block') or getattr(self.broker,'read_only',False):
            _reject('Списание недоступно в READ_ONLY или при миграции')
        if s.get('phase') in {'MANUAL','SAFE_PAUSE','MIGRATION_BLOCK'} or (c or {}).get('manual'):
            _reject('Списание недоступно: активна защитная блокировка; сначала ручная сверка')
        uncertain_actions=any(r.get('status')=='UNKNOWN' and rid!=getattr(self,'_active_control_id',None) for rid,r in s.get('control_receipts',{}).items())
        if (not s.get('stop') and (c or {}).get('recovery_mode')!='V3') or self._unresolved() or uncertain_actions:
            _reject('Остановите новые входы и дождитесь сверки всех обязательств')
        if self._time_guard():
            if self._time_waiting():raise TransientDataWait('Ожидаются свежие данные M1')
            _reject('Сначала подтвердите время; новый цикл заблокирован')
        if c and any(c.get(k) for k in ('edit','manual_close','initial_cancel','v3_edit','v3_request','v3_link','v3_rearm','v3_relink')):
            _reject('Предыдущая операция ещё не завершена; дождитесь сверки или отмените запланированный вход')
        if c and any(g.get('actual_ladder_tx',{}).get('status')=='PENDING' or g.get('entry_sl_once',{}).get('status') in ('PREPARED','PENDING') for g in c['groups']):
            _reject('Изменение защиты ещё не сверено; списание RECOVERY пока недоступно')
        # Waiver resumes new-cycle waiting, so apply the same live safety and
        # current strategy validation as Start, not archived legacy TP slots.
        try:
            snap=self._guard(entry=True)
            spec=Spec.load(snap['spec'])
            boundary=max(snap['candle']['id'],int(number(snap['now'])//60)*60-60)
        except TransientDataWait:raise
        except ActionRejected:
            _reject('Списание недоступно: активна защитная блокировка; проверьте статус, время и завершите сверку')
        except (ValueError,TypeError,KeyError,ArithmeticError,OSError,TimeoutError):
            _reject('Не удалось подтвердить свежие данные брокера; RECOVERY сохранён, сначала сверка')
        if snap['positions'] or snap['orders']:
            _reject('Остались позиции или ордера; списание RECOVERY разрешено только после их подтверждённого завершения')
        try:self._validate_cfg(BasketSettings.load(s['basket_settings']),spec)
        except (ValueError,TypeError,KeyError,ArithmeticError):
            _reject('Параметры начального входа не соответствуют брокеру; проверьте объём, TP и SL. Округления нет.')
        try:self._validate_start_ladder(spec)
        except (ValueError,TypeError,KeyError,StopIteration,ArithmeticError):
            _reject('Выбранный профиль RECOVERY или сетка TP не соответствует брокеру; проверьте объёмы, TP и SL. Округления нет.')
        try:
            if c:
                tickets=c['tickets']
                for t in tickets.values():
                    if not t.get('closed') or not t.get('exit_facts'):raise ValueError()
                    outs=[d for d in snap['deals'] if d['entry']=='OUT' and d['position_id']==t['id']]
                    if {d['id'] for d in outs}!={d['id'] for d in t['exit_facts']}:raise ValueError()
                    if sum((number(d['volume']) for d in outs),D(0))!=number(t['size']):raise ValueError()
                    if net_money(outs)!=number(t['exit_net']):raise ValueError()
                if self.evidence_contract=='per_obligation_v1':
                    for cid in c['entries']:
                        cmd=s['commands'][cid];proof=snap['obligations'].get(cid,{})
                        status=proof.get('status')
                        if status=='CLOSED_EVIDENCE':
                            if proof.get('position_id') not in tickets:raise ValueError()
                        elif status not in {'TERMINAL_UNFILLED_EVIDENCE','REJECTED_RESPONSE'}:
                            if not (status=='NO_SEND_EVIDENCE' and cmd.get('never_sent') is True):raise ValueError()
                total=sum((number(t['opening_net'])+number(t['exit_net']) for t in tickets.values()),D(0))-number(s.get('basket_carry','0'))
                if total!=number(c['realized']) or max(D(0),-total-number(c.get('recovery_waived','0')))!=debt:raise ValueError()
        except (ValueError,TypeError,KeyError,ArithmeticError,OSError,TimeoutError):
            _reject('Факты закрытий или итог RECOVERY не совпадают с учётом; долг сохранён, сначала ручная сверка')
        # Broker readback can span a deadline or clock-safety change. Latch the
        # guards again after all reads, without starting another broker read,
        # before clearing debt or allowing a new cycle.
        self._deadline_guard();self._time_guard()
        if s.get('time_block'):
            _reject('Сначала подтвердите время; новый цикл заблокирован')
        if s.get('phase') in {'MANUAL','SAFE_PAUSE','MIGRATION_BLOCK'} or (c or {}).get('manual'):
            _reject('Списание недоступно: активна защитная блокировка; сначала ручная сверка')
        if s.get('migration_block') or getattr(self.broker,'read_only',False):
            _reject('Списание недоступно в READ_ONLY или при миграции')
        event=dict(id=uuid.uuid4().hex,cycle_id=(c or {}).get('id'),amount=str(debt),currency=s['currency'],at_utc=snap['now'],realized_before=(c or {}).get('realized'),accounting='WAIVE_RECOVERY_ONLY_NO_PNL_DEBIT')
        before=deepcopy(s)
        if c:
            s['completed'].append(dict(id=c['id'],contract=c['contract'],currency=s['currency'],realized=c['realized'],recovery='0',written_off_recovery=str(debt),completion='RECOVERY_WAIVED'))
        s.setdefault('recovery_writeoffs',[]).append(event)
        s.update(basket_carry='0',cycle=None,phase='WAIT_CANDLE',stop=False,stop_mode='',last_candle=boundary)
        try:self.save('recovery_waived_without_pnl_debit')
        except BaseException:
            self.state=self.store.load() or before
            self._durable_state=deepcopy(self.state)
            raise
