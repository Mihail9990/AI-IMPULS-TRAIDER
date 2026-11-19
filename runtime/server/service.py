"""MT5 protection-only bridge. No opening or closing order API."""
import hmac
import os
import sqlite3
import threading
import json
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass
from typing import Optional, Literal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ConfigDict


@dataclass(frozen=True)
class Settings:
    token: str
    account: int
    server: str
    terminal_path: str
    live: bool = False
    journal: str = 'requests.sqlite3'

    @classmethod
    def from_env(cls):
        token = os.environ.get('WIDGET_TOKEN', '')
        if len(token) < 32:
            raise ValueError('WIDGET_TOKEN must contain at least 32 characters')
        return cls(token, int(os.environ['MT5_ACCOUNT']), os.environ['MT5_SERVER'],
                   os.environ.get('MT5_TERMINAL_PATH', ''), os.getenv('WIDGET_LIVE', '0') == '1',
                   os.getenv('WIDGET_JOURNAL', 'requests.sqlite3'))


class Change(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    request_id: UUID
    expected_symbol: str = Field(min_length=1, max_length=100)
    expected_entry: float = Field(gt=0)
    expected_side: Literal['BUY', 'SELL']
    expected_volume: float = Field(gt=0)
    expected_identifier: int = Field(ge=0)
    tp_distance: Optional[float] = Field(default=None, gt=0)
    sl_distance: Optional[float] = Field(default=None, gt=0)


class Bridge:
    def __init__(self, mt5, settings):
        self.mt5, self.settings = mt5, settings
        self.lock = threading.RLock()
        self.db = sqlite3.connect(settings.journal, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL)')
        self.db.commit()

    def guard(self):
        a = self.mt5.account_info()
        t = self.mt5.terminal_info()
        if a is None or t is None or not t.connected:
            raise HTTPException(503, 'MT5 disconnected')
        if a.login != self.settings.account or a.server != self.settings.server:
            raise HTTPException(409, 'MT5 account/server differs from configured account')
        return a

    def position(self, ticket):
        result = self.mt5.positions_get(ticket=ticket)
        if result is None:
            raise HTTPException(503, 'Cannot read MT5 positions')
        if len(result) != 1:
            raise HTTPException(409, 'Position no longer exists')
        return result[0]

    def serialize(self, p):
        return dict(ticket=p.ticket, symbol=p.symbol, side='BUY' if p.type == 0 else 'SELL',
                    volume=p.volume, price_open=p.price_open, sl=p.sl, tp=p.tp, profit=p.profit,
                    identifier=getattr(p, 'identifier', 0))

    def positions(self):
        with self.lock:
            a = self.guard()
            rows = self.mt5.positions_get()
            if rows is None:
                raise HTTPException(503, 'Cannot read MT5 positions')
            return dict(account=a.login, server=a.server, mode='live' if self.settings.live else 'dry_run',
                        positions=[self.serialize(p) for p in rows])

    def save(self, key, payload, result):
        self.db.execute('INSERT OR REPLACE INTO requests VALUES (?,?,?)',
                        (key, payload, json.dumps(result)))
        self.db.commit()
        return result

    def change(self, ticket, c):
        if c.tp_distance is None and c.sl_distance is None:
            raise HTTPException(422, 'Specify TP or SL distance')
        key = str(c.request_id)
        payload = json.dumps(dict(ticket=ticket, **c.model_dump(mode='json')), sort_keys=True)
        with self.lock:
            self.guard()
            old = self.db.execute('SELECT payload,result FROM requests WHERE id=?', (key,)).fetchone()
            if old:
                if old[0] != payload:
                    raise HTTPException(409, 'request_id reused with different data')
                return json.loads(old[1])
            p = self.position(ticket)
            info = self.mt5.symbol_info(p.symbol)
            if info is None:
                raise HTTPException(503, 'Symbol unavailable')
            tick_size = info.trade_tick_size or info.point
            if tick_size <= 0:
                raise HTTPException(503, 'Invalid symbol tick size')
            if (p.symbol != c.expected_symbol or abs(p.price_open - c.expected_entry) > tick_size / 10
                    or ('BUY' if p.type == 0 else 'SELL') != c.expected_side
                    or abs(p.volume - c.expected_volume) > 1e-9
                    or getattr(p, 'identifier', 0) != c.expected_identifier):
                raise HTTPException(409, 'Position changed; refresh and review')
            if p.type not in (0, 1):
                raise HTTPException(422, 'Unsupported position type')
            def price(distance, direction, existing):
                if distance is None:
                    return existing
                raw = Decimal(str(p.price_open)) + Decimal(str(distance)) * direction
                step = Decimal(str(tick_size))
                return float((raw / step).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * step)
            sign = 1 if p.type == 0 else -1
            tp, sl = price(c.tp_distance, sign, p.tp), price(c.sl_distance, -sign, p.sl)
            if (c.tp_distance is not None and (tp <= 0 or (tp-p.price_open)*sign <= 0)) or (c.sl_distance is not None and (sl <= 0 or (p.price_open-sl)*sign <= 0)):
                raise HTTPException(422, 'Distance too small or price invalid after tick rounding')
            tick = self.mt5.symbol_info_tick(p.symbol)
            if tick is None:
                raise HTTPException(503, 'No market quote')
            market = tick.bid if p.type == 0 else tick.ask
            minimum = max(info.trade_stops_level, info.trade_freeze_level) * info.point
            # Validate changed levels; unchanged protection is preserved verbatim.
            if c.tp_distance is not None and (tp-market)*sign <= minimum:
                raise HTTPException(422, 'TP is behind market or too close to current price')
            if c.sl_distance is not None and (market-sl)*sign <= minimum:
                raise HTTPException(422, 'SL is behind market or too close to current price')
            if not self.settings.live:
                return dict(status='rejected', message='Read-only mode: enable WIDGET_LIVE on VPS after setup',
                            position=self.serialize(p), proposed=dict(tp=tp, sl=sl))
            self.guard()
            # External terminal/manual changes are not serialized by this process.
            # Re-read immediately before dispatch; MT5 has no atomic compare-and-swap.
            latest = self.position(ticket)
            if (latest.symbol != p.symbol or latest.type != p.type or
                    latest.price_open != p.price_open or latest.volume != p.volume or
                    getattr(latest, 'identifier', None) != getattr(p, 'identifier', None)):
                raise HTTPException(409, 'Position changed; refresh and review')
            if c.sl_distance is None:
                sl = latest.sl
            if c.tp_distance is None:
                tp = latest.tp
            # Persist before send. A crash/timeout cannot lead to blind repeated orders.
            unknown = dict(status='unknown', message='Outcome not confirmed. Refresh positions; do not repeat blindly.')
            self.save(key, payload, unknown)
            try:
                request = dict(action=self.mt5.TRADE_ACTION_SLTP,
                               position=ticket, symbol=p.symbol, sl=sl, tp=tp)
                if getattr(self.mt5, 'IS_EA_BRIDGE', False):
                    request.update(request_id=key, expected_entry=latest.price_open,
                                   expected_symbol=latest.symbol, expected_type=latest.type,
                                   expected_identifier=getattr(latest, 'identifier', 0),
                                   expected_volume=latest.volume,
                                   change_sl=c.sl_distance is not None,
                                   change_tp=c.tp_distance is not None)
                result = self.mt5.order_send(request)
            except Exception:
                return unknown
            if result is None:
                return unknown
            if result.retcode == 10012:  # broker timeout: execution may still have happened
                return unknown
            if result.retcode not in (10009, 10025):
                return self.save(key, payload, dict(status='rejected', message=f'MT5 rejected update: {result.retcode}'))
            try:
                self.guard()
                current = self.position(ticket)
            except HTTPException:
                return unknown
            verified = abs(current.sl-sl) < tick_size/10 and abs(current.tp-tp) < tick_size/10
            answer = dict(status='verified' if verified else 'unknown',
                          message='TP/SL confirmed by MT5' if verified else 'MT5 response received, levels not confirmed',
                          position=self.serialize(current))
            return self.save(key, payload, answer)


def create_app(mt5, settings):
    bridge = Bridge(mt5, settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.bridge = bridge
    def authorize(value):
        if not value or not hmac.compare_digest(value, 'Bearer ' + settings.token):
            raise HTTPException(401, 'Unauthorized')
    @app.get('/health')
    def health(authorization: str = Header(default='')):
        authorize(authorization)
        with bridge.lock:
            bridge.guard()
        return dict(status='ok', mode='live' if settings.live else 'dry_run')
    @app.get('/api/positions')
    def positions(authorization: str = Header(default='')):
        authorize(authorization)
        return bridge.positions()
    @app.post('/api/positions/{ticket}/protection')
    def change(ticket: int, body: Change, authorization: str = Header(default='')):
        authorize(authorization)
        if ticket <= 0:
            raise HTTPException(422, 'Invalid ticket')
        return bridge.change(ticket, body)
    return app
