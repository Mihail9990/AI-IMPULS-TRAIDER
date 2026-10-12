"""Single-owner Recovery state machine. No bot-side close/refill/partial-close command exists."""
from copy import deepcopy
from decimal import Decimal as D
import logging
import time
import uuid

from .broker import ACCOUNT_CURRENCY, COMPLETION_TOLERANCE, Spec, target_price, stepped_stop
from .settings import Settings, parse_settings, number
from .storage import DurabilityError

LOG = logging.getLogger(__name__)
TERMINAL = {'DONE', 'REJECTED', 'CANCELLED', 'FILLED', 'OBSOLETE'}


class Controller:
    def __init__(self, store, broker, *, clock=time.time, monotonic=time.monotonic):
        self.clock, self.monotonic = clock, monotonic
        self._cycle_clock = None
        self.store, self.broker = store, broker
        self.evidence_contract = getattr(broker, 'evidence_contract', None)
        self._durable_state=store.load()
        self.state = store.load() or {
            'schema': 1, 'phase': 'IDLE', 'settings': Settings().dump(), 'cycle': None,
            'stop': False, 'stop_mode': '', 'commands': {}, 'seen_deals': [],
            'notifications': [], 'telegram_offset': 0, 'last_candle': 0,
            'completed': [], 'account': None, 'currency': ACCOUNT_CURRENCY,
            'backend': getattr(broker, 'backend_id', 'file_gateway_v1'),
        }

        backend = getattr(broker, 'backend_id', 'file_gateway_v1')
        if self.state.get('backend', 'file_gateway_v1') != backend:
            raise DurabilityError('Incompatible backend state; preserve and reconcile, never replay')

        if self.evidence_contract == 'per_obligation_v1':
            saved=store.load()
            if saved is not None and saved.get('ledger_contract')!='deal_ledger_v1':
                raise DurabilityError('Evidence controller requires its own ledger state; no automatic migration')
            if saved and saved.get('commands') and saved.get('scope_contract')!='cycle_scope_v1':
                raise DurabilityError('Cycle binding missing; preserve old state for explicit reconciliation')
            self.state['scope_contract']='cycle_scope_v1'
            self.state['ledger_contract']='deal_ledger_v1'
            broker.before_mutation=self._mutation_allowed
            store.enable_deal_ledger()
            store.verify_deal_ledger(self.state)

        # Legacy monetary state must never be silently relabelled or converted.
        self._validate_currency()
        self._deadline_guard()

    def _validate_currency(self):
        records = [self.state, *self.state['completed']]
        if self.state['cycle'] is not None:
            records.append(self.state['cycle'])
        if any(record.get('currency') != ACCOUNT_CURRENCY for record in records):
            raise DurabilityError(
                'Incompatible or missing saved monetary currency; requires UST. '
                'Preserve the database and reconcile; no automatic conversion/reset.')

    def save(self, event):
        try:
            if self.evidence_contract=='per_obligation_v1':
                self.store.save(self.state,event,expected=self._durable_state)
            else:
                self.store.save(self.state, event)
            self._durable_state=deepcopy(self.state)
        except DurabilityError:
            self.state = self.store.load() or self.state
            self._durable_state=deepcopy(self.state)
            raise
        LOG.info('%s phase=%s', event, self.state['phase'])

    def notify(self, message):
        self.state['notifications'].append({'id': uuid.uuid4().hex, 'text': message})

    def pause(self, reason):
        if (self.state.get('cycle') or {}).get('manual'):
            self.state['cycle']['reconciliation_error']=reason
            self.save('manual_reconciliation_error'); return
        self.state['phase'] = 'SAFE_PAUSE'
        self.notify('SAFE_PAUSE: ' + reason + '. Broker SL/TP remain; no automatic close.')
        self.save('safe_pause')

    def snapshot(self):
        self._validate_currency()
        snap = self.broker.snapshot()
        identity = snap['account']
        if self.state['account'] is not None and self.state['account'] != identity:
            raise ValueError('Gateway account identity changed')
        if not snap['hedging']:
            raise ValueError('MT5 hedging account required for explicit OCO race detection')
        Spec.load(snap['spec'])
        if self.evidence_contract == 'per_obligation_v1':
            if snap.get('reconciliation_contract') != self.evidence_contract or snap.get('history_complete') is not False:
                raise ValueError('Missing per-obligation evidence contract')
            if not snap.get('observations_usable'):
                raise OSError('Required evidence API is unavailable')
            self.store.verify_deal_ledger(self._durable_state or self.state)
            for cid,cmd in self.state['commands'].items():
                if self._archived(cmd) or (self.state.get('cycle') or {}).get('manual'):
                    continue
                proof=snap['obligations'].get(cid,{}).get('status')
                if cmd['status']=='OBSOLETE':
                    continue
                if proof=='MISSING_DURABLE_JOURNAL':
                    raise ValueError('Missing durable command journal; no further execution')
                allowed={'FILLED':{'FILLED_OPEN_EVIDENCE','CLOSED_EVIDENCE'},
                         'DONE':{'CANCEL_TERMINAL_READBACK','PROTECTION_READBACK'},
                         'CANCELLED':{'TERMINAL_UNFILLED_EVIDENCE','NO_SEND_EVIDENCE'},
                         'REJECTED':{'TERMINAL_UNFILLED_EVIDENCE','REJECTED_RESPONSE','NO_SEND_EVIDENCE'}}
                # An old acknowledgement cannot override missing current evidence.
                # Protection may disappear after a proven full close; closure supersedes it.
                closed_modify=(cmd['kind']=='MODIFY' and any(
                    r.get('position_id')==cmd['position_id'] and r.get('status')=='CLOSED_EVIDENCE'
                    for r in snap['obligations'].values()))
                superseded_modify=(cmd['kind']=='MODIFY' and cmd['status']=='DONE' and any(
                    other['kind']=='MODIFY' and other['position_id']==cmd['position_id']
                    and snap['obligations'].get(other_id,{}).get('status')=='PROTECTION_READBACK'
                    for other_id,other in self.state['commands'].items() if other_id!=cid))
                if cmd['status'] in allowed and proof not in allowed[cmd['status']] and not (closed_modify or superseded_modify):
                    if proof in {'UNKNOWN','AWAIT_FILL_EVIDENCE','AWAIT_POSITION_EVIDENCE','API_EVIDENCE_UNAVAILABLE','ORDER_NOT_FINAL'}:
                        raise OSError('Terminal obligation evidence is delayed')
                    raise ValueError('Terminal obligation evidence changed; reconcile before execution')
        elif not snap.get('history_complete'):
            raise ValueError('Gateway history is incomplete; no trading reconciliation')
        bid, ask = number(snap['quote']['bid']), number(snap['quote']['ask'])
        if bid <= 0 or ask < bid:
            raise ValueError('Invalid executable quote')
        unique = {}
        for deal in snap['deals']:
            if deal['id'] in unique and unique[deal['id']] != deal:
                raise ValueError('Conflicting broker deal identity')
            for key in ('volume', 'price', 'profit'):
                number(deal[key])
            unique[deal['id']] = deal
        snap['deals'] = list(unique.values())
        return snap

    def command(self, text, update_id=None):
        self._deadline_guard()
        if update_id is not None and update_id < self.state['telegram_offset']:
            return
        raw = text.strip()
        # Parse and validate everything before committing settings or offset.
        settings = None
        if raw.upper().startswith('/SETTINGS:'):
            settings = parse_settings(raw)
            Spec.load(self.snapshot()['spec']).validate(settings)
        elif raw.lower() not in {'/start', '/stop', '/settings', '/status'}:
            raise ValueError('Commands: /settings, complete /SETTINGS: block, /start, /stop, /status')
        if settings:
            self.state['settings'] = settings.dump()
            self.notify('Settings saved atomically; active cycle snapshot is unchanged.')
        elif raw.lower() == '/start':
            if (self.state.get('cycle') or {}).get('manual'):
                raise ValueError('Manual cycle remains unresolved; /start cannot reset it or Recovery')
            snap = self.snapshot()
            Spec.load(snap['spec']).validate(Settings.load(self.state['settings']))
            if self.state['phase'] == 'SAFE_PAUSE':
                raise ValueError('SAFE_PAUSE requires broker reconciliation; /start cannot clear it')
            self.state['account'] = snap['account']
            if self.state['phase'] == 'PAUSED_CYCLE':
                if snap['positions'] or snap['orders'] or self._unresolved():
                    raise ValueError('Previous commands/positions/orders remain unresolved')
                self.state['phase'] = 'NEXT_PAIR'
            elif not self.state['cycle']:
                if snap['positions'] or snap['orders'] or self._unresolved():
                    raise ValueError('Start requires reconciled broker-flat state')
                self.state['last_candle'] = snap['candle']['id']
                self.state['phase'] = ('COOLDOWN' if snap['now'] < self.state.get('cooldown_until', 0)
                                       else 'WAIT_CANDLE')
            self.state['stop'], self.state['stop_mode'] = False, ''
        elif raw.lower() == '/stop':
            self.state['stop'] = True
            # A not-yet-observed fill racing with this command upgrades CANCEL to FINISH on tick.
            self.state['stop_mode'] = ('FINISH' if self.state['phase'] == 'POSITION'
                                       or self.state['stop_mode'] == 'FINISH' else 'CANCEL')
            if self.state['stop_mode'] == 'CANCEL':
                for cmd in self.state['commands'].values():
                    if cmd['status'] == 'READY' and cmd['kind'] in {'STOP', 'MARKET'}:
                        cmd['status'] = 'CANCELLED'  # durable proof: never crossed the may-send boundary
                        cmd['never_sent'] = True
            if not self.state['cycle'] and self.state['phase'] != 'SAFE_PAUSE':
                self.state['phase'] = 'PAUSED'
        elif raw.lower() == '/settings':
            self.notify(Settings.load(self.state['settings']).template())
        else:
            self.notify(self.status())
        if update_id is not None:
            self.state['telegram_offset'] = update_id + 1
        self.save('telegram_command')

    def status(self):
        c = self.state['cycle'] or {}
        return (f"TradFi {self.state['phase']} stop={self.state['stop']} "
                f"cycle={c.get('id', '-')} branch={c.get('branch', '-')} "
                f"target={c.get('target', '-')} recovery={c.get('recovery', '-')} {self.state['currency']} "
                f"position={c.get('position', {})} manual={c.get('manual',False)} "
                f"manual_reason={c.get('manual_reason','-')} deadline_utc={c.get('deadline_utc','-')} "
                f"observation={c.get('manual_observation',c.get('last_broker_observation',{}))}")

    def _archived(self, command):
        return (command['status'] in TERMINAL and command.get('cycle_id') in
                {c['id'] for c in self.state['completed']})

    def _deadline_guard(self):
        c=self.state.get('cycle')
        if not c: return False
        if c.get('manual'):
            self.state['phase']='MANUAL'
            return True
        try:
            now=float(number(self.clock())); mono=float(number(self.monotonic()))
            if now<=0 or mono<0: raise ValueError('Invalid clock')
        except (ValueError,TypeError,OverflowError):
            return self._enter_manual(c,'INVALID_CYCLE_CLOCK',None)
        reason=None
        try:
            start=float(number(c['started_at_utc'])); deadline=float(number(c['deadline_utc']))
            if start<=0 or deadline!=start+86400 or c['max_duration_seconds']!=86400:
                reason='INVALID_CYCLE_DEADLINE'
            last=float(number(c.get('last_observed_utc',start)))
            if now<last-1: reason='UTC_CLOCK_MOVED_BACKWARDS'
            if self._cycle_clock is None or self._cycle_clock[0]!=c['id']:
                self._cycle_clock=(c['id'],now,mono)
            _,wall0,mono0=self._cycle_clock
            if mono<mono0 or now<wall0+(mono-mono0)-1:
                reason='UTC_CLOCK_MOVED_BACKWARDS'
            if max(now,wall0+mono-mono0)>=deadline: reason='CYCLE_24H_LIMIT'
        except (KeyError,ValueError,TypeError,OverflowError):
            reason='MISSING_OR_INVALID_CYCLE_DEADLINE'
        if reason:
            return self._enter_manual(c,reason,now)
        if now-last>=1: c['last_observed_utc']=max(last,int(now))
        return False

    def _enter_manual(self, c, reason, now):
        c.update(manual=True,manual_reason=reason,manual_since_utc=now)
        self.state['phase']='MANUAL'
        pending=sum(cmd['kind'] in {'STOP','MARKET'} and cmd['status'] not in TERMINAL
                    for cmd in self.state['commands'].values() if cmd.get('cycle_id')==c['id'])
        last=c.get('last_broker_observation',{'status':'UNAVAILABLE'})
        self.notify(f'MANUAL: {reason}; cycle={c["id"]}; pending/unknown entry intents={pending}; '
                    f'last broker observation={last}; '
                    'broker orders and SL/TP retained, no automatic cancel/close/modify/new entry. '
                    'Recovery preserved; read-only reconciliation continues. /start cannot resume.')
        self.save('cycle_manual_required')
        return True

    def _mutation_allowed(self):
        return not self._deadline_guard() and self.state['phase']!='SAFE_PAUSE'

    def _manual_reconcile(self, snap):
        c=self.state['cycle']
        c['manual_observation']=dict(observed_at_utc=snap['now'],
            orders=[dict(id=o['id'],status=o['status'],owned=o['owned']) for o in snap['orders']],
            positions=[dict(id=p['id'],size=p['size'],sl=p['sl'],tp=p['tp'],owned=p['owned']) for p in snap['positions']],
            obligations=deepcopy(snap.get('obligations',{})))
        self.save('manual_observation')
        if any(not p.get('owned',True) for p in snap['orders']+snap['positions']): return
        opening=[d for d in snap['deals'] if d['entry']=='IN' and
                 d.get('command_id') in c['entries'] and d.get('cycle_id')==c['id']]
        ids={d['position_id'] for d in opening}
        if len(ids)!=1: return
        if c.get('manual_closure'): return  # no new branch/adoption after a manual close
        if not c['position']:
            proof=[r for r in snap.get('obligations',{}).values() if r.get('position_id') in ids]
            if not any(r.get('status') in {'FILLED_OPEN_EVIDENCE','CLOSED_EVIDENCE'} for r in proof): return
            self._adopt(opening,snap)
        p=c['position']
        if not p: return
        exits=[d for d in snap['deals'] if d['entry']=='OUT' and d['position_id']==p['id'] and d.get('cycle_id')==c['id']]
        if (exits and not snap['orders'] and not snap['positions']
                and not any(cmd['status'] not in TERMINAL and cmd['kind'] in {'STOP','MARKET','CANCEL'}
                            for cmd in self.state['commands'].values())
                and any(r.get('status')=='CLOSED_EVIDENCE' and r.get('position_id')==p['id']
                        for r in snap.get('obligations',{}).values())):
            self._closed(exits,snap)

    def _unresolved(self):
        return any(c['status'] not in TERMINAL for c in self.state['commands'].values())

    def _intent(self, kind, **params):
        if self._deadline_guard():
            raise ValueError('Manual cycle cannot create automatic intents')
        if self.state.get('cycle'):
            params['cycle_id']=self.state['cycle']['id']
        cid = uuid.uuid4().hex[:24]
        self.state['commands'][cid] = dict(id=cid, kind=kind, status='READY', **params)
        return cid

    def dispatch(self, kinds=None):
        if not self._mutation_allowed(): return
        for cmd in list(self.state['commands'].values()):
            if cmd['status'] != 'READY' or (kinds is not None and cmd['kind'] not in kinds):
                continue
            if (getattr(self.broker, 'backend_id', '') == 'mt5_direct_v1'
                    and cmd['kind'] in {'STOP', 'MARKET'}
                    and any(other['kind'] in {'STOP', 'MARKET'} and other['status'] == 'UNKNOWN'
                            and self.broker.result(other['id']).get('status') == 'UNKNOWN'
                            for other in self.state['commands'].values())):
                continue  # unresolved send cannot justify another entry; cancellation remains allowed
            if not self._mutation_allowed(): return
            cmd['status'] = 'UNKNOWN'
            self.save('may_send:' + cmd['id'])
            try:
                self.broker.submit(deepcopy(cmd))
            except (OSError, TimeoutError) as exc:
                LOG.warning('Unknown gateway outcome %s: %s', cmd['id'], exc)
            # Never call submit again for this intent, even if the file/reply was lost.

    def _reconcile_evidence_commands(self, snap):
        mapping={'FILLED_OPEN_EVIDENCE':'FILLED','CLOSED_EVIDENCE':'FILLED',
                 'PENDING_OBSERVED':'PENDING','CANCEL_TERMINAL_READBACK':'DONE',
                 'PROTECTION_READBACK':'DONE'}
        for cid,cmd in self.state['commands'].items():
            if self._archived(cmd): continue
            if cmd['status'] in {'READY','OBSOLETE'}:
                continue
            evidence=snap['obligations'].get(cid,{})
            status=evidence.get('status')
            if status in mapping:
                cmd['status']=mapping[status]
            elif status=='TERMINAL_UNFILLED_EVIDENCE':
                order=next(o for o in snap['order_history'] if o['id']==evidence['order_id'])
                cmd['status']=order['status']
            elif status in {'NO_SEND_EVIDENCE','REJECTED_RESPONSE'}:
                result=self.broker.result(cid)
                if result.get('status')=='REJECTED':
                    cmd.update(status='REJECTED',result=result)
            if evidence.get('order_id'):
                cmd['order_id']=evidence['order_id']
        self.save('obligations_reconciled')

    def _reconcile_commands(self, snap):
        if self.evidence_contract == 'per_obligation_v1':
            return self._reconcile_evidence_commands(snap)
        for cid, cmd in self.state['commands'].items():
            if cmd['status'] == 'OBSOLETE':
                continue  # terminal position closure superseded this operation, not a success ack
            if cmd['status'] == 'READY':
                continue
            result = self.broker.result(cid)
            if result.get('status') in TERMINAL | {'ACCEPTED'}:
                cmd.update(status=result['status'], result=result)
            if cmd['kind'] in {'STOP', 'MARKET'}:
                evidence = [x for x in snap['orders'] + snap['order_history']
                            if x.get('command_id') == cid or
                            (cmd.get('result', {}).get('order_id') and
                             x['id'] == cmd['result']['order_id'])]
                for order in evidence:
                    cmd['order_id'] = order['id']
                    cmd['status'] = order['status']
                if any(d.get('command_id') == cid or
                       (cmd.get('order_id') and d['order_id'] == cmd['order_id'])
                       for d in snap['deals'] if d['entry'] == 'IN'):
                    cmd['status'] = 'FILLED'
            elif cmd['kind'] == 'CANCEL':
                if any(o['id'] == cmd['order_id'] and o['status'] in {'CANCELLED', 'FILLED', 'REJECTED'}
                       for o in snap['order_history']):
                    cmd['status'] = 'DONE'
            elif cmd['kind'] == 'MODIFY':
                if any(p['id'] == cmd['position_id'] and D(p['sl']) == D(cmd['sl'])
                       and D(p['tp']) == D(cmd['tp']) for p in snap['positions']):
                    cmd['status'] = 'DONE'
                if any(d['position_id'] == cmd['position_id'] and d['entry'] == 'OUT'
                       and D(d.get('sl', '0')) == D(cmd['sl'])
                       and D(d.get('tp', '0')) == D(cmd['tp']) for d in snap['deals']):
                    cmd['status'] = 'DONE'
        self.save('broker_reconciled')

    def tick(self):
        self._deadline_guard()
        if self.state['phase'] == 'SAFE_PAUSE':
            return
        try:
            snap = self.snapshot()
        except (OSError, TimeoutError):
            return  # stale/unavailable gateway cannot justify any mutation
        except ValueError as exc:
            self.pause(str(exc))
            return
        if self.state.get('cycle'):
            self.state['cycle']['last_broker_observation']=dict(
                utc=snap['now'],order_statuses=[o['status'] for o in snap['orders']],
                positions=len(snap['positions']))
        self._deadline_guard()
        self._reconcile_commands(snap)
        if (self.state.get('cycle') or {}).get('manual'):
            self._manual_reconcile(snap); return
        if any(not x.get('owned', True) for x in snap['orders'] + snap['positions']):
            self.pause('Foreign order/position on XAUUSD.s; ownership conflict')
            return
        phase = self.state['phase']
        c = self.state['cycle']
        if c:
            self._cycle_tick(snap)
        elif phase == 'COOLDOWN':
            if snap['now'] >= self.state['cooldown_until']:
                self.state['last_candle'] = snap['candle']['id']
                self.state['phase'] = 'PAUSED' if self.state['stop'] else 'WAIT_CANDLE'
                self.save('cooldown_finished_wait_new_close')
        elif phase == 'WAIT_CANDLE' and snap['candle']['id'] > self.state['last_candle']:
            if snap['orders'] or snap['positions'] or self._unresolved():
                self.pause('Cannot start fresh cycle without confirmed flat')
                return
            cfg = Settings.load(self.state['settings'])
            spec = Spec.load(snap['spec'])
            try:
                spec.validate(cfg)
                center = spec.price(snap['candle']['close'])
            except ValueError as exc:
                self.pause(str(exc))
                return
            self.state['cycle'] = {
                'id': uuid.uuid4().hex, 'currency': spec.currency,
                'started_at_utc': self.clock(), 'max_duration_seconds': 86400,
                'settings': cfg.dump(), 'original_center': str(center),
                'center': str(center), 'branch': 'FIRST', 'first': True,
                'target': None, 'recovery': None, 'realized': '0', 'round': 0,
                'entries': [], 'position': None, 'opened_ids': [],
            }
            self.state['cycle']['deadline_utc']=self.state['cycle']['started_at_utc']+86400
            self.state['cycle']['last_observed_utc']=self.state['cycle']['started_at_utc']
            self._cycle_clock=None
            self.state['last_candle'] = snap['candle']['id']
            self.state['phase'] = 'NEXT_PAIR'
            self.save('new_cycle_snapshot')
            self._cycle_tick(snap)
        if self.state['phase'] != 'SAFE_PAUSE':
            self.dispatch()

    def _pair(self, snap):
        c = self.state['cycle']; cfg = Settings.load(c['settings']); spec = Spec.load(snap['spec'])
        branch = c['branch']
        distance = cfg.BRANCH1_RANGE if branch in {'FIRST', 'BRANCH1_RECOVERY'} else (
            cfg.RANGE_AFTER_PARTIAL_TP if branch == 'PARTIAL_TP' else cfg.RANGE_AFTER_SL)
        sl_distance = cfg.BRANCH1_SL_DISTANCE if branch in {'FIRST', 'BRANCH1_RECOVERY'} else (
            cfg.SL_DISTANCE_AFTER_PARTIAL_TP if branch == 'PARTIAL_TP' else cfg.SL_DISTANCE_AFTER_SL)
        plans = []
        spec.validate(cfg)
        for side, sign in [('BUY', D(1)), ('SELL', D(-1))]:
            trigger = spec.price(D(c['center']) + sign * distance)
            quote = D(snap['quote']['ask' if side == 'BUY' else 'bid'])
            crossed = sign * (quote - trigger) >= 0
            entry = spec.price(quote) if crossed else trigger
            tp = entry + sign * cfg.BASE_TP_DISTANCE if c['first'] else target_price(
                self.broker, spec, side, cfg.INITIAL_SIZE, entry, D(c['recovery']))
            sl = entry - sign * sl_distance
            spec.protection(side, entry, sl, tp, snap['quote'] if crossed else None)
            if not crossed and sign * (trigger - quote) < spec.point * spec.stops_level:
                raise ValueError('STOP too close to current quote; requested level unchanged')
            plans.append(dict(kind='MARKET' if crossed else 'STOP', side=side,
                              trigger=str(trigger), price=str(entry), sl=str(sl), tp=str(tp),
                              size=str(cfg.INITIAL_SIZE), sl_distance=str(sl_distance)))
        if all(p['kind'] == 'MARKET' for p in plans):
            raise ValueError('Both STOP levels crossed by spread; ambiguous pair')
        c.update(round=c['round'] + 1, entries=[], position=None, opened_ids=[])
        for plan in plans:
            c['entries'].append(self._intent(**plan))
        c['sl_distance'] = str(sl_distance)
        self.state['phase'] = 'PAIR'
        self.save('pair_intents_atomic')

    def _cancel_pending(self, snap, entries):
        for order in snap['orders']:
            if order.get('command_id') not in entries and not any(
                    self.state['commands'][cid].get('order_id') == order['id'] for cid in entries):
                continue
            attempts = [cmd for cmd in self.state['commands'].values()
                        if cmd['kind'] == 'CANCEL' and cmd['order_id'] == order['id']]
            if any(cmd['status'] != 'REJECTED' for cmd in attempts):
                continue  # especially UNKNOWN: never resubmit without a terminal rejection
            if len(attempts) >= 3:
                self.pause('Opposite cancellation rejected three times; pending remains broker-protected')
                return
            if not attempts or all(cmd.get('result', {}).get('status') == 'REJECTED' for cmd in attempts):
                self._intent('CANCEL', order_id=order['id'])
                self.save('cancel_intent')

    def _cycle_tick(self, snap):
        if not self._mutation_allowed(): return
        c = self.state['cycle']; phase = self.state['phase']
        if phase == 'PAUSED_CYCLE':
            return
        if phase == 'NEXT_PAIR':
            if self.state['stop_mode'] == 'CANCEL':
                self.state['phase'] = 'PAUSED_CYCLE'; self.save('stop_between_branches'); return
            if snap['orders'] or snap['positions'] or self._unresolved():
                return
            try:
                self._pair(snap)
            except (ValueError, ArithmeticError) as exc:
                self.pause(str(exc))
            return
        entries = c['entries']
        if any(o.get('command_id') not in entries and not any(
                self.state['commands'][cid].get('order_id') == o['id'] for cid in entries)
                for o in snap['orders']):
            self.pause('Unidentified working order; ownership conflict')
            return
        opening = [d for d in snap['deals'] if d['entry'] == 'IN' and any(
            d.get('command_id') == cid or (self.state['commands'][cid].get('order_id')
            and d['order_id'] == self.state['commands'][cid]['order_id']) for cid in entries)]
        ids = set(c['opened_ids']) | {d['position_id'] for d in opening}
        c['opened_ids'] = sorted(ids)
        if {p['id'] for p in snap['positions']} - ids:
            self.pause('Unidentified position; no price/direction-based identity guessing')
            return
        if len(ids) > 1:
            self.pause('Both OCO triggers executed (including positions already closed)')
            return
        if ids:
            if self.evidence_contract == 'per_obligation_v1':
                filled=[snap['obligations'].get(cid,{}) for cid in entries
                        if any(d.get('command_id')==cid for d in opening)]
                if any(r.get('status') in {'PARTIAL_FILL','PARTIAL_CLOSE','EXTERNAL_EXIT','UNSUPPORTED_EXIT',
                                          'VOLUME_EVIDENCE_INCONSISTENT','MIXED_EXIT_REASONS','AMBIGUOUS_HISTORY_TIME'} for r in filled):
                    self.pause('Per-obligation evidence requires manual reconciliation'); return
                if any(r.get('status') not in {'FILLED_OPEN_EVIDENCE','CLOSED_EVIDENCE'} for r in filled):
                    return
            if self.state['stop_mode'] == 'CANCEL':
                self.state['stop_mode'] = 'FINISH'
            self._cancel_pending(snap, entries)
            if self.state['phase'] == 'SAFE_PAUSE':
                return
            for cid in entries:
                if self.state['commands'][cid]['status'] == 'READY':
                    self.state['commands'][cid]['status'] = 'CANCELLED'
                    self.state['commands'][cid]['never_sent'] = True
            self.save('fill_oco_intents')
            # Cancellation must not wait for an OrderCalcProfit round trip or its timeout.
            self.dispatch({'CANCEL'})
            if c.get('manual'):
                self._manual_reconcile(snap); return
            if not c['position']:
                self._adopt(opening, snap)
                if self.state['phase'] == 'SAFE_PAUSE':
                    return
            self._position(snap)
            return
        if not self.state['stop']:
            for index, cid in enumerate(entries):
                cmd = self.state['commands'][cid]
                if (cmd['kind'] == 'STOP' and cmd['status'] == 'REJECTED'
                        and cmd.get('result', {}).get('reason') == 'CROSSED'):
                    # Executor explicitly rejected before send. UNKNOWN never permits this path.
                    cfg = Settings.load(c['settings']); spec = Spec.load(snap['spec'])
                    side = cmd['side']; sign = D(1) if side == 'BUY' else D(-1)
                    try:
                        price = spec.price(snap['quote']['ask' if side == 'BUY' else 'bid'])
                        if sign * (price - D(cmd['trigger'])) < 0:
                            raise ValueError('Crossed STOP quote reverted; no implicit retry')
                        sl = price - sign * D(cmd['sl_distance'])
                        tp = price + sign * cfg.BASE_TP_DISTANCE if c['first'] else target_price(
                            self.broker, spec, side, cfg.INITIAL_SIZE, price, D(c['recovery']))
                        spec.protection(side, price, sl, tp, snap['quote'])
                    except ValueError as exc:
                        self.pause(str(exc)); return
                    entries[index] = self._intent('MARKET', side=side, size=cmd['size'],
                        trigger=cmd['trigger'], price=str(price), sl=str(sl), tp=str(tp),
                        sl_distance=cmd['sl_distance'])
                    self.save('crossed_stop_explicit_rejection_market_intent')
                    return
        if self.state['stop_mode'] == 'CANCEL' or any(
                self.state['commands'][cid]['status'] == 'REJECTED' for cid in entries):
            self._cancel_pending(snap, entries)
            if self.state['phase'] == 'SAFE_PAUSE':
                return
            if not snap['orders'] and not snap['positions'] and all(
                    self.state['commands'][cid]['status'] in {'CANCELLED', 'REJECTED'} for cid in entries):
                if self.state['stop_mode'] == 'CANCEL':
                    self.state['phase'] = 'PAUSED_CYCLE'
                    self.save('pending_pair_cancelled')
                else:
                    self.pause('Entry rejected; opposite cancellation confirmed')
            return
        # Missing pending orders never count as cancellation, even after repeated empty snapshots.

    def _adopt(self, opening, snap):
        c = self.state['cycle']; cfg = Settings.load(c['settings'])
        size = sum((D(d['volume']) for d in opening), D(0))
        if size != cfg.INITIAL_SIZE:
            self.pause('Partial/oversize opening requires reconciliation; no refill')
            return
        entry = sum((D(d['price']) * D(d['volume']) for d in opening), D(0)) / size
        side = opening[0]['side']; sign = D(1) if side == 'BUY' else D(-1)
        if c['first']:
            target = self.broker.profit(side, size, entry, entry + sign * cfg.BASE_TP_DISTANCE)
            if target <= 0:
                self.pause('Nonpositive OrderCalcProfit target'); return
            c['target'] = c['recovery'] = str(target)
        self._deadline_guard()
        c['position'] = dict(id=opening[0]['position_id'], entry=str(entry), size=str(size),
                             side=side, stepped=False, reanchored=False)
        self.state['phase'] = 'MANUAL' if c.get('manual') else 'POSITION'
        self.save('actual_fill_adopted')

    def _position(self, snap):
        if self._deadline_guard():
            self._manual_reconcile(snap); return
        c = self.state['cycle']; p = c['position']; cfg = Settings.load(c['settings'])
        live = next((x for x in snap['positions'] if x['id'] == p['id']), None)
        exits = [d for d in snap['deals'] if d['position_id'] == p['id'] and d['entry'] == 'OUT']
        modifications = [cmd for cmd in self.state['commands'].values()
                         if cmd['kind'] == 'MODIFY' and cmd['position_id'] == p['id']]
        for cmd in modifications:
            if exits and not live and cmd['status'] == 'READY':
                cmd['status'] = 'CANCELLED'
                cmd['never_sent'] = True
            if cmd['status'] == 'DONE':
                if cmd['purpose'] == 'ANCHOR':
                    p['reanchored'] = True
                else:
                    p['stepped'] = True
            # A deal's native DEAL_SL can resolve an unknown modification after position closed.
            if cmd['status'] not in {'READY', 'CANCELLED', 'REJECTED'} and cmd['purpose'] == 'STEP' and any(
                    D(d.get('sl', '0')) == D(cmd['sl']) for d in exits):
                p['stepped'] = True
        if exits:
            if self.evidence_contract == 'per_obligation_v1':
                proofs=[r for r in snap['obligations'].values() if r.get('position_id')==p['id']]
                if not any(r.get('status')=='CLOSED_EVIDENCE' for r in proofs):
                    return
            if live or sum((D(d['volume']) for d in exits), D(0)) != D(p['size']):
                self.pause('Partial/external close: no guessed full-close P&L'); return
            # Full native close evidence makes an outstanding modification ineffectual.
            # This is NOT confirmation that the requested protection was ever installed.
            for cmd in modifications:
                if cmd['status'] not in TERMINAL:
                    cmd['status'] = 'OBSOLETE'
                    cmd['closed_by_deals'] = [d['id'] for d in exits]
            native_sl = [D(d.get('sl', '0')) for d in exits]
            if all(sl > 0 for sl in native_sl):
                p['stepped'] = all(any(cmd['purpose'] == 'STEP'
                    and cmd['status'] not in {'READY', 'CANCELLED', 'REJECTED'}
                    and D(cmd['sl']) == sl for cmd in modifications) for sl in native_sl)
            if snap['orders'] or self._unresolved():
                return  # OCO must be positively resolved before another pair
            self._closed(exits, snap)
            return
        if not live:
            return  # history may lag; no absence-based inference
        if D(live['size']) != D(p['size']):
            self.pause('Live volume changed; no partial-close/refill strategy'); return
        spec = Spec.load(snap['spec']); side = p['side']; sign = D(1) if side == 'BUY' else D(-1)
        entry = D(p['entry']); size = D(p['size'])
        if any(cmd['status'] not in TERMINAL for cmd in modifications):
            return
        if any(cmd['status'] == 'REJECTED' for cmd in modifications):
            self.pause('Protection modification rejected; original SL/TP retained'); return
        try:
            if 'desired_tp' not in p:
                p['desired_tp'] = str(entry + sign * cfg.BASE_TP_DISTANCE if c['first'] else target_price(
                    self.broker, spec, side, size, entry, D(c['recovery'])))
            tp = D(p['desired_tp'])
            if not p['reanchored']:
                sl = entry - sign * D(c['sl_distance'])
                if D(live['sl']) == sl and D(live['tp']) == tp:
                    p['reanchored'] = True; self.save('anchor_readback')
                else:
                    spec.protection(side, entry, sl, tp, snap['quote'], modify=True)
                    self._intent('MODIFY', position_id=p['id'], sl=str(sl), tp=str(tp), purpose='ANCHOR')
                    self.save('anchor_intent')
                return
            if c['first'] or abs(tp - entry) < cfg.STEPPED_TP_ACTIVATION:
                return
            executable = D(snap['quote']['bid' if side == 'BUY' else 'ask'])
            if sign * (executable - tp) >= 0:
                return  # broker TP wins equality; no bot-close command
            sl = stepped_stop(side, entry, snap['quote'], cfg.STEPPED_TP_ACTIVATION, cfg.STEPPED_TP_STEP)
            if sl is None or sign * (sl - D(live['sl'])) <= 0:
                return
            spec.protection(side, entry, sl, tp, snap['quote'], modify=True)
            self._intent('MODIFY', position_id=p['id'], sl=str(sl), tp=str(tp), purpose='STEP')
            self.save('step_intent')
        except ValueError as exc:
            # Original broker protection stays in place. Wait for legal stop/freeze distances.
            LOG.info('Protection unchanged: %s', exc)

    def _closed(self, deals, snap):
        self._deadline_guard()
        c = self.state['cycle']; p = c['position']
        atomic=self.evidence_contract=='per_obligation_v1'
        expected=deepcopy(self._durable_state) if atomic else None
        overlap={d['id'] for d in deals} & set(self.state['seen_deals'])
        if overlap:
            if atomic:
                raise DurabilityError('Closure batch overlaps consumed deals; preserve and reconcile')
            return
        if snap['positions'] or snap['orders']:
            return
        if atomic and any(d.get('cycle_id')!=c['id'] for d in deals):
            raise DurabilityError('Cross-cycle closure is forbidden')
        reasons = {d['reason'] for d in deals}
        if len(reasons) != 1 or not reasons <= {'TP', 'SL'}:
            self.pause('External/ambiguous closing reason'); return
        reason = next(iter(reasons))
        profit = sum((D(d['profit']) for d in deals), D(0))  # excludes commission and swap
        def execution_order(deal):
            ticket = str(deal['id'])
            # MT5 tickets are numeric strings; lexicographic "9" > "10" is not chronology.
            return (deal['time'], (0, int(ticket)) if ticket.isdecimal() else (1, ticket))
        close = D(max(deals, key=execution_order)['price'])
        remaining = D(c['recovery']) - profit
        self.state['seen_deals'].extend(d['id'] for d in deals)
        c['realized'] = str(D(c['realized']) + profit)
        c['recovery'] = str(remaining)
        eligible = reason == 'TP' or p['stepped']
        if c.get('manual'):
            c['position']=None
            c['manual_closure']=dict(reason=reason, accounted_deals=[d['id'] for d in deals],
                                    target_satisfied=bool(eligible and remaining<=COMPLETION_TOLERANCE))
            self.state['phase']='MANUAL'
            self._save_closure('manual_closure_accounted',expected,deals,snap) if atomic else self.save('manual_closure_accounted')
            return
        if eligible and remaining <= COMPLETION_TOLERANCE:
            if snap['positions'] or snap['orders']:
                return
            self.state['completed'].append(dict(id=c['id'], currency=c['currency'], target=c['target'], realized=c['realized'],
                                                forgiven_residual=str(max(D(0), remaining))))
            self.state['cooldown_until'] = snap['now'] + int(D(c['settings']['CYCLE_COOLDOWN_SECONDS']))
            self.state['cycle'] = None
            self.state['phase'] = 'PAUSED' if self.state['stop'] else 'COOLDOWN'
            self.notify(f'Cycle complete: realized={c["realized"]} {c["currency"]}, tolerance remainder={max(D(0), remaining)} {c["currency"]} (not profit).')
            self._save_closure('cycle_complete', expected, deals, snap) if atomic else self.save('cycle_complete')
            return
        if c['first'] and reason == 'SL':
            c['branch'], c['center'] = 'BRANCH1_RECOVERY', c['original_center']
        else:
            c['branch'] = 'PARTIAL_TP' if eligible else 'AFTER_SL'
            c['center'] = str(close)
        c['first'] = False
        c['position'] = None
        self.state['phase'] = 'NEXT_PAIR'
        self._save_closure('closure_accounted_and_next_branch', expected, deals, snap) if atomic else self.save('closure_accounted_and_next_branch')

    def _save_closure(self, event, expected, deals, snap):
        try:
            records=[dict(backend=self.state['backend'],account=self.state['account'],currency=self.state['currency'],
                          ticket=d['id'],position_id=d['position_id'],cycle_id=expected['cycle']['id'],
                          raw=snap['ledger_facts'][d['id']]) for d in deals]
            self.store.commit_deals(expected,self.state,records,event)
            self._durable_state=deepcopy(self.state)
        except BaseException:
            self.state=self.store.load() or expected
            self._durable_state=deepcopy(self.state)
            raise
