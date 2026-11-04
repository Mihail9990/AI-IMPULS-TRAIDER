"""Read-only production UTC check. No orders, journal, clock adjustment or web server."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.time_profile import offset_at

def main():
    parser=argparse.ArgumentParser(description='Проверка UTC tick/M1 без торговли')
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    broker=None
    try:
        config=json.loads(Path(args.config).read_text(encoding='utf-8'))
        config['enable_trading']=False
        broker=MT5EvidenceBroker(config)  # No Store: no state/database mutation.
        broker.connect()
        safety=broker.time_safety
        if safety is None:raise ValueError('Production profile required')
        safety.refresh()
        reason=safety.check();verified=reason is None
        utc=safety.trusted_utc()
        result=dict(status='UTC_VERIFIED' if verified else 'WAIT_OR_REVIEW',reason=reason,
            sdk_epoch_contract='UTC_NO_OFFSET',trading=False,
            server_display_offset_seconds=offset_at(utc) if utc is not None else None)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if verified else 2
    except Exception as exc:
        print(json.dumps(dict(status='UNAVAILABLE',error_type=type(exc).__name__,trading=False)))
        return 2
    finally:
        if broker:broker.close()

if __name__=='__main__':raise SystemExit(main())
