"""Strict parser for experimental, local read-only MQL5 DEAL_SL exports.

Used by the optional strict TEST lifecycle, not a live EA bridge. Caller must establish provenance and
compare the binding with the current authoritative broker history and ledger.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


class StopEvidenceUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class ExitBinding:
    request_id: str
    account: int
    server: str
    deal: int
    order: int
    position: int
    symbol: str
    exit_side: str
    time_msc: int
    volume: Decimal


def parse_stop_evidence(payload: bytes, binding: ExitBinding) -> Decimal:
    """Validate a single record; return a level, never a trading decision.

    Replayed identical historical evidence is idempotent, NOT a new exit event.
    Exact binding is mandatory; no inferred ticket, price, or account is allowed.
    """
    def fail():
        raise StopEvidenceUnavailable('Missing, malformed or mismatched SL evidence')
    if not isinstance(payload, bytes) or len(payload)>8192:
        fail()
    if not re.fullmatch(r'[0-9a-f]{32}', binding.request_id):
        fail()
    ids=(binding.account,binding.deal,binding.order,binding.position,binding.time_msc)
    if any(type(x) is not int or x<=0 for x in ids):
        fail()
    if binding.exit_side not in ('BUY','SELL') or not isinstance(binding.volume,Decimal) or not binding.volume.is_finite() or binding.volume<=0:
        fail()
    if any(not isinstance(s,str) or not s or any(c in s for c in '\t\r\n') for s in (binding.server,binding.symbol)):
        fail()
    try:
        text=payload.decode('utf-8')
        # FILE_TXT/FileWriteString on Windows may expand the terminal LF to CRLF.
        # Accept exactly one LF or CRLF terminator, never embedded line breaks.
        if text.endswith('\r\n'):
            record=text[:-2]
        elif text.endswith('\n'):
            record=text[:-1]
        else:
            fail()
        if '\r' in record or '\n' in record:
            fail()
        fields=record.split('\t')
        if len(fields)!=13:
            fail()
        expected=['RECOVERY_DEAL_SL_V1',binding.request_id,str(binding.account),binding.server,str(binding.deal),str(binding.order),str(binding.position),binding.symbol,binding.exit_side,str(binding.time_msc)]
        if fields[:10]!=expected or fields[12]!='END':
            fail()
        if any(not re.fullmatch(r'[0-9]+\.[0-9]{16}',v) for v in fields[10:12]):
            fail()
        volume,sl=map(Decimal,fields[10:12])
        if volume!=binding.volume or not sl.is_finite() or sl<=0:
            fail()
        return sl
    except (UnicodeError,InvalidOperation,TypeError) as exc:
        raise StopEvidenceUnavailable('Malformed evidence') from exc
