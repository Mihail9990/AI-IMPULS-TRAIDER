"""Explicit flat-only recovery waiver. Never changes realized broker P/L."""
from copy import deepcopy
from decimal import Decimal as D
import uuid
from .control import ActionRejected
from .settings import number
from .basket_math import BasketSettings,net_money
from .broker import Spec

class RecoveryWriteoff:
    def writeoff_recovery(self,a):
        if set(a)!={'type','cycle_id','expected_debt','confirmed'} or a.get('confirmed') is not True:
            raise ActionRejected('Нужно явное подтверждение списания RECOVERY')
        s=self.state;c=s.get('cycle')
        try:expected=number(a['expected_debt']);debt=number(c['recovery'] if c else s.get('basket_carry','0'))
        except (ValueError,TypeError,KeyError,ArithmeticError):raise ActionRejected('Неверная сумма RECOVERY')
        if a['cycle_id']!=(c or {}).get('id') or expected!=debt or debt<=0:
            raise ActionRejected('Цикл или остаток изменился; обновите подтверждение')
        if s.get('migration_block') or getattr(self.broker,'read_only',False):
            raise ActionRejected('Списание недоступно в READ_ONLY или при миграции')
        uncertain_actions=any(r.get('status')=='UNKNOWN' and rid!=getattr(self,'_active_control_id',None) for rid,r in s.get('control_receipts',{}).items())
        if not s.get('stop') or self._unresolved() or uncertain_actions:
            raise ActionRejected('Остановите новые входы и дождитесь сверки всех обязательств')
        if s.get('time_block') or (getattr(self.broker,'time_safety',None) and self.broker.time_safety.check()):
            raise ActionRejected('Сначала подтвердите время; новый цикл заблокирован')
        if c and any(c.get(k) for k in ('edit','manual_close','initial_cancel')):
            raise ActionRejected('Предыдущая операция ещё не завершена')
        try:
            snap=self.snapshot()
            if snap['positions'] or snap['orders']:raise ValueError()
            spec=Spec.load(snap['spec']);BasketSettings.load(s['basket_settings']).validate(spec)
            self._validate_distances(s['tp_distances'],spec)
            if self.evidence_contract=='per_obligation_v1' and snap['quote'].get('utc_time') is None:raise ValueError()
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
        except ActionRejected:raise
        except (ValueError,TypeError,KeyError,ArithmeticError,OSError,TimeoutError):
            raise ActionRejected('Списание отклонено: нужны свежие факты, отсутствие позиций/ордеров и полностью учтённые закрытия; сначала ручная сверка')
        event=dict(id=uuid.uuid4().hex,cycle_id=(c or {}).get('id'),amount=str(debt),currency=s['currency'],at_utc=snap['now'],realized_before=(c or {}).get('realized'),accounting='WAIVE_RECOVERY_ONLY_NO_PNL_DEBIT')
        before=deepcopy(s)
        if c:
            s['completed'].append(dict(id=c['id'],contract=c['contract'],currency=s['currency'],realized=c['realized'],recovery='0',written_off_recovery=str(debt),completion='RECOVERY_WAIVED'))
        s.setdefault('recovery_writeoffs',[]).append(event)
        s.update(basket_carry='0',cycle=None,phase='WAIT_CANDLE',stop=False,stop_mode='',last_candle=max(snap['candle']['id'],int(number(snap['now'])//60)*60-60))
        try:self.save('recovery_waived_without_pnl_debit')
        except BaseException:
            self.state=self.store.load() or before
            self._durable_state=deepcopy(self.state)
            raise
