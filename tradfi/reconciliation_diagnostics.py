"""In-memory diagnostic codes only. Never copy broker exception text."""
CODES = {
    'Raw tick and M1 observation disagree': 'RAW_TICK_M1_ROLLOVER_UNRESOLVED',
    'Raw M1 pair unavailable': 'RAW_M1_PAIR_UNAVAILABLE',
    'Invalid raw M1 timestamp': 'RAW_M1_TIMESTAMP_INVALID',
    'Invalid raw M1 OHLC': 'RAW_M1_OHLC_INVALID',
    'Raw M1 order or duplicate invalid': 'RAW_M1_ORDER_INVALID',
    'Orders changed during evidence collection': 'CURRENT_ORDER_SET_CHANGED',
    'Positions changed during evidence collection': 'CURRENT_POSITION_SET_CHANGED',
    'Position evidence changed during collection': 'POSITION_EXECUTION_EVIDENCE_CHANGED',
    'Order evidence changed during collection': 'ORDER_EXECUTION_EVIDENCE_CHANGED',
    'Terminal obligation evidence changed; reconcile before execution': 'TERMINAL_OBLIGATION_EVIDENCE_CHANGED',
    'Terminal obligation evidence is delayed': 'TERMINAL_OBLIGATION_EVIDENCE_DELAYED',
    'Required evidence API is unavailable': 'EVIDENCE_API_UNAVAILABLE',
    'Missing per-obligation evidence contract': 'EVIDENCE_CONTRACT_MISSING',
    'Missing durable command journal; no further execution': 'DURABLE_COMMAND_JOURNAL_MISSING',
    'Gateway account identity changed': 'ACCOUNT_IDENTITY_CHANGED',
    'MT5 hedging account required for explicit OCO race detection': 'HEDGING_REQUIRED',
    'Gateway history is incomplete; no trading reconciliation': 'HISTORY_INCOMPLETE',
    'Invalid executable quote': 'INVALID_EXECUTABLE_QUOTE',
    'Conflicting broker deal identity': 'CONFLICTING_DEAL_IDENTITY',
    'Не удалось подтвердить брокерские факты': 'BROKER_FACTS_NOT_CONFIRMED',
    'Неизвестные ордера/позиции': 'UNOWNED_BROKER_OBLIGATIONS',
    'Native validation failed; reconcile before execution': 'NATIVE_VALIDATION_FAILED',
}
EVIDENCE_STATUSES = set('''MISSING_DURABLE_JOURNAL UNKNOWN NO_SEND_EVIDENCE REJECTED_RESPONSE
PENDING_OBSERVED AWAIT_FILL_EVIDENCE TERMINAL_UNFILLED_EVIDENCE UNSUPPORTED_EXIT
VOLUME_EVIDENCE_INCONSISTENT PARTIAL_FILL EXTERNAL_EXIT MIXED_EXIT_REASONS AMBIGUOUS_HISTORY_TIME
API_EVIDENCE_UNAVAILABLE ORDER_NOT_FINAL FILLED_OPEN_EVIDENCE CLOSED_EVIDENCE PARTIAL_CLOSE
AWAIT_POSITION_EVIDENCE CANCEL_TERMINAL_READBACK PROTECTION_READBACK CLOSE_TERMINAL_READBACK'''.split())
SAFE_CODES = set(CODES.values()) | {'OTHER_VALIDATION_FAILURE', 'OTHER_PAUSE_REASON', 'OTHER_EVIDENCE_STATUS', 'SNAPSHOT_VALIDATED', 'SNAPSHOT_REQUESTED'}

def reason_code(reason, fallback):
    try:return CODES.get(str(reason), fallback)
    except Exception:return fallback


def evidence_codes(snap):
    if not isinstance(snap, dict):
        return []
    obligations = snap.get('obligations')
    if not isinstance(obligations, dict):
        return []
    result = []
    for cid, proof in list(obligations.items())[-100:]:
        if not isinstance(proof, dict) or not isinstance(cid, str):
            continue
        status = proof.get('status')
        result.append({'id': cid, 'status': status if isinstance(status, str) and status in EVIDENCE_STATUSES else 'OTHER_EVIDENCE_STATUS'})
    return result

# Fixed MT5 enum labels only; never serialize arbitrary broker values.
BROKER_ORDER_TYPES = {0:'BUY',1:'SELL',2:'BUY_LIMIT',3:'SELL_LIMIT',
                      4:'BUY_STOP',5:'SELL_STOP',6:'BUY_STOP_LIMIT',
                      7:'SELL_STOP_LIMIT',8:'CLOSE_BY'}
OBLIGATION_WORDS = set(BROKER_ORDER_TYPES.values()) | {'ORDER','POSITION','UNKNOWN'}

def obligation_descriptor(kind, raw, known_positions):
    """Diagnostic relationship only, NOT an ownership or execution decision."""
    value = raw.get('type')
    order_type = BROKER_ORDER_TYPES.get(value, 'UNKNOWN') if type(value) is int else 'UNKNOWN'
    if kind == 'POSITION' and order_type not in {'BUY','SELL'}:
        order_type = 'UNKNOWN'
    link = raw.get('position_id') if kind == 'ORDER' else raw.get('identifier')
    linked = str(link) in known_positions if isinstance(link,(int,str)) and not isinstance(link,bool) and str(link) not in {'','0'} else False
    return {'record_kind':kind if kind in {'ORDER','POSITION'} else 'UNKNOWN',
            'broker_order_type':order_type,'linked_to_known_position':linked}

def unowned_diagnostics(snap):
    """Bounded cause captured from the exact snapshot that triggers a pause."""
    entries=[]; count=0
    for key,kind in (('orders','ORDER'),('positions','POSITION')):
        for row in snap.get(key,[]):
            if row.get('owned'):continue
            count+=1
            if len(entries)>=20:continue
            meta=row.get('obligation_diagnostic',{})
            value=meta.get('broker_order_type') if isinstance(meta,dict) else None
            entries.append({'record_kind':kind,
                'broker_order_type':value if isinstance(value,str) and value in set(BROKER_ORDER_TYPES.values()) else 'UNKNOWN',
                'linked_to_known_position':isinstance(meta,dict) and meta.get('linked_to_known_position') is True})
    return {'unowned_count':count,'truncated':count>len(entries),'unowned_obligations':entries}
