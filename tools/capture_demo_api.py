"""Capture redacted Capital.com demo responses for integration-test fixtures.

This utility is deliberately read-only: run it before/after a demo event and give each capture
a descriptive label. It never creates, changes, or closes a position.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader.capital import CapitalClient
from trader.config import Settings


REDACT_KEYS = {
    "accountId", "accountName", "email", "identifier", "name", "password",
    "cst", "x-security-token", "x-cap-api-key",
}


def redact(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key.lower() in {item.lower() for item in REDACT_KEYS} else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("label", help="For example: before_stop, after_stop, after_trigger")
    parser.add_argument("--output-dir", default="demo_captures")
    args = parser.parse_args()

    cfg = Settings.from_env()
    if not cfg.demo:
        raise SystemExit("Refusing capture: CAPITAL_DEMO must be true")
    client = CapitalClient(cfg)
    payload = {
        "capturedAtUTC": datetime.now(timezone.utc).isoformat(),
        "label": args.label,
        "epic": cfg.epic,
        "market": client.request("GET", f"/markets/{cfg.epic}"),
        "prices": client.request("GET", f"/prices/{cfg.epic}", params={"resolution": "MINUTE", "max": 12}),
        "positions": client.request("GET", "/positions"),
        "workingOrders": client.request("GET", "/workingorders"),
        "activity": client.request("GET", "/history/activity", params={
            "lastPeriod": 86400, "detailed": "true", "filter": f"epic=={cfg.epic}",
        }),
        "transactions": client.request("GET", "/history/transactions", params={
            "lastPeriod": 86400, "type": "TRADE",
        }),
    }
    destination = Path(args.output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / f"{args.label}.json"
    path.write_text(json.dumps(redact(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved redacted read-only demo capture: {path}")


if __name__ == "__main__":
    main()
