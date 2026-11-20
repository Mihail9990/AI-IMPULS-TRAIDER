"""HTTP adapter for the existing canonical EA command spool; no broker client."""
from __future__ import annotations

import hmac
import json
import re

from fastapi.concurrency import run_in_threadpool
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

if __package__:
    from .channel import Channel, ProtocolError, VERSION, digest, ident
else:
    from channel import Channel, ProtocolError, VERSION, digest, ident


_PUBLIC = {'manual_wait', 'manual_cancel'}
_BOUND = ('boot_id', 'context_id', 'generation_id')
_POST_FIELDS = frozenset(('request_id', 'expected_boot', 'expected_context',
                          'expected_generation', 'confirmed'))
_DURABLE_FIELDS = frozenset(('version', 'scope_id', *_BOUND, 'command_id',
                             'created_at', 'operation', 'payload', 'content_sha256'))
_MAX_BODY = 4096


def _uuid(value):
    try:
        return ident(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError('invalid id') from exc


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise ValueError('duplicate field')
        result[name] = value
    return result


async def _post(request: Request):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > _MAX_BODY:
            raise HTTPException(413, 'EA command body too large')
        data.extend(chunk)
    try:
        body = json.loads(data.decode('utf-8'), object_pairs_hook=_pairs)
        if not isinstance(body, dict) or body.keys() != _POST_FIELDS:
            raise ProtocolError('invalid keys')
        if body['confirmed'] is not True:
            raise ProtocolError('confirmation required')
        return tuple(_uuid(body[name]) for name in
                     ('request_id', 'expected_boot', 'expected_context', 'expected_generation'))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(422, 'Invalid EA command or missing confirmation') from exc


def _stored(channel: Channel, request_id: str, boot: str):
    """An immutable command is the durable correlation for a later receipt."""
    try:
        command = channel.command_record(request_id, boot_id=boot)
    except FileNotFoundError:
        return None
    if command.keys() != _DURABLE_FIELDS:
        raise ProtocolError('invalid durable command keys')
    if type(command['version']) is not int or command['version'] != VERSION or (
            command['scope_id'] != channel.scope_id or command['command_id'] != request_id
            or command['boot_id'] != boot):
        raise ProtocolError('invalid durable command identity')
    if (not isinstance(command['operation'], str) or command['operation'] not in _PUBLIC
            or command['payload'] != {} or type(command['created_at']) is not int):
        raise ProtocolError('invalid durable command content')
    for field in _BOUND:
        _uuid(command[field])
    sha = command['content_sha256']
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
        raise ProtocolError('invalid durable command hash')
    original = {k: v for k, v in command.items() if k != 'content_sha256'}
    if not hmac.compare_digest(sha, digest(original)):
        raise ProtocolError('invalid durable command hash')
    return command


def _result(channel, request_id, command=None):
    """The browser sees the verified status, never arbitrary receipt fields."""
    if command is None:
        return dict(request_id=request_id, dispatch='unknown', status='unknown',
                    reason='no_command_record', retry_allowed=False)
    try:
        receipt = channel.receipt(command)
        if 'version' in receipt and type(receipt['version']) is not int:
            raise ProtocolError('invalid receipt version')
        status = receipt['status']
        reason = 'no_receipt' if status == 'unknown' and receipt.get('reason') == 'no_receipt' else None
        return dict(request_id=request_id, dispatch='published', status=status,
                    reason=reason, retry_allowed=False)
    except (OSError, ValueError, TypeError):
        return dict(request_id=request_id, dispatch='published', status='unknown',
                    reason='receipt_unverified', retry_allowed=False)


def _projection(channel, request_id, boot):
    try:
        command = _stored(channel, request_id, boot)
    except (OSError, ValueError, TypeError):
        return dict(request_id=request_id, dispatch='unknown', status='unknown',
                    reason='command_unverified', retry_allowed=False)
    return _result(channel, request_id, command)


def create_handoff_app(token: str, channel: Channel) -> FastAPI:
    """Mount only in the authenticated local web worker. Do not mount as a fallback."""
    if not isinstance(token, str) or len(token) < 32 or not token.isascii() or any(
            ord(c) <= 32 or ord(c) >= 127 for c in token):
        raise ValueError('token must be at least 32 printable ASCII characters without whitespace')
    if not isinstance(channel, Channel):
        raise TypeError('EA Channel required')
    expected = 'Bearer ' + token
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def auth(request: Request, call_next):
        headers = request.headers.getlist('authorization')
        if len(headers) != 1 or not headers[0].isascii() or not hmac.compare_digest(headers[0], expected):
            return JSONResponse({'detail': 'Unauthorized'}, status_code=401,
                                headers={'Cache-Control': 'no-store'})
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/recovery/ea/state')
    def state():
        try:
            observed = channel.state()
            return {key: observed[key] for key in ('version', *_BOUND, 'mode')}
        except (OSError, ValueError, TypeError):
            raise HTTPException(503, 'EA state unavailable') from None

    @app.get('/api/recovery/ea/commands/{request_id}')
    def status(request_id: str, expected_boot: str):
        try:
            _uuid(request_id)
            _uuid(expected_boot)
        except ProtocolError:
            raise HTTPException(422, 'Invalid request ID') from None
        return _projection(channel, request_id, expected_boot)

    @app.post('/api/recovery/ea/{operation}', status_code=202)
    async def publish(operation: str, request: Request):
        if operation not in _PUBLIC:
            raise HTTPException(404, 'EA command not available')
        args = await _post(request)
        return await run_in_threadpool(publish_once, operation, *args)

    def publish_once(operation, request_id, boot, context, generation):
        expected_binding = (boot, context, generation)

        def same(command):
            return (command['operation'] == operation
                    and tuple(command[field] for field in _BOUND) == expected_binding)

        try:
            existing = _stored(channel, request_id, boot)
        except (OSError, ValueError, TypeError):
            # Do not dispatch if status of this ID cannot be read.
            return dict(request_id=request_id, dispatch='unknown', status='unknown',
                        reason='command_unverified', retry_allowed=False)

        if existing is not None:
            if not same(existing):
                raise HTTPException(409, 'Request ID belongs to another command')
            return _result(channel, request_id, existing)
        try:
            channel.publish(operation, {}, command_id=request_id,
                            expected_boot=boot, expected_context=context,
                            expected_generation=generation, confirmed=True)
        except (OSError, ValueError, TypeError):
            # The hard-link may already have succeeded, or another request may
            # have won the same ID. Read once, never publish twice here.
            try:
                existing = _stored(channel, request_id, boot)
            except (OSError, ValueError, TypeError):
                return dict(request_id=request_id, dispatch='unknown', status='unknown',
                            reason='command_unverified', retry_allowed=False)
            if existing is not None and not same(existing):
                raise HTTPException(409, 'Request ID belongs to another command') from None
            return _result(channel, request_id, existing)
        return _projection(channel, request_id, boot)

    return app
