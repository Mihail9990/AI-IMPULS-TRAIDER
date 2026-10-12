"""Bounded, allowlisted diagnostics: never export raw logs, state or credentials."""
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import stat

ROTATION_BYTES = 5_000_000
BACKUPS = 3
INPUT_TAIL_BYTES = 512 * 1024  # per existing segment; at most four segments
DOCUMENT_BYTES = 1024 * 1024
MAX_LINE_BYTES = 4096
PHASES = {'IDLE','WAIT_CANDLE','NEXT_PAIR','PAIR','POSITION','COOLDOWN','PAUSED',
          'PAUSED_CYCLE','SAFE_PAUSE','MANUAL','WAIT_START'}
EVENTS = set('''manual_reconciliation_error safe_pause telegram_command cycle_manual_required
manual_observation may_send obligations_reconciled broker_reconciled
cooldown_finished_wait_new_close new_cycle_snapshot pair_intents_atomic cancel_intent
stop_between_branches fill_oco_intents crossed_stop_explicit_rejection_market_intent
pending_pair_cancelled actual_fill_adopted anchor_readback anchor_intent step_intent
manual_closure_accounted cycle_complete closure_accounted_and_next_branch
telegram_first_start_discard_backlog telegram_command_rejected telegram_foreign_update_ignored
telegram_outbox_delivered telegram_sendlog_requested telegram_sendlog_result manual_reset_evidence manual_reset_duplicate manual_reset
unknown_gateway_outcome protection_unchanged mt5_unavailable'''.split())
ERROR_TYPES = {'OSError','TimeoutError','ValueError','QuoteFreshnessError','ConnectionError',
               'ReadTimeout','ConnectTimeout','HTTPError','Error'}
LEVELS = {'DEBUG','INFO','WARNING','ERROR','CRITICAL'}
STAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')
LEGACY = re.compile(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)')


def _event_name(value):
    # May-send UUIDs, account/order/deal IDs and arbitrary event payloads are omitted.
    if not isinstance(value,str): return None
    name=value.split(':',1)[0]
    return name if name in EVENTS else None


def project_record(record):
    event=None; extra={}
    if record.name=='tradfi.engine':
        if record.msg=='%s phase=%s' and isinstance(record.args,tuple) and len(record.args)==2:
            event=_event_name(record.args[0])
            if not isinstance(record.args[1],str) or record.args[1] not in PHASES: return None
            extra['phase']=record.args[1]
        elif record.msg=='Unknown gateway outcome %s: %s':
            event='unknown_gateway_outcome'
        elif record.msg=='Protection unchanged: %s':
            event='protection_unchanged'
    elif record.name in {'root','tradfi.runtime'} and record.msg=='MT5 temporarily unavailable: %s':
        event='mt5_unavailable'
        kind=record.args[0] if isinstance(record.args,tuple) and len(record.args)==1 else 'Error'
        extra['error_type']=kind if isinstance(kind,str) and kind in ERROR_TYPES else 'Error'
    if not event or record.levelname not in LEVELS: return None
    stamp=datetime.fromtimestamp(record.created,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return dict(version=1,utc=stamp,level=record.levelname,event=event,**extra)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(project_record(record),sort_keys=True,ensure_ascii=True)


def diagnostic_handler(path):
    handler=RotatingFileHandler(path,maxBytes=ROTATION_BYTES,backupCount=BACKUPS,encoding='utf-8')
    handler.addFilter(lambda record: project_record(record) is not None)
    handler.setFormatter(SafeFormatter())
    return handler


def sanitize_line(line):
    """Re-project even previously rotated/legacy files; never trust file contents."""
    if len(line)>MAX_LINE_BYTES: return None
    try:
        row=json.loads(line)
    except (ValueError,UnicodeError):
        row=None
    if isinstance(row,dict):
        if (row.get('version')!=1 or not isinstance(row.get('utc'),str)
                or not STAMP.fullmatch(row['utc']) or not isinstance(row.get('level'),str) or row['level'] not in LEVELS
                or not isinstance(row.get('event'),str) or row['event'] not in EVENTS):
            return None
        safe={k:row[k] for k in ('version','utc','level','event')}
        if isinstance(row.get('phase'),str) and row['phase'] in PHASES: safe['phase']=row['phase']
        if isinstance(row.get('error_type'),str) and row['error_type'] in ERROR_TYPES: safe['error_type']=row['error_type']
        return json.dumps(safe,sort_keys=True).encode('ascii')+b'\n'
    match=LEGACY.fullmatch(line.strip())
    if not match: return None
    stamp,level,message=match.groups()
    event_phase=re.fullmatch(r'([a-z_]+(?::[0-9a-f]{24})?) phase=([A-Z_]+)',message)
    if event_phase:
        event=_event_name(event_phase[1]); phase=event_phase[2]
        if not event or phase not in PHASES: return None
        safe=dict(version=1,legacy_local_time=stamp,level=level,event=event,phase=phase)
    elif message.startswith('Unknown gateway outcome '):
        safe=dict(version=1,legacy_local_time=stamp,level=level,event='unknown_gateway_outcome')
    elif message.startswith('Protection unchanged: '):
        safe=dict(version=1,legacy_local_time=stamp,level=level,event='protection_unchanged')
    elif message.startswith('MT5 temporarily unavailable: '):
        safe=dict(version=1,legacy_local_time=stamp,level=level,event='mt5_unavailable')
    else: return None
    return json.dumps(safe,sort_keys=True).encode('ascii')+b'\n'


def build_document(log_path):
    """Best-effort tail of fixed rotated paths. Disk work belongs only to document worker."""
    base=Path(log_path)
    lines=deque(); size=0; omitted=0; files_read=0; tails_cut=0; unavailable=0
    budget=DOCUMENT_BYTES-2048
    for suffix in ('.3','.2','.1',''):
        path=Path(str(base)+suffix)
        try:
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                unavailable+=1; continue
            with path.open('rb') as handle:
                handle.seek(0,2); end=handle.tell(); start=max(0,end-INPUT_TAIL_BYTES)
                handle.seek(start); raw=handle.read(min(end-start,INPUT_TAIL_BYTES))
            files_read+=1
            if start:
                tails_cut+=1
                raw=raw.split(b'\n',1)[1] if b'\n' in raw else b''
            for line in raw.splitlines():
                cleaned=sanitize_line(line.decode('utf-8',errors='replace'))
                if cleaned is None:
                    omitted+=1; continue
                lines.append(cleaned); size+=len(cleaned)
                while size>budget:
                    size-=len(lines.popleft()); omitted+=1
        except (OSError,ValueError):
            unavailable+=1  # rotation/missing files never expose paths or exception strings
    header=(
        'TradFi sanitized diagnostic log v1\n'
        'Scope: current configured log plus .1/.2/.3; bounded tails, not complete history.\n'
        'Only event/phase/time/severity and safe error categories; no raw exceptions, IDs, state or secrets.\n'
        f'files_read={files_read}; unavailable={unavailable}; truncated_tails={tails_cut}; '
        f'omitted_records={omitted}; exported_records={len(lines)}\n'
        'Rotation may change during reading; this is a best-effort diagnostic snapshot.\n\n'
    ).encode('ascii')
    return header+b''.join(lines)
