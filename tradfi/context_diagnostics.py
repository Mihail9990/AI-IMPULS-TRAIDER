"""Explicit diagnostic schema: numeric trading facts, no credentials/raw messages."""
from decimal import Decimal,InvalidOperation
import hashlib
import re

VERSION='panel-diagnostics-v1'
BASE='caa4a30b9f469ef9a9df41cb4abda9eabd6b3cbe'
NUMERIC={'sequence','line','margin_mode','price','entry','price_open','sl','tp','volume','volume_min','volume_step','volume_max','size','bid','ask','time','time_msc','utc_time','timestamp','age_seconds','raw_time_msc','offset_seconds','spread','profit','commission','swap','fee','realized','debt','recovery','retcode','revision','control_revision','point','trade_tick_size','trade_contract_size','trade_stops_level','trade_freeze_level','filling_mode','trade_exemode','trade_mode','order_mode','expiration_mode','trade_calc_mode','margin_buy','margin_sell','action','type','type_filling','type_time','deviation','started','last_candle','tick_msc','cursor','COST_RESERVE','INITIAL_SIZE','INITIAL_ENTRY_DISTANCE','INITIAL_SL_DISTANCE','INITIAL_TP_DISTANCE','BASKET_COUNT','TICKET_SIZE','RECOVERY_SL_DISTANCE','EXPANSION_DISTANCE','TP_STEP_MULTIPLIER','CYCLE_COOLDOWN_SECONDS'}
IDS={'run_id','id','ticket','position','order','deal','position_id','operation_id','receipt','receipt_id','cycle_id'}
CONTAINERS={'last_rejection','last_error','result','settings','active_settings','cfg','spec','specification','quote','positions','orders','tickets','request','commands','time_block','paper','expanded','levels','frozen_levels','account','edit','old','new','history','error_trace'}
TEXT={'phase','status','stage','side','operation','event','reason','mode','source','symbol','error_type','function','file','line','currency_profit'}
ALLOWED_WORDS={'XAUUSD.s','XAUUSDT','BUY','SELL','DEMO','PAPER','buy','sell','stop_buy','stop_sell','modify_stop','cancel','close','protection','auto','USDT','UST','USD'}

def ident(value):
    value=str(value)
    if re.fullmatch(r'(?:[0-9]{1,20}|[a-f0-9-]{16,80})',value):return value
    return 'hash-'+hashlib.sha256(value.encode()).hexdigest()[:16]

def project(value,key='',depth=0):
    if depth>7:return None
    if isinstance(value,dict):
        result={}
        for k,v in list(value.items())[:200]:
            if k not in NUMERIC|IDS|CONTAINERS|TEXT|{'enabled','ready','paused_gap','stopped','identifier'}:continue
            safe=project(v,k,depth+1)
            if safe is not None:result[k]=safe
        return result
    if isinstance(value,(list,tuple)):return [project(v,key,depth+1) for v in value[-100:]]
    if key in IDS:return ident(value)
    if key in NUMERIC:
        try:
            n=Decimal(str(value))
            return str(n) if n.is_finite() and len(str(n))<=80 else None
        except (InvalidOperation,ValueError):return None
    if isinstance(value,bool):return value
    if key=='identifier':return '••'+str(value)[-2:]
    if key in TEXT and isinstance(value,str):
        if value in ALLOWED_WORDS or re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}',value):return value
        if key in {'event','function','file','error_type'} and re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.]{0,79}',value):return value
        return 'REDACTED_TEXT'
    return None
