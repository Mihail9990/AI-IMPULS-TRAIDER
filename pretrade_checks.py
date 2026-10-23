"""Pre-trade checks: no Controller, SQLite mutation, Telegram, or execution route."""
import argparse
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))


class ReadOnlyMT5:
    """Capability allowlist: mutation methods are never forwarded to the binding."""
    READS=frozenset({'initialize','shutdown','terminal_info','account_info','symbol_info',
        'symbol_info_tick','positions_get','orders_get','copy_rates_from_pos'})
    def __init__(self,api):self.__api=api
    def __getattr__(self,name):
        if name not in self.READS:raise PermissionError('MT5 mutation/capability unavailable in pretrade checks')
        return getattr(self.__api,name)


def config_check(path):
    try:
        with Path(path).open(encoding='utf-8-sig') as f:raw=f.read(65537)
        if len(raw)>65536:raise ValueError()
        c=json.loads(raw)
        valid=(isinstance(c,dict) and c.get('backend')=='mt5_direct_v1'
            and c.get('time_profile')=='bybit_live6_time_v1' and c.get('expected_server')=='Bybit-Live-6'
            and type(c.get('expected_login')) is int and c['expected_login']>0
            and c.get('symbol')=='XAUUSD.s' and isinstance(c.get('terminal_path'),str)
            and Path(c['terminal_path']).is_absolute() and Path(c['terminal_path']).is_file())
        if not valid:raise ValueError()
        return c,dict(check='configuration',status='PASS',values_redacted=True,
            saved_execution_enabled=c.get('enable_trading') is True,diagnostic_execution_enabled=False)
    except (OSError,ValueError,TypeError):
        return None,dict(check='configuration',status='FAIL',code='CHECK_LOCAL_CONFIG_AND_TERMINAL_PATH',values_redacted=True)


def environment_check():
    packages={}
    for name in ('MetaTrader5','requests'):
        try:packages[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:packages[name]=None
    supported=sys.platform=='win32' and sys.version_info[:2]==(3,13) and struct.calcsize('P')==8
    return dict(check='environment',status='PASS' if supported and all(packages.values()) else 'FAIL',
        windows=sys.platform=='win32',python='.'.join(map(str,sys.version_info[:3])),bits=struct.calcsize('P')*8,packages=packages)


def inspect_mt5(config,*,api=None,get=None,clock=time.time):
    # Configuration may contain execution opt-ins; they are neither used nor changed.
    result=dict(check='mt5',mode='READ_ONLY',execution_ready=False,history_complete=False,
                timestamp_semantics='MT5_TIMESTAMP_SEMANTICS_UNVERIFIED')
    try:
        api=ReadOnlyMT5(api if api is not None else importlib.import_module('MetaTrader5'))
        if not api.initialize(config['terminal_path'],timeout=10000):raise ValueError()
        account=api.account_info();terminal=api.terminal_info()
        if not account or not terminal or not terminal.connected:raise ValueError()
        if account.login!=config['expected_login'] or account.server!=config['expected_server']:raise ValueError()
        if account.currency!='UST' or account.margin_mode!=2:raise ValueError() # MT5 retail hedging enum
        symbol=config['symbol'];info=api.symbol_info(symbol);tick=api.symbol_info_tick(symbol)
        positions=api.positions_get(symbol=symbol);orders=api.orders_get(symbol=symbol)
        bars=api.copy_rates_from_pos(symbol,1,0,120) # M1 enum; read only
        if info is None or tick is None or positions is None or orders is None or bars is None:raise ValueError()
        bid=float(tick.bid);ask=float(tick.ask);raw=float(tick.time_msc)
        if not all(math.isfinite(x) and x>0 for x in (bid,ask,raw)) or ask<bid:raise ValueError()
        result.update(status='WARN',symbol=symbol,positions=len(positions),pending=len(orders),candles=len(bars),
            bid=bid,ask=ask,raw_broker_time_msc=raw,local_observed_time=clock(),
            quote_age_seconds=None,quote_freshness='UNVERIFIED',code='OBSERVED_NOT_TRADING_APPROVAL')
        from tradfi.time_safety import TimeSafety
        safety=TimeSafety(clock=clock,get=get);safety.refresh()
        result['independent_utc']=dict(ok=bool((safety.result or {}).get('ok')),
            reason=(safety.result or {}).get('reason','UNAVAILABLE'))
        # Independent UTC alone cannot establish the meaning of MT5 server timestamps.
        return result
    except Exception:
        return dict(result,status='FAIL',code='MT5_READ_ONLY_CHECK_FAILED',details_redacted=True)
    finally:
        if api is not None:
            try:api.shutdown()
            except Exception:pass # Never leak binding errors/credentials during cleanup.


def main():
    parser=argparse.ArgumentParser();parser.add_argument('kind',choices=('environment','configuration','mt5'))
    parser.add_argument('--config',required=True);args=parser.parse_args()
    config,report=config_check(args.config)
    if args.kind=='environment':report=environment_check()
    elif args.kind=='mt5':
        if sys.platform!='win32':report=dict(check='mt5',status='FAIL',code='WINDOWS_REQUIRED')
        elif config is not None:report=inspect_mt5(config)
    print(json.dumps(report,ensure_ascii=False,allow_nan=False))
    return 1 if report['status']=='FAIL' else 0

if __name__=='__main__':raise SystemExit(main())
