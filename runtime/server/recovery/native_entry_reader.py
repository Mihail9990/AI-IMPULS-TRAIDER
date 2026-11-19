"""Read-only native entry evidence. No login, sends or guessed position bindings.

MT5 history_deals_get(ticket=...) filters DEAL_ORDER (not DEAL_TICKET):
https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py
Reads are non-atomic; contradictory snapshots require another observation.
"""
from decimal import Decimal
from .engine import number
from .native_executor_session import NativeExecutorSession


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValueError('Positive broker identifier required')
    return value


class NativeEntryReader:
    def __init__(self, session):
        if not isinstance(session, NativeExecutorSession):
            raise TypeError('NativeExecutorSession required')
        self.session = session

    def read(self, item, *, magic):
        return self._read(item, magic=magic, receipt_mode=False)

    def read_receipt(self, item, *, magic):
        """Read partial-entry evidence from an exact durable broker receipt.

        Does not promote the journal or authorize execution. Missing receipts
        cannot be recovered by comment/price matching or enumerating candidates.
        """
        return self._read(item, magic=magic, receipt_mode=True)

    def _read(self, item, *, magic, receipt_mode):
        with self.session._lock:
            t = self.session.transport
            t._guard(); t.validator._session()
            if type(magic) is not int or magic not in t.binding.magics:
                raise ValueError('Owned magic required')
            ticket = positive_id(item['broker_order'])
            row = self.session.executor.read(item['key'])
            evidence = row.get('evidence') or {}
            if receipt_mode:
                receipt = row.get('receipt') or {}
                receipt_ticket = positive_id(receipt.get('order'))
                if row['state'] not in ('UNKNOWN', 'ACKNOWLEDGED') or receipt_ticket != ticket:
                    raise ValueError('Exact unresolved broker receipt required')
            elif (row['state'] != 'VERIFIED' or evidence.get('order') != ticket or
                    evidence.get('kind') not in ('pending_verified', 'order_filled_before_verification',
                                                'partial_entry_verified')):
                raise ValueError('Verified entry-order mapping required')
            scope = {'account':t.binding.account, 'server':t.binding.server,
                     'symbol':t.binding.symbol, 'magic':magic}
            if any(row['payload']['expected'].get(k) != v for k,v in scope.items()):
                raise ValueError('Journal scope differs from entry reader')
            if row['payload']['request'] != item['request']:
                raise ValueError('Entry checkpoint differs from verified journal')
            api = t.api; intent = item['intent']; symbol = t.binding.symbol
            side = intent['side']
            if side not in ('BUY', 'SELL'): raise ValueError('Invalid side')
            expected_type = api.ORDER_TYPE_BUY_STOP if side == 'BUY' else api.ORDER_TYPE_SELL_STOP
            expected_deal_type = api.DEAL_TYPE_BUY if side == 'BUY' else api.DEAL_TYPE_SELL
            desired = number(intent['volume'])
            if desired <= 0: raise ValueError('Invalid expected volume')
            request = row['payload']['request']
            if (request.get('action') != 'TRADE_ACTION_PENDING' or
                    request.get('type') != ('ORDER_TYPE_BUY_STOP' if side == 'BUY' else 'ORDER_TYPE_SELL_STOP') or
                    request.get('symbol') != symbol or request.get('magic') != magic or
                    number(request['volume']) != desired):
                raise ValueError('Intent differs from original pending request')

            def get(rows):
                if rows is None: raise ValueError('Incomplete broker evidence')
                return list(rows)

            pending = get(api.orders_get(ticket=ticket))
            history = get(api.history_orders_get(ticket=ticket))
            entries = get(api.history_deals_get(ticket=ticket))
            if len(pending) > 1 or len(history) > 1 or not (pending or history):
                raise ValueError('Missing or ambiguous exact entry order')
            for order in pending + history:
                if (order.ticket != ticket or order.magic != magic or order.symbol != symbol or
                        order.type != expected_type or number(order.volume_initial) != desired):
                    raise ValueError('Entry order scope mismatch')
                if receipt_mode and any(number(getattr(order, field)) != number(request[key])
                                        for field,key in (('price_open','price'),('sl','sl'),('tp','tp'))):
                    raise ValueError('Unverified receipt order differs from original request')
            if pending and history: raise ValueError('Order snapshot in transition')
            terminal_states = {api.ORDER_STATE_FILLED, api.ORDER_STATE_CANCELED}
            for name in ('ORDER_STATE_EXPIRED', 'ORDER_STATE_REJECTED'):
                if hasattr(api, name): terminal_states.add(getattr(api, name))
            terminal = not pending and bool(history) and history[0].state in terminal_states
            if history and not terminal: raise ValueError('Nonterminal history without live order')
            deals = {}; pid = None; filled = Decimal(0); notional = Decimal(0)
            def normalize(d):
                positive_id(d.ticket); positive_id(d.order); positive_id(d.position_id)
                if type(d.time_msc) is not int or d.time_msc < 0: raise ValueError('Invalid deal timestamp')
                volume, price = number(d.volume), number(d.price)
                if volume <= 0 or price <= 0: raise ValueError('Invalid deal volume/price')
                return {'ticket':d.ticket, 'order':d.order, 'position_identifier':d.position_id,
                        'symbol':d.symbol, 'magic':d.magic, 'entry':d.entry, 'type':d.type,
                        'time_msc':d.time_msc, 'volume':str(volume), 'price':str(price),
                        'profit':str(number(d.profit)), 'commission':str(number(d.commission)),
                        'swap':str(number(d.swap)), 'fee':str(number(d.fee)), 'reason':d.reason}
            for d in entries:
                if (d.order != ticket or d.symbol != symbol or d.magic != magic or
                        d.entry != api.DEAL_ENTRY_IN or d.type != expected_deal_type):
                    raise ValueError('Entry deal scope/direction mismatch')
                record = normalize(d)
                if d.ticket in deals:
                    if deals[d.ticket] != record: raise ValueError('Conflicting duplicate deal')
                    continue
                deals[d.ticket] = record
                if pid is not None and pid != d.position_id: raise ValueError('Multiple entry positions')
                pid = d.position_id
                filled += number(d.volume); notional += number(d.volume) * number(d.price)
            if filled > desired: raise ValueError('Entry volume exceeds mapped order')
            if receipt_mode and not 0 < filled < desired:
                raise ValueError('Receipt recovery requires strictly partial entry evidence')
            order = (pending or history)[0]
            if pid is not None and getattr(order, 'position_id', None) != pid:
                raise ValueError('Order/entry position identifier mismatch')
            remainder = number(order.volume_current)
            if remainder < 0 or filled + remainder != desired:
                raise ValueError('Incomplete entry history or changing order volume')
            if terminal and order.state == api.ORDER_STATE_FILLED and filled != desired:
                raise ValueError('Filled order missing entry volume')
            exits = {}; position = None
            if pid is not None:
                position_history = get(api.history_deals_get(position=pid))
                seen_entries = set(); exit_volume = Decimal(0)
                for d in position_history:
                    if d.position_id != pid or d.symbol != symbol: raise ValueError('Position history scope mismatch')
                    record = normalize(d)
                    if d.entry == api.DEAL_ENTRY_IN:
                        if d.ticket not in deals or deals[d.ticket] != record:
                            raise ValueError('Unmapped or changing position entry')
                        seen_entries.add(d.ticket)
                    elif d.entry == api.DEAL_ENTRY_OUT:
                        if d.type == expected_deal_type: raise ValueError('Exit direction mismatch')
                        if d.type not in (api.DEAL_TYPE_BUY, api.DEAL_TYPE_SELL): raise ValueError('Unsupported exit')
                        if d.ticket in exits:
                            if exits[d.ticket] != record: raise ValueError('Conflicting exit deal')
                            continue
                        exits[d.ticket] = record; exit_volume += number(d.volume)
                    else: raise ValueError('Unsupported INOUT/OUT_BY history')
                if seen_entries != set(deals): raise ValueError('Incomplete position history')
                open_volume = filled - exit_volume
                if open_volume < 0: raise ValueError('Exit volume exceeds entries')
                positions = get(api.positions_get())
                matches = [p for p in positions if p.identifier == pid]
                if len(matches) > 1: raise ValueError('Ambiguous position identifier')
                if matches:
                    p = matches[0]
                    positive_id(p.ticket); positive_id(p.identifier)
                    if number(p.price_open) <= 0 or number(p.sl) < 0 or number(p.tp) < 0:
                        raise ValueError('Invalid position price or protection')
                    if (p.symbol != symbol or p.magic != magic or
                            p.type != (api.POSITION_TYPE_BUY if side == 'BUY' else api.POSITION_TYPE_SELL) or
                            number(p.volume) != open_volume or open_volume <= 0):
                        raise ValueError('Position snapshot differs from deal history')
                    position = t.validator.session._normalize_position(p)
                elif open_volume != 0:
                    raise ValueError('Open entry has no matching position')
            # Repeat pending read to catch disappearance/late fill during history read.
            again = get(api.orders_get(ticket=ticket))
            normal = t.validator.session._normalize_order
            if [normal(o) for o in again] != [normal(o) for o in pending]:
                raise ValueError('Pending order changed during observation')
            t._guard(); t.validator._session()
            return {'order_ticket':ticket, 'position_identifier':pid,
                    'deals':sorted(deals.values(), key=lambda d:(d['time_msc'],d['ticket'])),
                    'exit_deals':sorted(exits.values(), key=lambda d:(d['time_msc'],d['ticket'])),
                    'filled_volume':str(filled), 'average_price':str(notional/filled) if filled else None,
                    'pending_order':normal(pending[0]) if pending else None,
                    'history_order':normal(history[0]) if history else None,
                    'position':position, 'terminal_order':terminal}
