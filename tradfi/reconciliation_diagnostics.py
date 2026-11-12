"""In-memory diagnostic codes only. Never copy broker exception text."""
CODES = {
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
