from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable


D = Decimal


@dataclass(frozen=True)
class BrokerEvent:
    event_id: str
    timestamp: datetime
    source: str
    status: str
    event_type: str
    deal_id: str
    deal_reference: str
    working_order_id: str
    direction: str
    level: Decimal | None
    size: Decimal | None
    raw: dict

    @property
    def is_stop(self) -> bool:
        return self.source == "SL"

    @property
    def is_take_profit(self) -> bool:
        return self.source == "TP"


def normalize_events(items: Iterable[dict]) -> list[BrokerEvent]:
    events = [normalize_event(item, index) for index, item in enumerate(items)]
    return sorted(events, key=lambda event: (event.timestamp, event.event_id))


def normalize_event(item: dict, index: int = 0) -> BrokerEvent:
    containers = list(_dicts(item))
    timestamp_text = _first(containers, "dateUTC", "date", "timestamp", "createdDateUTC")
    timestamp = _datetime(timestamp_text)
    deal_id = _text(_first(containers, "dealId", "affectedDealId", "positionDealId"))
    reference = _text(_first(containers, "dealReference", "reference"))
    working_order_id = _text(_first(containers, "workingOrderId", "orderId"))
    source = _text(_first(containers, "source", "channel")).upper()
    status = _text(_first(containers, "status", "dealStatus")).upper()
    event_type = _text(_first(containers, "type", "activityType")).upper()
    direction = _text(_first(containers, "direction")).upper()
    level = _decimal(_first(containers, "closeLevel", "level", "price"))
    size = _decimal(_first(containers, "size"))
    event_id = _text(_first(containers, "id", "activityId", "transactionId"))
    if not event_id:
        event_id = f"{timestamp.isoformat()}:{source}:{status}:{deal_id}:{reference}:{level}:{index}"
    return BrokerEvent(
        event_id, timestamp, source, status, event_type, deal_id, reference,
        working_order_id, direction, level, size, item,
    )


def find_close_event(items: Iterable[dict], deal_id: str, source: str) -> BrokerEvent | None:
    expected = source.upper()
    matches = [event for event in normalize_events(items)
               if event.deal_id == deal_id and event.source == expected
               and event.status != "REJECTED" and event.level is not None]
    return matches[-1] if matches else None


def find_trigger_open_event(
    items: Iterable[dict], working_order_id: str, direction: str = ""
) -> BrokerEvent | None:
    """Find a position opening linked to a saved working trigger."""
    expected_direction = direction.upper()
    matches = [
        event for event in normalize_events(items)
        if event.working_order_id == working_order_id
        and event.event_type == "POSITION"
        and event.source == "USER"
        and event.status == "ACCEPTED"
        and event.level is not None
        and (not expected_direction or event.direction == expected_direction)
    ]
    return matches[0] if matches else None


def _dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _dicts(nested)


def _first(containers: list[dict], *keys: str):
    for key in keys:
        for container in containers:
            if container.get(key) is not None:
                return container[key]
    return None


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return D(str(value).replace(",", ""))
    except Exception:
        return None


def _datetime(value: Any) -> datetime:
    if value:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)
