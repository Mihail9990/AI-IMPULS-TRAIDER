"""Authenticated API for an explicitly owned native coordinator.

No terminal initialization and no automatic tick on GET. The process owner must
run the lifecycle separately and hold the NativeExecutorSession for app lifetime.
"""
import hmac
import json
import hashlib
import re
from uuid import UUID
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from .api import Command, SetRanges
from pydantic import Field
from .native_coordinator import NativeCoordinator


class NativeBodyLimit:
    """Bound native HTTP body buffering before FastAPI parses JSON.

    Content-Length is only an early rejection hint; the streamed byte count is
    authoritative. Oversize replies have no predispatch proof, so clients retain
    pending rather than guessing an outcome from a proxy-like HTTP status.
    """
    def __init__(self, app, maximum=65536):
        self.app, self.maximum = app, maximum

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('method') not in ('POST','PUT','PATCH'):
            return await self.app(scope, receive, send)
        lengths = [value for key,value in scope.get('headers', []) if key.lower() == b'content-length']
        try:
            if any(int(value) < 0 for value in lengths): raise ValueError()
            too_large = any(int(value) > self.maximum for value in lengths)
        except ValueError:
            return await JSONResponse({'detail':'Invalid Content-Length'}, status_code=400)(scope, receive, send)
        if too_large:
            return await JSONResponse({'detail':'Request body too large'}, status_code=413)(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect': return
            if message['type'] != 'http.request': continue
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > self.maximum:
                return await JSONResponse({'detail':'Request body too large'}, status_code=413)(scope, receive, send)
            if chunk: chunks.append(chunk)
            if not message.get('more_body', False): break
        body = b''.join(chunks)
        delivered = False
        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type':'http.request', 'body':body, 'more_body':False}
            return await receive()
        await self.app(scope, bounded_receive, send)


class NativeCommand(Command):
    expected_generation: str | None = Field(...)
    expected_context: str = Field(min_length=1)


class NativeSetRanges(SetRanges):
    expected_generation: str | None = Field(...)
    expected_context: str = Field(min_length=1)


def create_native_app(token, coordinator, *, lifespan=None):
    if not isinstance(token, str) or len(token) < 32 or not token.isascii():
        raise ValueError('Token must contain at least 32 ASCII characters')
    if not isinstance(coordinator, NativeCoordinator):
        raise TypeError('NativeCoordinator required')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(NativeBodyLimit)
    app.state.coordinator = coordinator

    async def predispatch_rejection(request, reason):
        # This proves only one HTTP attempt was rejected before endpoint dispatch.
        # It does NOT revoke a UUID or prove absence of another in-flight attempt.
        # Clients may clear only their persisted single-ever-attempt request.
        path = request.url.path
        if request.method != 'POST' or not (path in ('/api/recovery/ranges', '/api/recovery/cancel')
                or re.fullmatch(r'/api/recovery/ranges/[^/]+/close-cancel', path)):
            return None
        raw = await request.body()
        if len(raw) > 65536: return None
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get('request_id'), str): return None
            key = str(UUID(body['request_id']))
        except (ValueError, TypeError, KeyError):
            return None
        with coordinator.session._lock:
            try:
                c = coordinator.control; c._guard()
                existing = c.db.execute('SELECT id FROM native_api_commands WHERE id=?', (key,)).fetchone()
                if existing: return None
            except (ValueError, RuntimeError):
                return None
        return dict(schema_version=1, method='POST', path=path, request_id=key,
                    request_sha256=hashlib.sha256(raw).hexdigest(), phase='not_dispatched',
                    reason=reason, scope='attempt_only', attempt_only=True)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        result = {'detail': exc.detail}
        if exc.status_code == 401:
            marker = await predispatch_rejection(request, 'authentication_failed')
            if marker is not None: result['rejection'] = marker
        return JSONResponse(result, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # Do not echo validation inputs such as caller tokens or arbitrary values.
        result = {'detail': 'Invalid native command request'}
        marker = await predispatch_rejection(request, 'validation_failed')
        if marker is not None: result['rejection'] = marker
        return JSONResponse(result, status_code=422)

    def auth(value):
        if not value or not value.isascii() or not hmac.compare_digest(value, 'Bearer ' + token):
            raise HTTPException(401, 'Unauthorized')

    def command(key, payload):
        try:
            result = coordinator.command(str(key), payload)
            data = result.get('data') or {}
            waiting = data.get('range_management', {}).get('phase') == 'waiting'
            outcome = result.get('command_outcome') or ('confirmed' if result['executor_ready'] or waiting else 'reconciliation_required')
            return dict(result, widget=native_widget_projection(coordinator),
                        command=dict(request_id=str(key), phase=result.get('command_phase','completed'), outcome=outcome,
                                     range_outcomes=result.get('range_outcomes', [])))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post('/api/recovery/reconcile')
    def reconcile(body: NativeCommand, authorization: str | None = Header(default=None)):
        auth(authorization)
        try:
            result = coordinator.reconcile(expected_context=body.expected_context,
                                           expected_generation=body.expected_generation)
            return dict(result, widget=native_widget_projection(coordinator))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get('/api/recovery/commands/{request_id}')
    def command_status(request_id: UUID, authorization: str | None = Header(default=None)):
        auth(authorization)
        with coordinator.session._lock:
            c = coordinator.control; c._guard()
            row = c.db.execute('SELECT payload,phase,result FROM native_api_commands WHERE id=?',
                               (str(request_id),)).fetchone()
            receipt = dict(request_id=str(request_id), phase='not_recorded', outcome=None, payload=None)
            if row:
                receipt.update(phase=row[1], payload=json.loads(row[0]))
                if row[1] == 'completed':
                    result = json.loads(row[2]); data = result.get('data') or {}
                    waiting = data.get('range_management', {}).get('phase') == 'waiting'
                    receipt['range_outcomes'] = result.get('range_outcomes', [])
                    receipt['outcome'] = result.get('command_outcome') or ('confirmed' if result['executor_ready'] or waiting else 'reconciliation_required')
                elif row[1] == 'rejected':
                    rejected_result=json.loads(row[2])
                    receipt.update(outcome='rejected', error=rejected_result['error'],
                                   range_outcomes=rejected_result.get('range_outcomes', []))
            return {'context_id':coordinator.context_id(), 'command':receipt}

    @app.get('/api/recovery/state')
    def state(authorization: str | None = Header(default=None)):
        auth(authorization)
        return dict(coordinator.state(), widget=native_widget_projection(coordinator))

    @app.post('/api/recovery/ranges')
    def ranges(body: NativeSetRanges, authorization: str | None = Header(default=None)):
        auth(authorization)
        payload = body.model_dump(mode='json')
        key = payload.pop('request_id')
        return command(key, dict(payload, kind='ranges'))

    @app.post('/api/recovery/cancel')
    def cancel(body: NativeCommand, authorization: str | None = Header(default=None)):
        auth(authorization)
        return command(body.request_id, dict(kind='cancel', expected_generation=body.expected_generation, expected_context=body.expected_context))

    @app.post('/api/recovery/ranges/{rid}/close-cancel')
    def close_cancel(rid: str, body: NativeCommand, authorization: str | None = Header(default=None)):
        auth(authorization)
        return command(body.request_id, dict(kind='close_cancel', range_id=rid, expected_generation=body.expected_generation, expected_context=body.expected_context))

    return app


def native_widget_projection(coordinator):
    """Read-only broker inventory plus explicitly labelled durable configuration.

    No observer commits, strategy ticks or broker sends. A snapshot is a bounded
    non-atomic read, never a guarantee that subsequent commands remain valid.
    """
    from copy import deepcopy
    from datetime import datetime, timezone
    from decimal import Decimal
    c, s = coordinator.control, coordinator.session
    with s._lock:
        raw = coordinator.state()
        data = raw['data'] or {}
        binding = s.transport.binding
        config = data.get('strategy_config') or {}
        issues = []
        configured = deepcopy(config.get('ranges', []))
        for r in configured:
            r['lifecycle'] = data.get('range_modes', {}).get(r['id'], 'active')
            r['value_source'] = 'configured'
        history = []
        for generation in coordinator.ranges.history() + ([data] if data else []):
            for row in generation.get('close_ledger', {}).values():
                history.append(dict(deepcopy(row), generation=generation['generation'],
                                    value_source='durable_broker_ledger'))
        totals = deepcopy(raw['totals'])
        for rid in {r['id'] for r in configured} | set(totals):
            total = totals.setdefault(rid, dict(realized_net='0', closed_positions=0,
                                              classification_pending_count=0, full_sl_count=0))
            total['gross_negative_closed_net'] = str(sum(
                (max(Decimal('0'), -Decimal(row['realized_net']))
                 for row in data.get('close_ledger', {}).values()
                 if row['range_id'] == rid and row['fully_closed']), Decimal('0')))
        widget = dict(schema_version=1, mode='native', account_trade_mode='unknown',
            snapshot=dict(status='unavailable', observed_at=None, generation=data.get('generation'), symbol=binding.symbol, context_id=coordinator.context_id()),
            settings=deepcopy(config.get('settings', {})), ranges=configured,
            positions=None, orders=None, loss_history=history, totals=totals,
            executor_ready=raw['executor_ready'], reconciliation_required=False, issues=issues,
            capabilities=dict(set_ranges=False, cancel_ranges=False, close_cancel_range_ids=[], reasons=[]))
        # A completed cancel generation is intentionally unready, not an error.
        without_management = deepcopy(data); without_management.pop('range_management', None)
        checkpoint = bool(data and c._unresolved_entry_cancellations(without_management))
        waiting = data.get('range_management', {}).get('phase') == 'waiting'
        management_unresolved = bool(data.get('range_management') and not waiting)
        initial_unresolved = any(x['phase'] not in ('staged', 'verified', 'rejected') for x in data.get('orders', []))
        blocked = (raw['api_reconciliation_required'] or checkpoint or initial_unresolved or management_unresolved
                   or (not raw['executor_ready'] and not waiting))
        if blocked: issues.append('native_reconciliation_required')
        if data and not config:
            blocked = True; issues.append('strategy_configuration_unavailable')
        try:
            c._guard()
            account, _ = s.transport.validator.session._verify_session()
            scope = dict(account=binding.account, server=binding.server, symbol=binding.symbol, magic=c.magic)
            snapshot = s.transport.snapshot(scope)
            mappings = {}
            position_mappings = {}
            mapping_error = False
            observed_pending = {}
            observed_positions = {}
            mapped_position_ids = set()
            for item in coordinator._items(data) if data else []:
                if item['phase'] != 'verified': continue
                try:
                    journal = s.executor.read(item['key'])
                    if (c._receipt(journal) != item['broker_order'] or
                        journal['payload'] != {'request':item['request'], 'expected':scope}):
                        raise ValueError('Mapping differs from durable receipt')
                    ticket = item['broker_order']
                    if ticket in mappings: raise ValueError('Duplicate mapping')
                    mappings[ticket] = item['intent']['range_id']
                    evidence = coordinator.observer.reader.read(item, magic=c.magic)
                    if evidence['position_identifier'] is not None:
                        mapped_position_ids.add(evidence['position_identifier'])
                    pending = evidence['pending_order']
                    if pending is not None:
                        observed_pending[pending['ticket']] = pending
                    p = evidence['position']
                    if p is not None:
                        if p['ticket'] in position_mappings: raise ValueError('Duplicate position mapping')
                        position_mappings[p['ticket']] = (item['intent']['range_id'], evidence['position_identifier'])
                        observed_positions[p['ticket']] = p
                except (ValueError, RuntimeError):
                    mapping_error = True
            owned = lambda row: row['symbol'] == binding.symbol and row['magic'] in binding.magics
            # Reader history is later than the inventory read. If it witnesses
            # a fill/close/partial-volume change, the earlier snapshot cannot be
            # labelled fresh or used to offer flat-only controls.
            for inventory, observations, fields in (
                    (snapshot['orders'], observed_pending, ('ticket','volume','price','sl','tp')),
                    (snapshot['positions'], observed_positions, ('ticket','identifier','volume','entry','sl','tp'))):
                actual = {row['ticket']:row for row in inventory if owned(row)}
                mapped_tickets = (set(mappings) if observations is observed_pending else
                                  set(position_mappings) | {ticket for ticket,row in actual.items()
                                                           if row.get('identifier') in mapped_position_ids})
                for ticket in mapped_tickets | set(observations):
                    before, after_read = actual.get(ticket), observations.get(ticket)
                    if (before is None) != (after_read is None):
                        raise ValueError('Broker inventory changed during projection')
                    if before is not None:
                        for field in fields:
                            left, right = before.get(field), after_read.get(field)
                            if field in ('volume','price','entry','sl','tp'):
                                if left is None or right is None or Decimal(str(left)) != Decimal(str(right)):
                                    raise ValueError('Broker inventory changed during projection')
                            elif left != right:
                                raise ValueError('Broker inventory changed during projection')
            orders, positions = [], []
            unknown = mapping_error
            for source in snapshot['orders']:
                if not owned(source): continue
                row = deepcopy(source)
                row['side'] = {'ORDER_TYPE_BUY_STOP':'BUY', 'ORDER_TYPE_SELL_STOP':'SELL'}.get(row['type'], 'unknown')
                row['range_id'] = mappings.get(row['ticket']) if row['magic'] == c.magic else None
                row['mapping_status'] = 'verified' if row['range_id'] else 'unmapped'
                unknown |= row['range_id'] is None
                orders.append(row)
            for source in snapshot['positions']:
                if not owned(source): continue
                row = deepcopy(source); row['price'] = row['entry']
                match = position_mappings.get(row['ticket'])
                row['range_id'] = (match[0] if match and match[1] == row['identifier'] and row['magic'] == c.magic else None)
                row['mapping_status'] = 'verified' if row['range_id'] else 'unmapped'
                unknown |= row['range_id'] is None
                positions.append(row)
            after, _ = s.transport.validator.session._verify_session(); c._guard()
            before_mode, after_mode = getattr(account, 'trade_mode', None), getattr(after, 'trade_mode', None)
            if before_mode != after_mode: raise ValueError('Account trade mode changed during snapshot')
            for name, label in (('ACCOUNT_TRADE_MODE_REAL','real'), ('ACCOUNT_TRADE_MODE_DEMO','demo'), ('ACCOUNT_TRADE_MODE_CONTEST','contest')):
                if hasattr(s.transport.api, name) and before_mode == getattr(s.transport.api, name):
                    widget['account_trade_mode'] = label
            try:
                s.transport.validator._session()
            except ValueError:
                blocked = True; issues.append('algorithmic_trading_permission_unavailable')
            widget['positions'], widget['orders'] = positions, orders
            widget['snapshot'].update(status='fresh', observed_at=datetime.now(timezone.utc).isoformat())
            if unknown:
                blocked = True; issues.append('unmapped_or_unverified_owned_exposure')
            terminal_reset = (not positions and not unknown and bool(data)
                and not raw['api_reconciliation_required'] and bool(c.protection_only_rejections(data))
                and c.db.execute("SELECT id FROM recovery_outbox WHERE state NOT IN ('VERIFIED','REJECTED') LIMIT 1").fetchone() is None
                and 'algorithmic_trading_permission_unavailable' not in issues)
            if terminal_reset:
                # No uncertain broker effect remains. Only explicit flat reset
                # is offered; rejected orders themselves are never retried.
                blocked = False
                issues[:] = [x for x in issues if x != 'native_reconciliation_required']
                issues.append('terminal_rejection_flat_reset_available')
            widget['reconciliation_required'] = blocked
            cap = widget['capabilities']
            cap['set_ranges'] = not blocked and not positions and (raw['executor_ready'] or waiting or terminal_reset)
            cap['cancel_ranges'] = not blocked and not positions and bool(data) and raw['executor_ready'] and not waiting
            if not blocked and raw['executor_ready']:
                for r in configured:
                    matching = [p for p in positions if p['range_id'] == r['id']]
                    if len(matching) == 1 and r['lifecycle'] == 'active':
                        cap['close_cancel_range_ids'].append(r['id'])
            if positions: cap['reasons'].append('open_positions_disable_set_and_cancel')
        except (ValueError, RuntimeError, OSError) as exc:
            # No guessed empty inventory and no stale data presented as fresh.
            widget['positions'] = widget['orders'] = None
            widget['snapshot'].update(status='unavailable', observed_at=None)
            widget['executor_ready'] = False
            widget['reconciliation_required'] = True
            issues.append('broker_snapshot_unavailable: ' + str(exc))
        widget['capabilities']['reasons'].extend(issues)
        return widget
