"""Explicit diagnostic schema: numeric trading facts, no credentials/raw messages."""
from decimal import Decimal,InvalidOperation
import hashlib
import re
from itertools import islice
from .log_export import PHASES, EVENTS, ERROR_TYPES

VERSION='panel-diagnostics-v2'
BASE='ca67567008f9d678c29a2872bee828c326bb5d8b'
NUMERIC={'sequence','line','margin_mode','price','entry','price_open','sl','tp','volume','volume_min','volume_step','volume_max','size','bid','ask','time','time_msc','utc_time','timestamp','age_seconds','raw_time_msc','offset_seconds','spread','profit','commission','swap','fee','realized','debt','recovery','retcode','revision','control_revision','point','trade_tick_size','trade_contract_size','trade_stops_level','trade_freeze_level','filling_mode','trade_exemode','trade_mode','order_mode','expiration_mode','trade_calc_mode','margin_buy','margin_sell','action','type','type_filling','type_time','deviation','started','last_candle','tick_msc','cursor','COST_RESERVE','INITIAL_SIZE','INITIAL_ENTRY_DISTANCE','INITIAL_SL_DISTANCE','INITIAL_TP_DISTANCE','BASKET_COUNT','TICKET_SIZE','RECOVERY_SL_DISTANCE','EXPANSION_DISTANCE','TP_STEP_MULTIPLIER','CYCLE_COOLDOWN_SECONDS'}
IDS={'run_id','id','ticket','position','order','deal','position_id','operation_id','receipt','receipt_id','cycle_id'}
CONTAINERS={'last_rejection','last_error','result','settings','active_settings','cfg','spec','specification','quote','positions','orders','tickets','request','commands','time_block','expanded','levels','frozen_levels','account','edit','old','new','history','error_trace'}
TEXT={'phase','status','stage','side','operation','event','reason','mode','source','symbol','error_type','function','file','line','currency_profit'}
ALLOWED_WORDS={'XAUUSD.s','XAUUSDT','BUY','SELL','DEMO','buy','sell','stop_buy','stop_sell','modify_stop','cancel','close','protection','auto','USDT','UST','USD'}

NUMERIC |= {'net','opening_net','exit_net','entry_price','exit_price','opened_at','closed_at','recovery_total','cycle_net','all_closed_net','amount','at_utc','snapshot_ms','publish_ms','capture_ms','heartbeat_seconds','omitted_fields','audit_seq','written_off_recovery','recovery_retained'}
CONTAINERS |= {'cycle','completed','metrics','position_history','cycle_history','feed','time_status','control_receipts','recovery_writeoffs','timings','main_range','range_override','manual_close','projection','groups'}
TEXT |= {'latched_reason','current_reason','timestamp_semantics','candle_status','currency','completion','record_kind','latency_band','cycle_net_basis','manual_reason','migration_block'}
ALLOWED_WORDS |= {'NORMAL','SLOW_500MS','SLOW_2000MS','CHANGE','HEARTBEAT','INITIAL_POSITION','RECOVERY_POSITION','WAIT_RECOVERY','IDLE','STOPPED','READ_ONLY','UNKNOWN','APPLIED','REJECTED','CONFLICT','OBSERVED','ACKNOWLEDGED','NOT_SENT','FRESH','STALE','DISCONNECTED','VERIFIED','UNVERIFIED','MT5','INJECTED_MT5','CYCLE_COMPLETE','INITIAL','RECOVERY','PENDING','CONFIRMED','USD','EUR','RUB','BOOKED_TICKET_NET_EXCLUDING_CARRY_AND_FLOATING','open','closed','incomplete','TP','SL'}
ALLOWED_WORDS |= PHASES | EVENTS | ERROR_TYPES | {'EXTERNAL_UTC_HTTP_STATUS','EXTERNAL_UTC_INCONSISTENT','EXTERNAL_UTC_OFFSET_OR_UNCERTAINTY','EXTERNAL_UTC_RESPONSE','EXTERNAL_UTC_RTT','EXTERNAL_UTC_STALE','EXTERNAL_UTC_TIMEOUT','EXTERNAL_UTC_UNAVAILABLE','EXTERNAL_UTC_UNVERIFIED','INVALID_LOCAL_CLOCK','LOCAL_CLOCK_JUMP','TIME_CHECK_FAILED'}
BOOLS = {'enabled','ready','paused_gap','stopped','read_only','connected','complete','history_complete','closed','retired','truncated'}

CONTAINERS |= {'diagnostics'}

from .reconciliation_diagnostics import SAFE_CODES, EVIDENCE_STATUSES
ALLOWED_WORDS |= SAFE_CODES | EVIDENCE_STATUSES
from .reconciliation_diagnostics import OBLIGATION_WORDS
ALLOWED_WORDS |= OBLIGATION_WORDS
NUMERIC |= {'unowned_count'}
CONTAINERS |= {'unowned_obligations'}
TEXT |= {'broker_order_type'}
BOOLS |= {'linked_to_known_position'}

def ident(value):
    value=str(value)
    if re.fullmatch(r'hash-[a-f0-9]{16}',value):return value
    return 'hash-'+hashlib.sha256(value.encode()).hexdigest()[:16]

def project(value,key='',depth=0):
    if depth>7:return None
    if isinstance(value,dict):
        result={}
        for k,v in islice(value.items(),200):
            if k not in NUMERIC|IDS|CONTAINERS|TEXT|BOOLS|{'identifier'}:continue
            safe=project(v,k,depth+1)
            if safe is not None:result[k]=safe
        return result
    if isinstance(value,(list,tuple)):return [project(v,key,depth+1) for v in (value[:100] if key=='position_history' else value[-100:])]
    if key in IDS:return ident(value)
    if value is None:return None
    if key in NUMERIC:
        try:
            n=Decimal(str(value))
            return str(n) if n.is_finite() and len(str(n))<=80 else None
        except (InvalidOperation,ValueError):return None
    if isinstance(value,bool):return value
    if key=='identifier':return '••'+str(value)[-2:]
    if key in TEXT and isinstance(value,str):
        if value in ALLOWED_WORDS or re.fullmatch(r'text-[a-f0-9]{16}',value):return value
        return 'text-'+hashlib.sha256(value.encode()).hexdigest()[:16]
    return None
