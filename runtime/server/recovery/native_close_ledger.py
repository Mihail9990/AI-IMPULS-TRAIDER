"""Native close accounting from durable authoritative entry observations.

No broker sends and no inferred stop classification. Python history lacks DEAL_SL;
SL exits remain pending until exact local EA evidence matches confirmed strategy
protection. Terminal mailbox ACLs are a deployment prerequisite; binding parsing
is not authentication against another local writer. No caller-byte supply API.
"""
from copy import deepcopy
from decimal import Decimal
from uuid import uuid4
from pathlib import Path
from .engine import number
from .ea_stop_evidence import ExitBinding, StopEvidenceUnavailable
from .native_entry_reader import positive_id


class NativeCloseLedger:
    def __init__(self, control):
        self.control = control
        self.session = control.session

    def _mapping(self, data, ticket):
        items = (list(data['orders']) + list(data.get('reverse_commands', {}).values())
                 + list(data.get('reentry_commands', {}).values()))
        matches = [x for x in items if x.get('broker_order') == ticket]
        if len(matches) != 1 or matches[0]['phase'] != 'verified':
            raise ValueError('Unique verified native order mapping required')
        return matches[0]

    def _own_close_resolutions(self, data, pid, exits):
        """Recognize only this control's VERIFIED explicit market close.

        EXPERT alone is never provenance. Require its durable group checkpoint,
        exact scoped journal, receipt order and the complete matching exit set.
        """
        result = {}
        b = self.session.transport.binding
        expected = dict(account=b.account, server=b.server, symbol=b.symbol,
                        magic=self.control.magic, identifier=pid)
        expert = getattr(self.session.transport.api, 'DEAL_REASON_EXPERT', None)
        if expert is None:
            return result
        for group in data.get('range_close_commands', {}).values():
            if group.get('position_identifier') != pid:
                continue
            for action in group.get('actions', []):
                if action.get('phase') != 'verified' or action['request'].get('action') != 'TRADE_ACTION_DEAL':
                    continue
                journal = self.session.executor.read(action['key'])
                if (action['scope'] != expected or
                        journal['payload'] != {'request': action['request'], 'expected': expected}):
                    raise ValueError('Own close scope/checkpoint differs from journal')
                evidence = journal.get('evidence') or {}
                ticket = (journal.get('receipt') or {}).get('order')
                if (journal['state'] != 'VERIFIED' or evidence.get('kind') != 'close_verified'
                        or evidence.get('position_identifier') != pid or type(ticket) is not int or ticket <= 0):
                    raise ValueError('Own close lacks exact verified receipt')
                actual = {k:d for k,d in exits.items() if d['order'] == ticket}
                recorded = evidence.get('deals', [])
                if len({str(d['ticket']) for d in recorded}) != len(recorded):
                    raise ValueError('Duplicate own-close journal deal')
                if set(actual) != {str(d['ticket']) for d in recorded}:
                    raise ValueError('Own close exit set differs from journal')
                for deal in recorded:
                    observed = actual[str(deal['ticket'])]
                    if (deal.get('order') != ticket or deal.get('position_identifier') != pid
                            or deal.get('symbol') != b.symbol or deal.get('entry') != 'OUT'
                            or number(deal['volume']) != number(observed['volume'])):
                        raise ValueError('Own close journal deal differs from observed exit')
                volume = sum((number(d['volume']) for d in actual.values()), Decimal(0))
                if not actual or volume != number(action['request']['volume']):
                    raise ValueError('Own close volume differs from verified request')
                for key, deal in actual.items():
                    if deal['reason'] == expert:
                        result[key] = {'source':'verified_explicit_close', 'key':action['key'], 'order':ticket}
        return result

    def _classify(self, row):
        api = self.session.transport.api
        manual = {getattr(api,n) for n in ('DEAL_REASON_CLIENT','DEAL_REASON_MOBILE','DEAL_REASON_WEB')
                  if hasattr(api,n)}
        row['classification'] = None; row['stop_level'] = None
        exits = row['exits']
        if row['fully_closed'] and exits:
            if all(d['reason'] in manual or (k in row.get('own_close_resolutions', {})
                    and d['reason'] == getattr(api, 'DEAL_REASON_EXPERT', None))
                   for k,d in exits.items()):
                row['classification'] = 'manual'
            else:
                resolutions = row.get('sl_resolutions', {})
                if all(k in resolutions and d['reason'] == getattr(api,'DEAL_REASON_SL',None)
                       for k,d in exits.items()):
                    values = {(resolutions[k]['classification'], resolutions[k]['stop_level']) for k in exits}
                    if len(values) == 1:
                        row['classification'], row['stop_level'] = next(iter(values))
        row['status'] = ('closed_classified' if row['classification'] else 'classification_pending') if row['fully_closed'] else 'open_or_partial'

    def record(self, order_ticket):
        """Account already observed broker facts only; never fetch/send by assertion.

        Caller first runs NativeEntryObserver for fresh authoritative evidence.
        Saved observations and all ledger writes share the native owner database.
        """
        c = self.control
        with self.session._lock:
            c._guard(); c.strategy_config(); positive_id(order_ticket)
            data = c._read()
            item = self._mapping(data, order_ticket)
            observation = data.get('entry_observations', {}).get(str(order_ticket))
            if observation is None: raise ValueError('Authoritative entry observation required')
            evidence = observation['evidence']
            if evidence['order_ticket'] != order_ticket: raise ValueError('Observation mapping differs')
            if number(evidence['filled_volume']) <= 0:
                raise ValueError('No executed volume to account')
            pid = positive_id(evidence['position_identifier'])
            ledger = data.setdefault('close_ledger', {})
            old = ledger.get(str(order_ticket))
            if old and old['position_identifier'] != pid: raise ValueError('Position binding changed')
            entries = {str(d['ticket']): deepcopy(d) for d in evidence['deals']}
            exits = {str(d['ticket']): deepcopy(d) for d in evidence['exit_deals']}
            if len(entries) != len(evidence['deals']) or len(exits) != len(evidence['exit_deals']):
                raise ValueError('Duplicate saved deal ticket')
            if set(entries) & set(exits): raise ValueError('Entry/exit ticket collision')
            for field, current in (('entries', entries), ('exits', exits)):
                if old and any(current.get(k) != v for k,v in old[field].items()):
                    raise ValueError('Previously accounted deal disappeared or changed')
            filled = sum((number(d['volume']) for d in entries.values()), Decimal(0))
            closed = sum((number(d['volume']) for d in exits.values()), Decimal(0))
            if filled != number(evidence['filled_volume']) or not 0 <= closed <= filled:
                raise ValueError('Observed close volume mismatch')
            fully_closed = closed == filled and evidence['position'] is None and evidence['terminal_order']
            net = sum((sum((number(d[k]) for k in ('profit','commission','swap','fee')), Decimal(0))
                       for d in list(entries.values()) + list(exits.values())), Decimal(0))
            api = self.session.transport.api
            manual = {getattr(api,n) for n in ('DEAL_REASON_CLIENT','DEAL_REASON_MOBILE','DEAL_REASON_WEB')
                      if hasattr(api,n)}
            sl_reason = getattr(api, 'DEAL_REASON_SL', None)
            requests = deepcopy(old.get('stop_evidence_requests', {})) if old else {}
            binding = self.session.transport.binding
            for key,d in exits.items():
                if d['reason'] != sl_reason: continue
                expected = dict(account=binding.account, server=binding.server,
                    deal=d['ticket'], order=d['order'], position=pid, symbol=binding.symbol,
                    exit_side='SELL' if item['intent']['side']=='BUY' else 'BUY',
                    time_msc=d['time_msc'], volume=d['volume'])
                if key in requests:
                    if {k:v for k,v in requests[key].items() if k!='request_id'} != expected:
                        raise ValueError('Stop evidence request binding changed')
                else:
                    requests[key] = dict(expected, request_id=uuid4().hex)
            # Only direct user-channel closes are classified without DEAL_SL.
            # EXPERT needs the exact verified explicit-close provenance below;
            # TP, stop-out and otherwise unknown/mixed reasons are not "manual".
            classification = ('manual' if fully_closed and exits and
                              all(d['reason'] in manual for d in exits.values()) else None)
            status = ('closed_classified' if classification else 'classification_pending') if fully_closed else 'open_or_partial'
            row = {'order_ticket':order_ticket, 'range_id':item['intent']['range_id'],
                   'position_identifier':pid, 'entries':entries, 'exits':exits,
                   'filled_volume':str(filled), 'closed_volume':str(closed),
                   'fully_closed':fully_closed, 'realized_net':str(net),
                   'classification':classification, 'status':status,
                   'stop_evidence_requests':requests,
                   'sl_resolutions':deepcopy(old.get('sl_resolutions', {})) if old else {}}
            row['own_close_resolutions'] = self._own_close_resolutions(data, pid, exits)
            if old and any(row['own_close_resolutions'].get(k) != v
                           for k,v in old.get('own_close_resolutions', {}).items()):
                raise ValueError('Previously verified own-close evidence changed')
            self._classify(row)
            ledger[str(order_ticket)] = row
            c._save(data)
            return deepcopy(row)

    def evidence_request(self, order_ticket, deal_ticket):
        """Return exact durable challenge used by the native local EA reader.

        The challenge alone cannot advance counters or authenticate local writers.
        """
        with self.session._lock:
            self.control._guard(); positive_id(order_ticket); positive_id(deal_ticket)
            row = self.control._read().get('close_ledger', {}).get(str(order_ticket), {})
            request = row.get('stop_evidence_requests', {}).get(str(deal_ticket))
            if request is None: raise ValueError('No observed SL evidence request')
            return ExitBinding(**dict(request, volume=number(request['volume'])))

    def resolve_sl(self, order_ticket):
        """Read exact local EA evidence, never accept caller levels/bytes/paths.

        Terminal mailbox ACLs are a deployment trust boundary. Other local
        processes with write access can forge exports; this is not cryptographic
        authentication. No trading commands are sent by this method.
        """
        from .local_evidence_bridge import LocalEvidenceBridge
        from .native_entry_observer import NativeEntryObserver
        from .price_evidence import canonical_exported_price
        c = self.control
        with self.session._lock:
            c._guard(); c.strategy_config(); positive_id(order_ticket)
            item = self._mapping(c._read(), order_ticket)
            observer = NativeEntryObserver(c)
            observer.observe(item['intent']['range_id'], order_key=item['key'])
            row = self.record(order_ticket)
            if not row['stop_evidence_requests']:
                raise ValueError('No authoritative SL exits to resolve')
            t = self.session.transport
            def bridge_path():
                t._guard(); t.validator._session()
                info = t.api.terminal_info()
                raw = getattr(info, 'data_path', None)
                if not isinstance(raw, str) or not raw:
                    raise ValueError('Terminal data path unavailable')
                path = Path(raw) / 'MQL5' / 'Files' / 'RECOVERY'
                if not path.exists():
                    # EA deployment can be completed while live positions still
                    # need support protection. Missing mailbox is pending evidence,
                    # not permission to guess a close and not a sticky send fault.
                    raise StopEvidenceUnavailable('EA mailbox directory unavailable; awaiting deployment')
                return LocalEvidenceBridge(path)
            bridge = bridge_path()
            # A request may be published before its EA response exists. Binding
            # and nonce stay durable; repeat attempts never create a new nonce.
            levels = {}
            symbol, _ = t.validator._metadata({'action':'TRADE_ACTION_SLTP'})
            for deal in row['stop_evidence_requests']:
                binding = self.evidence_request(order_ticket, int(deal))
                bridge.publish(binding)
                levels[deal] = canonical_exported_price(bridge.collect(binding), symbol.tick_size)
            if bridge_path().directory != bridge.directory:
                raise ValueError('Terminal mailbox changed during evidence retrieval')
            observer.observe(item['intent']['range_id'], order_key=item['key'])
            current = self.record(order_ticket)
            if current['exits'] != row['exits'] or current['entries'] != row['entries']:
                raise ValueError('Close history changed during evidence retrieval')
            data = c._read(); item = self._mapping(data, order_ticket)
            candidates = [(number(item['request']['sl']), 'initial_sl')]
            pid = current['position_identifier']
            for command in data.get('protection_commands', {}).values():
                if command['position_identifier'] != pid or command['phase'] != 'verified': continue
                journal = self.session.executor.read(command['key'])
                if (journal['state'] != 'VERIFIED' or
                        journal['payload'] != {'request':command['request'], 'expected':command['scope']} or
                        (journal.get('evidence') or {}).get('kind') != 'protection_verified' or
                        journal['evidence'].get('position') != command['request']['position']):
                    raise ValueError('Protection evidence differs from durable checkpoint')
                candidates.append((number(command['request']['sl']),
                                   'initial_sl' if command['stage'] == 0 else 'support_sl'))
            confirmed = data.get('confirmed_protection', {}).get(str(pid))
            if confirmed and confirmed.get('source') == 'observed_existing_stop':
                candidates.append((number(confirmed['sl']),
                                   'initial_sl' if confirmed['stage'] == 0 else 'support_sl'))
            saved = data['close_ledger'][str(order_ticket)]
            for deal,level in levels.items():
                classes = {kind for known,kind in candidates if known == level}
                if len(classes) != 1:
                    raise ValueError('EA stop is unknown or ambiguous initial/support level')
                resolution = {'stop_level':str(level), 'classification':classes.pop(),
                              'request_id':saved['stop_evidence_requests'][deal]['request_id'],
                              'source':'terminal_local_ea_mailbox'}
                old = saved['sl_resolutions'].get(deal)
                if old is not None and old != resolution:
                    raise ValueError('Resolved stop evidence replacement forbidden')
                saved['sl_resolutions'][deal] = resolution
            self._classify(saved); c._save(data)
            return deepcopy(saved)

    def totals(self):
        with self.session._lock:
            self.control._guard()
            data = self.control._read()
            if data is None: return {}
            result = {}
            for row in data.get('close_ledger', {}).values():
                r = result.setdefault(row['range_id'], {'realized_net':Decimal(0),
                    'closed_positions':0, 'classification_pending_count':0, 'full_sl_count':0})
                r['realized_net'] += number(row['realized_net'])
                if row['fully_closed']:
                    r['closed_positions'] += 1
                    r['classification_pending_count'] += row['classification'] is None
                    r['full_sl_count'] += row['classification'] == 'initial_sl'
            for r in result.values(): r['realized_net'] = str(r['realized_net'])
            return result
