from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path


def _load_file() -> dict:
    path = Path(os.getenv("BOT_CONFIG_FILE", "bot_config.json"))
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def _value(values: dict, name: str, default):
    return os.environ[name] if name in os.environ else values.get(name, default)


def _bool(value) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value!r}")


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    identifier: str = ""
    password: str = ""
    demo: bool = True
    epic: str = "GOLD"
    size: Decimal = Decimal("0.1")
    stop_distance: Decimal = Decimal("1")
    target_profit: Decimal = Decimal("0.3")
    entry_range: Decimal = Decimal("3")
    candle_minutes: int = 1
    max_scenarios: int = 9
    poll_seconds: float = 0.5
    telegram_token: str = ""
    telegram_chat_id: str = ""
    dry_run: bool = True
    state_file: str = "bot_state.json"
    diagnostic_log_file: str = "bot_diagnostics.log"

    @classmethod
    def from_env(cls) -> "Settings":
        values = _load_file()
        value = cls(
            api_key=str(_value(values, "CAPITAL_API_KEY", "")),
            identifier=str(_value(values, "CAPITAL_IDENTIFIER", "")),
            password=str(_value(values, "CAPITAL_PASSWORD", "")),
            demo=_bool(_value(values, "CAPITAL_DEMO", True)),
            epic=str(_value(values, "CAPITAL_EPIC", "GOLD")),
            size=Decimal(str(_value(values, "POSITION_SIZE", "0.1"))),
            stop_distance=Decimal(str(_value(values, "STOP_DISTANCE", "1"))),
            target_profit=Decimal(str(_value(values, "TARGET_PROFIT", "0.3"))),
            entry_range=Decimal(str(_value(values, "ENTRY_CANDLE_RANGE", "3"))),
            candle_minutes=int(_value(values, "ENTRY_CANDLE_MINUTES", 1)),
            max_scenarios=int(_value(values, "MAX_SCENARIOS", 9)),
            poll_seconds=float(_value(values, "POLL_SECONDS", 0.5)),
            telegram_token=str(_value(values, "TELEGRAM_BOT_TOKEN", "")),
            telegram_chat_id=str(_value(values, "TELEGRAM_CHAT_ID", "")),
            dry_run=_bool(_value(values, "BOT_DRY_RUN", True)),
            state_file=str(_value(values, "STATE_FILE", "bot_state.json")),
            diagnostic_log_file=str(
                _value(values, "DIAGNOSTIC_LOG_FILE", "bot_diagnostics.log")
            ),
        )
        value.validate()
        return value

    def validate(self) -> None:
        if (self.stop_distance <= 0 or self.target_profit < 0 or self.size <= 0
                or self.entry_range <= 0):
            raise ValueError(
                "POSITION_SIZE, STOP_DISTANCE and ENTRY_CANDLE_RANGE must be positive; "
                "TARGET_PROFIT cannot be negative"
            )
        if self.candle_minutes not in range(1, 6):
            raise ValueError("ENTRY_CANDLE_MINUTES must be one of 1, 2, 3, 4, 5")
        if self.max_scenarios != 9:
            raise ValueError("This strategy requires exactly 9 scenarios")
        if self.poll_seconds < 0.25:
            raise ValueError("POLL_SECONDS cannot be lower than 0.25")
        if not self.dry_run and not all((self.api_key, self.identifier, self.password)):
            raise ValueError("Capital.com credentials are required when BOT_DRY_RUN=false")
