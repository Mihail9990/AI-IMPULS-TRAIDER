# RECOVERY EA execution-core draft (offline, not deployable)

This directory is isolated from release 0.1.7. Its source was inspected for existing native position/evidence boundaries. Nothing there was modified. This is NOT a replacement for the production Python coordinator or a complete EA.

## Delivered

`RecoveryPlan.mqh` is a pure MQL5-style include: no terminal/account reads, timers, file bridge, network, order functions or lifecycle entry points. It prepares a manual-position plan only when explicit ticket AND stable position identifier match a unique, broker-confirmed, unadopted candidate selected by the manual-wait mode, regardless of its magic in the exact login/server/symbol scope on a hedging account. The adapter must prove uniqueness and the exact selection from real records; no magic-only filter.

- Existing nonzero broker SL is copied EXACTLY into required SL and opposite plain-stop trigger price. Existing TP is retained EXACTLY. No normalization, SL movement, cancellation or replacement occurs.
- Only when broker SL is absent: derive SL from actual position entry and supplied initial distance; reject non-grid result rather than round it. The two requests are not atomic. The user-confirmed no-SL handoff submits without waiting for full read-back first, and verifies both before activating RECOVERY. This planner does not dispatch either.
- Volume/tick grid checks use integer unit validation with 1e-6-unit floating division tolerance. Original SL/TP doubles are never reconstructed from those units.
- Existing trailed SL may be beyond entry. Current bid/ask stop/freeze geometry still must permit the reverse stop at exactly that level. If it does not, fail without changing SL or drifting the reverse price.
- Same-scope identity, entry, volume, SL and TP revalidation helpers; exact plain-stop evidence checks; terminal rejection and unknown outcome cannot reserve another attempt.

All returned booleans are **planning checks**, never execution authorization. `RCanReserve` takes booleans for durable uniqueness/exclusive writer but DOES NOT implement those guarantees. `RAfterAttempt` accepts facts supplied by an adapter; it does not derive them from a retcode. No blind retries are implemented.

## Two-EA architecture agreed by user

1. Trading EA is the sole order writer and owns command/state ledger and final native position/order/quote checks. Python must stop writing before migration, not run concurrently.
2. Verification EA is read-only. Existing `RecoveryEvidenceBridge.mq5` may be reused/extended, providing identity-bound observations to trading EA and Python. Reports are evidence, not commands. Verification EA must never submit, alter or cancel trades.

Trading EA must independently reselect and validate broker records immediately before every action; verification reports do not replace this final native check. Missing/contradictory report or uncertain result must leave command unresolved, never guess success or retry.

## Explicitly missing before any integration

- MQL5 MetaEditor compile and actual terminal/tester tests. No .ex5 produced.
- Terminal snapshot adapter, enumeration/uniqueness proof, exact plain-stop type mapping and durable logical-intent binding.
- Complete volume schedule and continued reverse-leg management. The planned reverse initial SL is distance-based; a confirmed pending must have exactly that broker SL. No order is submitted.
- Durable command UUID reservation, restart recovery, exclusive writer handover, cross-EA transport/parser, bounded queues, duplicate handling and acknowledgement journal.
- SL modification and stop submission ports with full MT5 return-code semantics, transaction processing and reconciliation. SL-present branch must never call the SL modification port.
- Full strategy lifecycle (fills, full/partial SL, trailing/support, recovery sizing, close/cancel) and a UI for blocked plans.
- Existing pending-order conflicts and ambiguous fills. These MUST block at the integration boundary; this planner does not enumerate them.

Rejected reverse submission must leave existing SL/TP untouched and retain the exact intended trigger price. Do not cancel a protective order or move it to make broker validation pass. An unknown result requires read-only reconciliation; no resubmission under the same or a new UUID merely to bypass uncertainty.

No age/TTL trading gate is introduced here. Snapshot consistency and identity are mandatory; elapsed time alone is not used as an authorization decision.

## Tests and limits

Run `g++ -std=c++17 -Wall -Wextra -Werror tests/test_plan.cpp -o /tmp/recovery-plan-test && /tmp/recovery-plan-test`.

43 assertions passed against the actual include through a tiny C++ compatibility shim for MQL numeric/string types. Covers exact existing SL/TP, absent-SL/read-back, trailed SL, BUY/SELL reverse geometry, off-grid price/lot rejection, scope/identity mutation, ambiguity, netting/incomplete snapshot rejection, wrong pending type and no retry after terminal/unknown states.

This is algorithm testing, NOT proof that MetaEditor accepts the MQL5 syntax or that a broker accepts an order. No live connection, trading, deployment, publication or production-file changes occurred.
