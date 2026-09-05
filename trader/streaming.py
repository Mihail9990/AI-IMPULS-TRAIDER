from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import logging
import threading
import time
from typing import Callable

import websocket


LOG = logging.getLogger(__name__)
D = Decimal
STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"


@dataclass(frozen=True)
class PriceWatch:
    kind: str
    direction: str
    level: Decimal

    def reached(self, bid: Decimal, ask: Decimal) -> bool:
        if self.kind == "TRIGGER":
            return ask >= self.level if self.direction == "BUY" else bid <= self.level
        if self.kind == "SL":
            return bid <= self.level if self.direction == "BUY" else ask >= self.level
        if self.kind == "TP":
            return bid >= self.level if self.direction == "BUY" else ask <= self.level
        return False


@dataclass(frozen=True)
class StreamingQuote:
    epic: str
    bid: Decimal
    ask: Decimal
    broker_timestamp: int
    received_at: float


class QuoteStream:
    """Reconnectable Capital.com quote subscription used only as a fast REST-check signal.

    A quote never mutates trading state.  It merely wakes the main loop when a watched level may
    have been reached; positions and activity history remain authoritative for every transition.
    """

    def __init__(
        self,
        epic: str,
        token_provider: Callable[[], tuple[str, str]],
        *,
        enabled: bool = True,
        stale_seconds: float = 5.0,
        reconnect_initial: float = 1.0,
        connection_factory: Callable[..., object] = websocket.create_connection,
    ):
        self.epic = epic
        self.token_provider = token_provider
        self.enabled = enabled
        self.stale_seconds = stale_seconds
        self.connection_factory = connection_factory
        self.reconnect_initial = reconnect_initial
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._quote: StreamingQuote | None = None
        self._watches: tuple[PriceWatch, ...] = ()
        self._connected = False

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="capital-quotes", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def watch(self, watches: list[PriceWatch]) -> None:
        with self._lock:
            self._watches = tuple(watches)

    def wait(self, timeout: float) -> bool:
        signalled = self._wake.wait(timeout)
        self._wake.clear()
        return signalled

    def latest(self) -> StreamingQuote | None:
        with self._lock:
            quote = self._quote
        if quote is None or time.monotonic() - quote.received_at > self.stale_seconds:
            return None
        return quote

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def _run(self) -> None:
        delay = self.reconnect_initial
        while not self._stop.is_set():
            connection = None
            try:
                cst, security_token = self.token_provider()
                if not cst or not security_token:
                    raise RuntimeError("REST session tokens are not available yet")
                connection = self.connection_factory(STREAM_URL, timeout=2)
                connection.send(json.dumps({
                    "destination": "marketData.subscribe",
                    "correlationId": str(time.time_ns()),
                    "cst": cst,
                    "securityToken": security_token,
                    "payload": {"epics": [self.epic]},
                }))
                connection.settimeout(1)
                with self._lock:
                    self._connected = True
                LOG.info("CAPITAL STREAM connected epic=%s", self.epic)
                delay = self.reconnect_initial
                last_ping = time.monotonic()
                while not self._stop.is_set():
                    if time.monotonic() - last_ping >= 300:
                        connection.send(json.dumps({
                            "destination": "ping",
                            "correlationId": str(time.time_ns()),
                            "cst": cst,
                            "securityToken": security_token,
                        }))
                        last_ping = time.monotonic()
                    try:
                        raw = connection.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        raise ConnectionError("Capital stream closed")
                    self._message(raw)
            except Exception as exc:
                if not self._stop.is_set():
                    LOG.warning("CAPITAL STREAM disconnected; retry in %.1fs: %s", delay, exc)
            finally:
                with self._lock:
                    self._connected = False
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30.0)

    def _message(self, raw: str) -> None:
        try:
            message = json.loads(raw)
            if message.get("status", "OK") != "OK":
                raise ConnectionError(
                    f"Capital stream rejected {message.get('destination', 'message')}"
                )
            if message.get("destination") != "quote":
                return
            payload = message.get("payload", {})
            if str(payload.get("epic", "")) != self.epic:
                return
            quote = StreamingQuote(
                epic=self.epic,
                bid=D(str(payload["bid"])),
                ask=D(str(payload["ofr"])),
                broker_timestamp=int(payload.get("timestamp", 0)),
                received_at=time.monotonic(),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            LOG.warning("CAPITAL STREAM ignored malformed quote")
            return
        with self._lock:
            if (
                self._quote is not None
                and quote.broker_timestamp
                and quote.broker_timestamp < self._quote.broker_timestamp
            ):
                LOG.info("CAPITAL STREAM ignored out-of-order quote epic=%s", self.epic)
                return
            self._quote = quote
            watches = self._watches
        if any(watch.reached(quote.bid, quote.ask) for watch in watches):
            self._wake.set()
