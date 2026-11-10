"""Explicit flat-only recovery waiver. Never changes realized broker P/L."""
from copy import deepcopy
from decimal import Decimal as D
import uuid
from .control import ActionRejected
from .settings import number
from .basket_math import BasketSettings,net_money
from .broker import Spec


def _reject(message):
    # Only fixed, user-safe reasons reach the control receipt. Never expose a
    # broker/API exception, account details, or a raw persisted payload here.
    raise ActionRejected(message,public_message=message)


class RecoveryWriteoff:
    def writeoff_recovery(self,a):
        if set(a)!={'type','cycle_id','expected_debt','confirmed'} or a.get('confirmed') is not True:
            _reject('Нужно явное подтверждение списания RECOVERY')
        s=self.state;c=s.get('cycle')
        try:expected=number(a['expected_debt']);debt=number(c['recovery'] if c else s.get('basket_carry','0'))
        except (ValueError,TypeError,KeyError,ArithmeticError):_reject('Неверная сумма RECOVERY')
        if a['cycle_id']!=(c or {}).get('id') or expected!=debt or debt<=0:
            _reject('Цикл или остаток изменился; обновите подтверждение')
        if s.get('migration_block') or getattr(self.broker,'read_only',False):
            _reject('Списание недоступно в READ_ONLY или при миграции')
        if s.get('phase') in {'MANUAL','SAFE_PAUSE','MIGRATION_BLOCK'} or (c or {}).get('manual'):
            _reject('Списание недоступно: активна защитная блокировка; сначала ручная сверка')
        uncertain_actions=any(r.get('status')=='UNKNOWN' and rid!=getattr(self,'_active_control_id',None) for rid,r in s.get('control_receipts',{}).items())
        if not s.get('stop') or self._unresolved() or uncertain_actions:
            _reject('Остановите новые входы и дождитесь сверки всех обязательств')
        if s.get('time_block') or (getattr(self.broker,'time_safety',None) and self.broker.time_safety.check()):
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
                if total!=number(c['realized']) or max(D(0),-total)!=debt:raise ValueError()
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
