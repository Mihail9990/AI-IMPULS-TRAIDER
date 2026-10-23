"""Read-only MT5 diagnostic launcher. No installs, config writes, Telegram or ownership state."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import uuid

CODES={
    'WINDOWS_REQUIRED':'Run this check on the Windows VPS with MetaTrader 5.',
    'PYTHON_UNSUPPORTED':'Use 64-bit Python 3.10 or newer. Nothing was installed.',
    'MT5_DEPENDENCY_MISSING':'MetaTrader5 import failed. Install requirements-tradfi-mt5.txt manually in this Python environment.',
    'CONFIG_MISSING':'Keep inspect.json or tradfi_config.example.json in the project folder.',
    'CONFIG_INVALID':'Check the existing JSON config locally; no config was changed.',
    'CHECK_TIMEOUT':'MT5 check timed out. Check the terminal and fresh quotes, then retry.',
    'INSPECT_FAILED':'Read-only inspection failed. Check the terminal, selected account, UST hedging, profile and fresh quotes.',
    'TICK_OLD':'No fresh quote. Check Market Watch/market hours and Windows clock; trading remains disabled.',
    'TICK_AHEAD_OF_PC':'Quote is ahead of the PC clock. Check Windows clock; do not change broker timestamps.',
    'TIME_PROFILE_ERROR':'Check the explicit Bybit time profile and server in the existing config.',
    'INVALID_RESULT':'Unexpected inspection output; no execution readiness can be established.',
    'READ_ONLY_OK':'Read-only market and history requests completed; this is not trading approval.',
}


def failure(code):
    return dict(status='FAIL',code=code,explanation=CODES[code],execution_ready=False)


def summarize(data):
    """Allowlist output, including numeric types; never forward raw stdout/stderr or config."""
    if (not isinstance(data,dict) or data.get('inspect_status')!='OK'
            or data.get('mode')!='READ_ONLY' or data.get('adapter_trading_enabled') is not False
            or data.get('execution_ready') is not False or data.get('history_complete') is not False
            or data.get('connected') is not True or data.get('hedging') is not True
            or data.get('currency')!='UST' or data.get('market_data_time_status')!='VERIFIED'):
        return failure('INVALID_RESULT')
    audit=data.get('reconciliation',{})
    if (not isinstance(audit,dict) or audit.get('execution_ready') is not False
            or audit.get('history_complete') is not False or audit.get('journal_source')!='none'):
        return failure('INVALID_RESULT')
    metrics={}
    for name,source,key in (
        ('orders',data,'orders'),('positions',data,'positions'),
        ('discovered_orders',audit,'discovered_orders'),('discovered_deals',audit,'discovered_deals'),
        ('unavailable_sources',audit,'unavailable_sources'),('foreign_orders',audit,'foreign_order_count'),
        ('foreign_positions',audit,'foreign_position_count')):
        value=source.get(key)
        if type(value) is not int or not 0<=value<10**12: return failure('INVALID_RESULT')
        metrics[name]=value
    quote=data.get('quote_time')
    if not isinstance(quote,dict): return failure('INVALID_RESULT')
    for key in ('age_s','offset_seconds'):
        value=quote.get(key)
        if type(value) not in (int,float) or not math.isfinite(value): return failure('INVALID_RESULT')
        metrics[key]=value
    if abs(metrics['age_s'])>5 or metrics['offset_seconds'] not in (7200,10800):
        return failure('INVALID_RESULT')
    warnings=['Historical UTC remains UNVERIFIED; history_complete=false.',
              'No ownership journal supplied; discovered history is not owned strategy evidence.',
              'Execution remains disabled; this report does not approve trading.']
    if metrics['orders'] or metrics['positions']: warnings.append('Existing live objects observed; nothing was modified.')
    if metrics['unavailable_sources']: warnings.append('Some evidence sources are unavailable; repeat only after resolving availability.')
    return dict(status='WARN',code='READ_ONLY_OK',explanation=CODES['READ_ONLY_OK'],
                read_only_checks='PASS',execution_ready=False,history_complete=False,
                history_time_status='UNVERIFIED',journal_source='none',metrics=metrics,warnings=warnings)


def check(root, *, run=subprocess.run, platform=None):
    root=Path(root)
    if (sys.platform if platform is None else platform)!='win32': return failure('WINDOWS_REQUIRED')
    if sys.version_info<(3,10) or struct.calcsize('P')!=8: return failure('PYTHON_UNSUPPORTED')
    configs=[root/name for name in ('inspect.json','tradfi_config.json','tradfi_config.example.json')]
    config=next((p for p in configs if p.is_file()),None)
    if config is None: return failure('CONFIG_MISSING')
    try:
        data=json.loads(config.read_text(encoding='utf-8-sig'))
        if (not isinstance(data,dict) or data.get('backend')!='mt5_direct_v1'
                or data.get('time_profile')!='bybit_live6_time_v1' or data.get('expected_server')!='Bybit-Live-6'):
            return failure('CONFIG_INVALID')
    except (OSError,ValueError): return failure('CONFIG_INVALID')
    options=dict(cwd=str(root),capture_output=True,text=True,encoding='utf-8',errors='replace')
    try:
        dependency=run([sys.executable,'-c','import MetaTrader5'],timeout=20,**options)
        if dependency.returncode: return failure('MT5_DEPENDENCY_MISSING')
        result=run([sys.executable,str(root/'tradfi_main.py'),'--mode','mt5','--inspect-evidence',
                    '--config',str(config)],timeout=60,**options)
    except subprocess.TimeoutExpired: return failure('CHECK_TIMEOUT')
    except (OSError,ValueError): return failure('INSPECT_FAILED')
    if result.returncode:
        # Only fixed categories, never the terminal's/raw exception text.
        error=result.stderr or ''
        code=('TICK_OLD' if '"reason": "TICK_OLD"' in error else
              'TICK_AHEAD_OF_PC' if '"reason": "TICK_AHEAD_OF_PC"' in error else
              'TIME_PROFILE_ERROR' if 'MT5 time profile:' in error else 'INSPECT_FAILED')
        return failure(code)
    try: report=summarize(json.loads(result.stdout))
    except (ValueError,TypeError): report=failure('INVALID_RESULT')
    if config.name=='tradfi_config.example.json' and report['status']!='FAIL':
        report['warnings'].append('Example config used; inspect locally that this is the intended terminal session.')
    return report


def write_report(root,report):
    stamp=datetime.now(timezone.utc)
    name='mt5_check_'+stamp.strftime('%Y%m%d_%H%M%S_%f')+'_'+uuid.uuid4().hex[:8]+'.txt'
    path=Path(root)/name
    body=dict(report_version=1,checked_at_utc=stamp.isoformat(),**report)
    with path.open('x',encoding='utf-8',newline='\r\n') as handle:
        handle.write('MT5 READ-ONLY CHECK — '+report['status']+'\n')
        handle.write(json.dumps(body,indent=2,ensure_ascii=False,allow_nan=False)+'\n')
    return path


def main():
    root=Path(__file__).resolve().parents[1]
    report=check(root)
    try: path=write_report(root,report)
    except OSError:
        print('FAIL: Report could not be saved. Check folder write permissions; no config was changed.')
        return 2
    print(report['status']+': '+report['explanation'])
    print('Report: '+path.name)
    print('Send only this report for review. Do not send tokens, configs, SQLite or raw terminal logs.')
    print('No trading permission changed. execution_ready=false.')
    return 2 if report['status']=='FAIL' else 0

if __name__=='__main__': raise SystemExit(main())
