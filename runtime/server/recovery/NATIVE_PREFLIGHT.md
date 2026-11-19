# Native MT5 preflight, development stage

`native_preflight.py` adds a **non-executing** boundary for an already initialized
MT5 Python session. It does not initialize or log in to a terminal, and it never
sends an order. `send()` is permanently disabled in this component. Existing
mock runners and their safety locks are unchanged.

## Implemented

* Immutable expected account, server, symbol, and dedicated magic set.
* Verify connected hedging account before and after reads/check; require both
  account trading permissions and terminal algorithmic/API trading permissions.
* Validate symbol selection, direction-specific permissions, stop/SL flags,
  tick and lot grids, minimum/freeze distances, and execution/filling policy.
* CLOSEONLY and LONGONLY do not incorrectly prohibit closing an owned BUY via SELL.
* Native Market Execution close request omits price. Fill policy uses the
  SYMBOL_FILLING bitmask, not the differently numbered ORDER_FILLING enum.
* Position modification/close requires matching ticket, stable identifier,
  symbol and owned magic. Existing TP is preserved. An externally tightened SL
  cannot be weakened even when the local state still reports stage zero.
* Pending entry volume is limited to the specified maximum 0.10 lot.
* `check()` calls only `order_check`; success is explicitly not a trading
  readiness certificate, execution acknowledgment, or reusable send permission.

## Before production

This is NOT the real trading service. The live scheduler/API, persistent deal
cursor and outbox integration, broker acknowledgments, unknown/partial results,
manual stop-change classification, restart/reconnect reconciliation, and actual
Windows MT5 checks must be integrated and tested before execution is enabled.
No live terminal was connected or broker request sent during development.

A deliberate conservative difference: if correcting SL from actual entry would
weaken an already attached SL, validation rejects the change rather than weaken
protection. The lifecycle must preserve the existing SL and report/reconcile the
state; it must not treat that rejection as successful correction.

## References

* https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py
* https://www.mql5.com/en/docs/python_metatrader5/mt5terminalinfo_py

## Offline verification

From `server`: `python -m unittest discover -s tests -p 'test_recovery*.py'`

The new tests use an injected fake, including one without the mock marker, to
verify native validation without network access or any financial transaction.

### Unresolved close-stop evidence

The official Python `history_deals_get` TradeDeal tuple does **not** expose a
`sl` field. A fake deal with such a field is not proof that the real terminal can
supply it. Distinguishing a full initial SL from a support SL (including external
manual changes) needs verified closing-order history or an EA enrichment carrying
MQL5 `DEAL_SL`, correctly bound to the deal/position. Missing evidence must remain
unresolved and must not advance the volume schedule. No production enrichment or
closing-stop evidence reader is supplied by this preflight module.

### Conservative validation limits

Even cancellation currently requires selected symbol metadata, a valid quote and
enabled symbol/terminal trade permissions. It therefore fails closed if those are
unavailable; it does not claim that the broker itself necessarily forbids cancel.
Session double-checks do not make symbol/quote reads atomic. Never cache a validated
native request for a future send; a production executor must validate again at
submission and reconcile subsequent broker evidence.
