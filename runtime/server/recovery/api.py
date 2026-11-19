"""Authenticated local simulation API. Intentionally cannot connect to MT5."""
from decimal import Decimal
import hmac
import json
import sqlite3
import threading
from uuid import UUID
from typing import Literal
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .engine import Engine


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    request_id: UUID


class Range(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    id: Literal['1', '2']
    price: float = Field(gt=0)
    side: Literal['BUY', 'SELL']
    volume: float = Field(gt=0)


class SetRanges(Command):
    initial_sl_distance: Decimal = Field(default=Decimal('0.70'), ge=Decimal('0.60'))
    reentry_distance: float = Field(default=0.70, gt=0)
    ranges: list[Range] = Field(min_length=1, max_length=2)


class Store:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS recovery_state (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS recovery_requests (id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL)')
        row = self.db.execute('SELECT state FROM recovery_state WHERE id=1').fetchone()
        self.engine = Engine(json.loads(row[0]) if row else None)
        self.db.commit()

    def command(self, request_id, payload, operation):
        encoded = json.dumps(payload, sort_keys=True)
        with self.lock:
            previous = self.db.execute('SELECT payload,result FROM recovery_requests WHERE id=?', (str(request_id),)).fetchone()
            if previous:
                if previous[0] != encoded:
                    raise HTTPException(409, 'Request ID reused for another command')
                return json.loads(previous[1])
            candidate = Engine(self.engine.data)
            try:
                operation(candidate)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            result = candidate.state()
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO recovery_state VALUES(1,?)', (json.dumps(candidate.data),))
                self.db.execute('INSERT INTO recovery_requests VALUES(?,?,?)', (str(request_id), encoded, json.dumps(result)))
            self.engine = candidate
            return result


def create_app(token, journal='recovery-simulation.sqlite3'):
    if len(token) < 32:
        raise ValueError('WIDGET_TOKEN must contain at least 32 characters')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    store = Store(journal)
    app.state.recovery = store

    def auth(authorization):
        if not authorization or not hmac.compare_digest(authorization, 'Bearer ' + token):
            raise HTTPException(401, 'Unauthorized')

    @app.get('/api/recovery/state')
    def state(authorization: str | None = Header(default=None)):
        auth(authorization)
        with store.lock:
            return store.engine.state()

    @app.post('/api/recovery/ranges')
    def ranges(body: SetRanges, authorization: str | None = Header(default=None)):
        auth(authorization)
        payload = body.model_dump(mode='json')
        return store.command(body.request_id, ['ranges', payload],
                             lambda e: e.replace_ranges(payload['ranges'], payload['initial_sl_distance'], payload['reentry_distance']))

    @app.post('/api/recovery/cancel')
    def cancel(body: Command, authorization: str | None = Header(default=None)):
        auth(authorization)
        return store.command(body.request_id, ['cancel'], lambda e: e.cancel_ranges())

    @app.post('/api/recovery/ranges/{rid}/close-cancel')
    def close_cancel(rid: str, body: Command, authorization: str | None = Header(default=None)):
        auth(authorization)
        raise HTTPException(409, 'Simulation only: no execution adapter. Close outcome and realized P/L cannot be invented.')

    return app
