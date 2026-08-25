from __future__ import annotations

from decimal import Decimal
from datetime import datetime
import itertools
import json
import logging
import time
import requests

from .config import Settings


LOG = logging.getLogger(__name__)
SENSITIVE_KEYS = {
    "password", "identifier", "apiKey", "x-cap-api-key", "cst",
    "x-security-token", "token", "telegram_bot_token", "accountId", "clientId",
}


class CapitalError(RuntimeError):
    pass


class CapitalClient:
    """Small wrapper around Capital.com's REST API (v1 endpoints)."""

    def __init__(self, settings: Settings):
        self.settings = settings
        host = "https://demo-api-capital.backend-capital.com" if settings.demo else "https://api-capital.backend-capital.com"
        self.base = host + "/api/v1"
        self.http = requests.Session()
        self.http.headers.update({"X-CAP-API-KEY": settings.api_key, "Content-Type": "application/json"})
        self.last_login = 0.0
        self._request_ids = itertools.count(1)

    def login(self) -> None:
        LOG.info("CAPITAL LOGIN request url=%s/session credentials=<redacted>", self.base)
        started = time.monotonic()
        response = self.http.post(self.base + "/session", json={"identifier": self.settings.identifier, "password": self.settings.password, "encryptedPassword": False}, timeout=20)
        self._log_response("LOGIN", response, started)
        self._check(response)
        self.http.headers.update({"CST": response.headers["CST"], "X-SECURITY-TOKEN": response.headers["X-SECURITY-TOKEN"]})
        self.last_login = time.time()

    def request(self, method: str, path: str, **kwargs) -> dict:
        if time.time() - self.last_login > 540:
            self.login()
        request_id = next(self._request_ids)
        safe_kwargs = {key: _redact(value) for key, value in kwargs.items()}
        LOG.info("CAPITAL REQUEST id=%s method=%s path=%s data=%s", request_id, method, path,
                 _json_text(safe_kwargs))
        started = time.monotonic()
        # GET requests are idempotent and safe to retry. Mutations are sent once: a timeout can
        # happen after the broker accepted them, so retrying POST/PUT/DELETE could duplicate risk.
        attempts = 3 if method.upper() == "GET" else 1
        response = None
        for attempt in range(attempts):
            try:
                response = self.http.request(method, self.base + path, timeout=20, **kwargs)
            except requests.RequestException as exc:
                LOG.warning(
                    "CAPITAL transport failure id=%s attempt=%s/%s method=%s path=%s: %s",
                    request_id, attempt + 1, attempts, method, path, exc,
                )
                if attempt + 1 == attempts:
                    raise CapitalError(
                        f"Capital transport error {method} {path}: {exc}"
                    ) from exc
                time.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt + 1 == attempts:
                break
            LOG.warning(
                "CAPITAL transient response id=%s status=%s attempt=%s/%s",
                request_id, response.status_code, attempt + 1, attempts,
            )
            time.sleep(0.5 * (attempt + 1))
        assert response is not None
        if response.status_code == 401:
            LOG.warning("CAPITAL REQUEST id=%s received 401; refreshing session", request_id)
            self.login()
            response = self.http.request(method, self.base + path, timeout=20, **kwargs)
        self._log_response(request_id, response, started)
        self._check(response)
        return response.json() if response.content else {}

    @staticmethod
    def _log_response(request_id, response: requests.Response, started: float) -> None:
        try:
            body = _json_text(_redact(response.json())) if response.content else "<empty>"
        except (ValueError, TypeError):
            body = response.text
        LOG.info(
            "CAPITAL RESPONSE id=%s status=%s elapsed=%.3fs body=%s",
            request_id, response.status_code, time.monotonic() - started, body,
        )

    @staticmethod
    def _check(response: requests.Response) -> None:
        if not response.ok:
            raise CapitalError(f"Capital API {response.status_code}: {response.text[:500]}")

    def quote(self, epic: str) -> tuple[Decimal, Decimal]:
        market = self.request("GET", f"/markets/{epic}")["snapshot"]
        return Decimal(str(market["bid"])), Decimal(str(market["offer"]))

    def candle_range(self, epic: str, minutes: int) -> Decimal:
        resolution = "MINUTE" if minutes == 1 else f"MINUTE_{minutes}"
        prices = self.request("GET", f"/prices/{epic}", params={"resolution": resolution, "max": 2})["prices"]
        candle = prices[-2]
        high = Decimal(str(candle["highPrice"]["bid"]))
        low = Decimal(str(candle["lowPrice"]["bid"]))
        return high - low

    def open_position(self, epic: str, direction: str, size: Decimal,
                      stop: Decimal | None = None, target: Decimal | None = None,
                      *, stop_distance: Decimal | None = None,
                      profit_distance: Decimal | None = None) -> str:
        body = {
            "epic": epic, "direction": direction, "size": float(size),
            "guaranteedStop": False,
        }
        if stop_distance is not None:
            body["stopDistance"] = float(stop_distance)
        elif stop is not None:
            body["stopLevel"] = float(stop)
        if profit_distance is not None:
            body["profitDistance"] = float(profit_distance)
        elif target is not None:
            body["profitLevel"] = float(target)
        return self.request("POST", "/positions", json=body)["dealReference"]

    def confirmation(self, reference: str) -> dict:
        return self.request("GET", f"/confirms/{reference}")

    def wait_confirmation(self, reference: str, attempts: int = 10) -> dict:
        last = {}
        for _ in range(attempts):
            try:
                last = self.confirmation(reference)
                if last.get("dealStatus") in {"ACCEPTED", "REJECTED"}:
                    return last
            except CapitalError as exc:
                if "404" not in str(exc):
                    raise
            time.sleep(0.5)
        raise CapitalError(f"No final confirmation for {reference}: {last}")

    def positions(self) -> list[dict]:
        return self.request("GET", "/positions").get("positions", [])

    def position(self, deal_id: str) -> dict:
        return self.request("GET", f"/positions/{deal_id}")

    def wait_position(self, deal_id: str, deal_reference: str, direction: str,
                      attempts: int = 20, excluded_ids: set[str] | None = None,
                      epic: str = "") -> dict:
        """Resolve the permanent position id after an accepted create confirmation."""
        last_positions = []
        for _ in range(attempts):
            last_positions = self.positions()
            normalized = []
            for item in last_positions:
                position = item.get("position", item)
                item_epic = str(item.get("market", {}).get("epic") or position.get("epic", ""))
                if epic and item_epic != epic:
                    continue
                normalized.append(position)
            exact = next((item for item in normalized if str(item.get("dealId", "")) == deal_id), None)
            if exact:
                return exact
            by_reference = next((item for item in normalized
                                 if str(item.get("dealReference", "")) == deal_reference), None)
            if by_reference:
                return by_reference
            excluded = excluded_ids or set()
            same_direction = [
                item for item in normalized
                if item.get("direction") == direction
                and str(item.get("dealId", "")) not in excluded
            ]
            if len(same_direction) == 1:
                LOG.warning(
                    "Resolved %s position by new-direction fallback: dealId=%s reference=%s",
                    direction, same_direction[0].get("dealId"), deal_reference,
                )
                return same_direction[0]
            time.sleep(0.5)
        raise CapitalError(
            f"Accepted {direction} position did not appear in /positions; confirmation dealId={deal_id}; "
            f"last positions={last_positions}"
        )

    def working_orders(self) -> list[dict]:
        return self.request("GET", "/workingorders").get("workingOrders", [])

    def update_position(self, deal_id: str, stop: Decimal | None, target: Decimal | None) -> str:
        body = {
            "stopLevel": float(stop) if stop is not None else None,
            "profitLevel": float(target) if target is not None else None,
        }
        last_error = None
        for attempt in range(5):
            try:
                return self.request("PUT", f"/positions/{deal_id}", json=body)["dealReference"]
            except CapitalError as exc:
                last_error = exc
                if "error.not-found.dealId" not in str(exc) or attempt == 4:
                    raise
                time.sleep(0.5)
        raise last_error  # pragma: no cover

    def close_position(self, deal_id: str) -> str:
        """Close an open position at market and return its confirmation reference."""
        return self.request("DELETE", f"/positions/{deal_id}")["dealReference"]

    def working_stop(self, epic: str, direction: str, size: Decimal, level: Decimal,
                     stop: Decimal | None = None, target: Decimal | None = None) -> str:
        body = {"epic": epic, "direction": direction, "size": float(size), "level": float(level),
                "type": "STOP", "guaranteedStop": False}
        if stop is not None:
            body["stopLevel"] = float(stop)
        if target is not None:
            body["profitLevel"] = float(target)
        return self.request("POST", "/workingorders", json=body)["dealReference"]

    def delete_working_order(self, deal_id: str) -> None:
        self.request("DELETE", f"/workingorders/{deal_id}")

    def activity(self, deal_id: str = "", last_period: int = 86400) -> list[dict]:
        params = {"lastPeriod": last_period, "detailed": "true"}
        if deal_id:
            params["dealId"] = deal_id
        return self.request("GET", "/history/activity", params=params).get("activities", [])

    def transactions(self, last_period: int = 86400) -> list[dict]:
        return self.request(
            "GET", "/history/transactions", params={"lastPeriod": last_period, "type": "TRADE"}
        ).get("transactions", [])

    def candle_ranges(self, epic: str, minutes: int) -> tuple[Decimal, Decimal]:
        """Return ranges of the last closed candle and the forming candle."""
        # Capital exposes MINUTE and MINUTE_5, but not 2/3/4-minute resolutions.
        # Aggregate one-minute bars so all selectable periods have identical semantics.
        prices = self.request(
            "GET", f"/prices/{epic}", params={"resolution": "MINUTE", "max": minutes * 3 + 2}
        )["prices"]
        if len(prices) < 2:
            raise CapitalError("Capital API returned fewer than two candles")

        def timestamp(candle: dict) -> datetime:
            value = candle.get("snapshotTimeUTC") or candle.get("snapshotTime")
            if not value:
                raise CapitalError("Candle has no timestamp")
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        groups: dict[datetime, list[dict]] = {}
        for candle in prices:
            moment = timestamp(candle)
            bucket_minute = moment.minute - moment.minute % minutes
            bucket = moment.replace(minute=bucket_minute, second=0, microsecond=0)
            groups.setdefault(bucket, []).append(candle)
        ordered = sorted(groups.items())
        if len(ordered) < 2:
            raise CapitalError("Not enough aggregated candles")

        def group_range(candles: list[dict]) -> Decimal:
            high = max(Decimal(str(candle["highPrice"]["bid"])) for candle in candles)
            low = min(Decimal(str(candle["lowPrice"]["bid"])) for candle in candles)
            return high - low

        return group_range(ordered[-2][1]), group_range(ordered[-1][1])


def _redact(value):
    sensitive = {item.lower() for item in SENSITIVE_KEYS}
    if isinstance(value, dict):
        return {
            key: "<redacted>" if str(key).lower() in sensitive else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
