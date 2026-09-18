from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExecutionPolicy:
    attempts: int = 4
    confirmation_attempts: int = 10


def trigger_level_passed(direction: str, trigger: Decimal, bid: Decimal, ask: Decimal) -> bool:
    return ask >= trigger if direction == "BUY" else bid <= trigger


def is_crossed_level_rejection(reason: str) -> bool:
    text = reason.lower()
    return any(marker in text for marker in (
        "already crossed",
        "level crossed",
        "level passed",
        "wrong side of market",
        "market level",
        "invalid.level",
        "error.validation.stop.price",
    ))
