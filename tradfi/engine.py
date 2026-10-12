"""Single-owner Recovery state machine. No bot-side close/refill/partial-close command exists."""
from copy import deepcopy
from decimal import Decimal as D
import logging
import uuid

from .broker import ACCOUNT_CURRENCY, COMPLETION_TOLERANCE, Spec, target_price, stepped_stop
from .settings import Settings, parse_settings, number
from .storage import DurabilityError

LOG = logging.getLogger(__name__)
TERMINAL = {'DONE', 'REJECTED', 'CANCELLED', 'FILLED', 'OBSOLETE'}


class Controller:
    def __init__(self, store, broker):
        self.store, self.broker = store, broker
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

        # Legacy monetary state must never be silently relabelled or converted.
        self._validate_currency()

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
            self.store.save(self.state, event)
        except DurabilityError:
            self.state = self.store.load() or self.state
            raise
        LOG.info('%s phase=%s', event, self.state['phase'])

    def notify(self, message):
        self.state['notifications'].append({'id': uuid.uuid4().hex, 'text': message})

    def pause(self, reason):
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
        if not snap.get('history_complete'):
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
                f"position={c.get('position', {})}")

    def _unresolved(self):
        return any(c['status'] not in TERMINAL for c in self.state['commands'].values())

    def _intent(self, kind, **params):
        cid = uuid.uuid4().hex[:24]
        self.state['commands'][cid] = dict(id=cid, kind=kind, status='READY', **params)
        return cid

    def dispatch(self, kinds=None):
        for cmd in list(self.state['commands'].values()):
            if cmd['status'] != 'READY' or (kinds is not None and cmd['kind'] not in kinds):
                continue
            if (getattr(self.broker, 'backend_id', '') == 'mt5_direct_v1'
                    and cmd['kind'] in {'STOP', 'MARKET'}
                    and any(other['kind'] in {'STOP', 'MARKET'} and other['status'] == 'UNKNOWN'
                            and self.broker.result(other['id']).get('status') == 'UNKNOWN'
                            for other in self.state['commands'].values())):
                continue  # unresolved send cannot justify another entry; cancellation remains allowed
            cmd['status'] = 'UNKNOWN'
            self.save('may_send:' + cmd['id'])
            try:
                self.broker.submit(deepcopy(cmd))
            except (OSError, TimeoutError) as exc:
                LOG.warning('Unknown gateway outcome %s: %s', cmd['id'], exc)
            # Never call submit again for this intent, even if the file/reply was lost.

    def _reconcile_commands(self, snap):
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
        if self.state['phase'] == 'SAFE_PAUSE':
            return
        try:
            snap = self.snapshot()
        except (OSError, TimeoutError):
            return  # stale/unavailable gateway cannot justify any mutation
        except ValueError as exc:
            self.pause(str(exc))
            return
        self._reconcile_commands(snap)
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
                'settings': cfg.dump(), 'original_center': str(center),
                'center': str(center), 'branch': 'FIRST', 'first': True,
                'target': None, 'recovery': None, 'realized': '0', 'round': 0,
                'entries': [], 'position': None, 'opened_ids': [],
            }
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
            if self.state['stop_mode'] == 'CANCEL':
                self.state['stop_mode'] = 'FINISH'
            self._cancel_pending(snap, entries)
            if self.state['phase'] == 'SAFE_PAUSE':
                return
            for cid in entries:
                if self.state['commands'][cid]['status'] == 'READY':
                    self.state['commands'][cid]['status'] = 'CANCELLED'
            self.save('fill_oco_intents')
            # Cancellation must not wait for an OrderCalcProfit round trip or its timeout.
            self.dispatch({'CANCEL'})
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
        c['position'] = dict(id=opening[0]['position_id'], entry=str(entry), size=str(size),
                             side=side, stepped=False, reanchored=False)
        self.state['phase'] = 'POSITION'
        self.save('actual_fill_adopted')

    def _position(self, snap):
        c = self.state['cycle']; p = c['position']; cfg = Settings.load(c['settings'])
        live = next((x for x in snap['positions'] if x['id'] == p['id']), None)
        exits = [d for d in snap['deals'] if d['position_id'] == p['id'] and d['entry'] == 'OUT']
        modifications = [cmd for cmd in self.state['commands'].values()
                         if cmd['kind'] == 'MODIFY' and cmd['position_id'] == p['id']]
        for cmd in modifications:
            if exits and not live and cmd['status'] == 'READY':
                cmd['status'] = 'CANCELLED'
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
        c = self.state['cycle']; p = c['position']
        if any(d['id'] in self.state['seen_deals'] for d in deals):
            return
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
        if eligible and remaining <= COMPLETION_TOLERANCE:
            if snap['positions'] or snap['orders']:
                return
            self.state['completed'].append(dict(id=c['id'], currency=c['currency'], target=c['target'], realized=c['realized'],
                                                forgiven_residual=str(max(D(0), remaining))))
            self.state['cooldown_until'] = snap['now'] + int(D(c['settings']['CYCLE_COOLDOWN_SECONDS']))
            self.state['cycle'] = None
            self.state['phase'] = 'PAUSED' if self.state['stop'] else 'COOLDOWN'
            self.notify(f'Cycle complete: realized={c["realized"]} {c["currency"]}, tolerance remainder={max(D(0), remaining)} {c["currency"]} (not profit).')
            self.save('cycle_complete')
            return
        if c['first'] and reason == 'SL':
            c['branch'], c['center'] = 'BRANCH1_RECOVERY', c['original_center']
        else:
            c['branch'] = 'PARTIAL_TP' if eligible else 'AFTER_SL'
            c['center'] = str(close)
        c['first'] = False
        c['position'] = None
        self.state['phase'] = 'NEXT_PAIR'
        self.save('closure_accounted_and_next_branch')
