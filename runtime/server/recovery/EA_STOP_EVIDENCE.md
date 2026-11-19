# Experimental read-only DEAL_SL exporter

This adds evidence acquisition source, **not a live execution bridge**. Existing
live locks are unchanged and neither parser nor exporter is connected to RECOVERY
counters, order creation or API commands. No terminal/account was connected.

`mql5/RecoveryDealEvidence.mq5` is an EA which exports ONE explicitly selected
historical deal using the documented boolean property getters. It accepts only
DEAL_ENTRY_OUT + DEAL_REASON_SL and BUY/SELL deals. Account, server, symbol,
position identifier and exact deal ticket must be supplied. It polls every second
until successful, then stops its timer. It performs no trading/network calls.
Read errors, disconnected terminal, wrong account and non-SL exits produce no
new completed export. Removing the EA stops retries.

## Protocol and consumer contract

- Caller creates a new random 32-character lowercase hexadecimal request ID for
  each request and binds account/server/deal/order/position/symbol/exit side/time
  and exact filled volume from persisted ownership plus authoritative history.
- Output: terminal-local `MQL5/Files/recovery-evidence-<request>.tsv` (UTF-8).
  All identifiers are decimal text, not floating-point JSON numbers. One line:
  version, request, account, server, deal, order, position, symbol, exit side,
  time_msc, volume, DEAL_SL, END. Tab separated, terminated by exactly one LF or CRLF. FILE_TXT on Windows
  may expand LF to CRLF; the parser accepts these two forms only.
- Temporary file is flushed/closed before move; OS atomicity is NOT assumed.
  Missing/truncated/invalid output must not advance a cursor or full-SL counter.
- `ea_stop_evidence.parse_stop_evidence` rejects any binding mismatch and returns
  only a positive Decimal level. It DOES NOT classify full versus support SL.
- Consumer must check account/server before and after its broker-history reads,
  compare the complete binding, and apply persisted event deduplication. Export
  replay does not constitute a new deal. One partial close is not full closure.
- Reconnect/restart: request the same historical ticket under a NEW request ID;
  reconcile broker history and persisted ownership first. Never infer evidence
  from absence, stale output, execution price or a remembered local stop stage.
- Restrict terminal folder access to the service/terminal owner. Request IDs
  prevent accidental stale reuse, not forgery. This is not cryptographic
  authentication and cannot trust files from untrusted local writers.

## Required before production

Compile in MetaEditor on Windows and verify actual broker exports against
HistoryDealGetDouble(DEAL_SL), including partial fills, SELL/BUY, slippage,
external SL changes, restarts, account changes, empty history and file failures.
No MQL5 compiler or Windows MT5 integration was available here; Python tests only
validate parser fixtures and static absence of trade calls, not EA execution.
Automated request transport, secure folder setup, ledger integration, full-close
aggregation and strategy classification remain unimplemented. At equal initial
and support levels (e.g. .25), strategy policy still needs resolution.

Official reference: https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties
Documents DEAL_SL for exit deals as the position's SL at closing time.
File publication reference: https://www.mql5.com/en/docs/files/filemove

## Numeric representation

MQL5 stores SL/volume as binary doubles. `DoubleToString(value,16)` can expose
a binary representation tail (for example 3999.3000000000001819). The parser
preserves this decimal text; it does not round to a tick, classify stops or silently
treat approximately equal levels as equal. A future classifier needs verified
symbol tick-size/digits and explicit canonicalization before comparing with a
persisted SL. Volume binding is exact and therefore may conservatively reject
a record if its double rendering differs from the caller's Decimal representation;
that is unavailable evidence, not permission to infer a match. Production needs
an explicitly tested canonicalization policy for both fields.
