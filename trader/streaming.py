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
        token_provider: Callable[[], tuple[str, str] | tuple[str, str, int]],
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
        self._session_generation = -1
        self._signal_reason = ""
        self._quote_count = 0
        self._last_summary = 0.0
        self._last_fallback_log = 0.0
        self._signalled_watches: set[PriceWatch] = set()

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
            updated = tuple(watches)
            if updated != self._watches:
                self._watches = updated
                self._signalled_watches.intersection_update(updated)
                LOG.info(
                    "CAPITAL STREAM watches updated count=%s levels=%s",
                    len(updated),
                    [f"{watch.kind}:{watch.direction}:{watch.level}" for watch in updated],
                )

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

    def consume_signal(self) -> str:
        with self._lock:
            reason = self._signal_reason
            self._signal_reason = ""
        return reason or "stream_crossing"

    def log_rest_check(self, reason: str) -> None:
        now = time.monotonic()
        routine = reason in {"periodic_fallback", "stream_unavailable"}
        if routine and now - self._last_fallback_log < 60:
            return
        if routine:
            self._last_fallback_log = now
        quote = self.latest() if self.connected else None
        LOG.info(
            "CAPITAL STREAM -> REST check reason=%s connected=%s quote_age=%s bid=%s ask=%s",
            reason, self.connected,
            f"{now - quote.received_at:.3f}s" if quote else "unavailable",
            quote.bid if quote else "-", quote.ask if quote else "-",
        )

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    def _run(self) -> None:
        delay = self.reconnect_initial
        while not self._stop.is_set():
            connection = None
            try:
                session = self.token_provider()
                cst, security_token = session[:2]
                generation = int(session[2]) if len(session) > 2 else 0
                if not cst or not security_token:
                    raise RuntimeError("REST session tokens are not available yet")
                connection = self.connection_factory(STREAM_URL, timeout=2)
                correlation_id = str(time.time_ns())
                connection.send(json.dumps({
                    "destination": "marketData.subscribe",
                    "correlationId": correlation_id,
                    "cst": cst,
                    "securityToken": security_token,
                    "payload": {"epics": [self.epic]},
                }))
                connection.settimeout(1)
                LOG.info(
                    "CAPITAL STREAM socket connected; subscription requested epic=%s generation=%s",
                    self.epic, generation,
                )
                self._await_subscription(connection, correlation_id)
                with self._lock:
                    self._connected = True
                    self._session_generation = generation
                    self._last_summary = 0.0
                LOG.info(
                    "CAPITAL STREAM subscription confirmed epic=%s status=PROCESSED generation=%s",
                    self.epic, generation,
                )
                delay = self.reconnect_initial
                last_ping = time.monotonic()
                while not self._stop.is_set():
                    current_session = self.token_provider()
                    current_generation = int(current_session[2]) if len(current_session) > 2 else 0
                    if current_generation != generation:
                        LOG.info(
                            "CAPITAL STREAM REST session changed old_generation=%s new_generation=%s; reconnecting",
                            generation, current_generation,
                        )
                        raise ConnectionError("REST session tokens refreshed")
                    if time.monotonic() - last_ping >= 300:
                        ping_id = str(time.time_ns())
                        connection.send(json.dumps({
                            "destination": "ping",
                            "correlationId": ping_id,
                            "cst": cst,
                            "securityToken": security_token,
                        }))
                        LOG.info("CAPITAL STREAM ping sent correlation=%s", ping_id)
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
                    self._quote = None
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30.0)

    def _await_subscription(self, connection: object, correlation_id: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                raw = connection.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ConnectionError("Capital stream closed before subscription confirmation")
            message = self._decode(raw)
            if message.get("destination") == "quote":
                self._quote_message(message)
                continue
            if (message.get("destination") != "marketData.subscribe"
                    or str(message.get("correlationId", "")) != correlation_id):
                LOG.info(
                    "CAPITAL STREAM ignored handshake message destination=%s correlation=%s",
                    message.get("destination"), message.get("correlationId"),
                )
                continue
            subscriptions = message.get("payload", {}).get("subscriptions", {})
            if subscriptions.get(self.epic) != "PROCESSED":
                raise ConnectionError(
                    f"Capital stream subscription not processed for epic={self.epic}: "
                    f"{subscriptions.get(self.epic, 'missing')}"
                )
            return
        raise TimeoutError(f"Capital stream subscription confirmation timed out epic={self.epic}")

    def _message(self, raw: str) -> None:
        try:
            message = self._decode(raw)
            if message.get("destination") == "ping":
                LOG.info("CAPITAL STREAM ping confirmed correlation=%s", message.get("correlationId"))
                return
            if message.get("destination") != "quote":
                return
            self._quote_message(message)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            LOG.warning("CAPITAL STREAM ignored malformed message")

    @staticmethod
    def _decode(raw: str) -> dict:
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("stream message must be an object")
        if message.get("status", "OK") != "OK":
            raise ConnectionError(
                f"Capital stream rejected {message.get('destination', 'message')} "
                f"status={message.get('status')}"
            )
        return message

    def _quote_message(self, message: dict) -> None:
        payload = message.get("payload", {})
        if str(payload.get("epic", "")) != self.epic:
            return
        try:
            timestamp = int(payload["timestamp"])
            if timestamp <= 0:
                raise ValueError("timestamp must be positive")
            quote = StreamingQuote(
                epic=self.epic,
                bid=D(str(payload["bid"])),
                ask=D(str(payload["ofr"])),
                broker_timestamp=timestamp,
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
            self._quote_count += 1
            count = self._quote_count
            self._signalled_watches = {
                watch for watch in self._signalled_watches
                if watch in watches and watch.reached(quote.bid, quote.ask)
            }
            reached = [
                watch for watch in watches
                if watch.reached(quote.bid, quote.ask) and watch not in self._signalled_watches
            ]
            self._signalled_watches.update(reached)
        now = time.monotonic()
        if count == 1 or now - self._last_summary >= 60:
            self._last_summary = now
            LOG.info(
                "CAPITAL STREAM quotes healthy count=%s bid=%s ask=%s broker_timestamp=%s watches=%s",
                count, quote.bid, quote.ask, quote.broker_timestamp, len(watches),
            )
        if reached:
            reason = ",".join(f"{watch.kind}:{watch.direction}:{watch.level}" for watch in reached)
            with self._lock:
                self._signal_reason = reason
            LOG.info(
                "CAPITAL STREAM level reached levels=%s bid=%s ask=%s broker_timestamp=%s; waking REST",
                reason, quote.bid, quote.ask, quote.broker_timestamp,
            )
            self._wake.set()
