# RECOVERY: executable specification, not a live bot

This separate module implements the revised range strategy without the old TP/SL
widget controls. It **cannot send any MT5 order**. `run.py` remains the old bridge;
do not run it for RECOVERY. `run_recovery_sim.py` is simulation-only, binds loopback
port 8767, and refuses `WIDGET_LIVE`. Set a random `WIDGET_TOKEN` of at least 32
characters. Remote phone access needs a trusted HTTPS reverse proxy, not supplied.
Nothing has been installed on a VPS. No real financial transaction was performed.

## Implemented specification

* Confirm one or two ranges (exact price, explicit BUY/SELL, explicit fixed volume).
  Simulated pending orders include a stop 0.70 price units from trigger price.
* A supplied fill event resets SL to 0.70 from actual fill, and puts an opposite
  trigger at that initial SL. Closed/unknown positions cannot be modified.
* BUY progress uses BID; SELL uses ASK. At +0.25, SL is entry minus 0.25 in the
  favorable direction; at +0.50 SL is entry. Stop stage never regresses.
* Opposite trigger does NOT follow support-stop tightening. Support SL closure
  puts same-side reentry 0.70 beyond the support SL level. Opposite stays put until
  a reentry fills, then it is replaced at new position's initial SL. If opposite
  fills first the old same-side reentry is removed, avoiding duplicated entries.
* Manual closing keeps the opposite trigger. Explicit close-and-cancel removes
  only that range; cancel-all is rejected while a position is open. Replacing
  ranges is also rejected while open (not defined by user; conservative guard).
* Ranges run independently, including simultaneous positions. Counters persist
  until that range is cancelled/replaced; the completed result is archived.
  Activating the other range or BUY/SELL reversal does not reset either counter.
  Gross negative realized results accumulate as `loss`; signed results including
  supplied fees/swaps/commission accumulate separately as `realized_net`.
* No ladder, TP, lot growth, debt-based trading decisions or automatic market fills.

## API contract

All routes require `Authorization: Bearer <WIDGET_TOKEN>`. JSON state is returned
DIRECTLY, not wrapped. `mode="simulation"`, `live_trading=false` are invariant.
Prices, volumes and P/L are decimal strings in state. Input allows numeric values.
No CORS, OpenAPI or public event-injection routes. Unknown fields are rejected.

- GET `/api/recovery/state`
- POST `/api/recovery/ranges`: `{"request_id":"UUID","ranges":[{"id":"1","price":4000,"side":"BUY","volume":0.01}]}`
- POST `/api/recovery/cancel`: `{"request_id":"UUID"}`
- POST `/api/recovery/ranges/1/close-cancel`: always HTTP409; no execution adapter
  is available to confirm close and actual P/L. UI must not show success.

State has `ranges`, `positions`, `orders`, `loss_history`, `events`, `active_range`,
`can_cancel_ranges`. Each range includes id, price, side, volume, loss, realized_net,
closed_trades, session. Changes use a SQLite snapshot and idempotent UUID journal in
one transaction. Retrying an accepted UUID returns the original result; reuse with
another payload is rejected. A subsequent GET shows current state. Never delete
journal to retry commands. One process only. Engine event methods are test APIs,
not broker callbacks or network endpoints.

## Not implemented / unresolved before live adapter

* MT5 Python and EA execution, broker acknowledgments, fill/close reconciliation,
  partial fills, reconnect/restart reconciliation and server-owned order tickets.
* Tick-size rounding, stop/freeze constraints, quotes crossing at request time,
  price gaps and ordering of stop-vs-reversal execution (cannot be atomic at broker).
* Simultaneous positions in the SAME range after a broker race require reconciliation.
* Lot sizing beyond fixed input volume was not specified. No martingale assumed.
* Whether gross losses or net loss should be the primary UI label, and behavior of
  setting new ranges while positions remain open, still need explicit resolution.
* Support SL update is a simulated transition, not proof a broker accepted it.
  A real adapter must only advance confirmed stage, preserving newer protection.

Tests: `python -m unittest discover -s tests -v` from server. All tests use only
simulation/fakes. Passing them is not evidence of Android/VPS/MT5 compatibility.

## Latest counter update (partly integrated)

`full_sl_count` increments only on confirmed `initial_sl` close events; manual and
support stop closures do not increment it. At 24, range enters
`cancellation_pending`; remaining orders are marked cancel-requested, not erased.
`confirm_limit_cancellations` requires the exact confirmed cancellation set before
entering `manual`. A fill arriving during transition is rejected by the simulation
and requires explicit broker reconciliation, not assumed prevented at the broker.
Replacing ranges resets count. Volume schedule table is implemented separately but
NOT applied: order-to-full-stop counter indexing remains under clarification.

Latest confirmed update: parallel positions in different ranges are implemented;
each range retains its own loss/full-SL counters until cancellation/replacement.
The former active-range-switch reset is removed. `active_range` is display-only.

Two independent positive settings are accepted with range confirmation:
`initial_sl_distance` and `reentry_distance`, both default 0.70. They are applied
as a snapshot to each newly configured range. No separate API silently moves
existing stops. `settings` and each range expose decimal-string values. Support
thresholds remain +0.25 / +0.50; their target stops remain entry-0.25 / entry.

## Volume indexing confirmed and implemented

Initial entry is schedule slot 1. Current entry slot = full_sl_count + 1.
After full-stop close #5 next entry is 0.02. Support/manual exits do not advance.
Initial submitted volume must be 0.01; the engine calculates subsequent volumes.
The opposite prearmed reverse anticipates a full-stop close (next slot). If a
support/manual exit happens instead, its PRICE is preserved but simulated volume
is adjusted back to current slot. A real MT5 controller must cancel/recreate or
otherwise explicitly reconcile this volume change; never assume it took effect.

At slot24 the remaining contingent reverse is 0.10, but the simulated full-stop
#24 transition disarms fills and requests cancellation. This is NOT a guarantee
against an actual broker executing a prearmed reverse before receiving cancellation.
A real implementation needs an explicit cap policy (e.g. virtual final reversal or
withholding the last pending order), chosen by the user rather than silently
invented. This is a real-execution blocker, not a missing unit test.

## Final-slot policy (supersedes prior contingent-reverse description)

On filling slot24 (23 full SL closures already recorded), the engine does NOT
create an opposite trigger. Support-stop reentry may still reuse slot24 at 0.10,
but it also creates no opposite trigger. Full closure #24 requests reconciliation,
then manual mode only after explicit confirmation of absence/cancellation. Old
broker tickets, partial fills and in-flight cancellations must be reconciled by
transport; clearing simulated orders is not evidence of real cancellation.

## Review boundary: initial SL distance exactly 0.25

At favorable +0.25 the stage1 target equals the existing initial SL. The requested
rules do not define whether a later hit counts as full SL or support SL when no
actual stop movement occurred. Engine marks `stop_classification_pending` and does
not guess a full-SL increment or support reentry. Both SL close classifications are
blocked until policy is resolved; manual close remains explicit and allowed. If
price subsequently reaches +0.50, SL really improves to entry, stage2 is applied and
the ambiguity clears. Both BUY and SELL have regression coverage.

Controller binding creation now rolls memory back on persistence failure, as do
quote updates and deal handling. Final current suite: 97 tests passed (mock only).

## Integrated mock API (separate factory, not the default server)

`integrated_api.create_mock_pipeline_app(token, pipeline)` exposes the same
state/ranges/cancel paths against ONE authoritative Controller. `Coordinator`
persists request IDs, cancels mapped owned fake orders through Executor, requires
verified cancellation history, rechecks broker positions/orders, and only then
commits new ranges. It creates their pending orders through the fake pipeline.
Unknown cancellation/creation or an observed race retains incomplete state and
refuses blind replay. Existing unresolved blocked state prevents replacement.
Open broker positions block cancellation even if the local engine missed the fill.

Only a test-double pipeline can instantiate it. No live CLI/runner is supplied;
`run_recovery_sim.py` still starts the simpler standalone simulation API. Integrated
factory is covered by HTTP+fake-broker tests, not a real deployment.

Remaining: automatic pending-volume replacement during RECOVERY (not whole-range
replacement), market-close implementation, confirmed partial-remainder cancellation,
blocked-state reconciliation/unblock, startup reconstruction from broker truth and
live/VPS packaging. Exact initial SL=.25 classification still needs user decision.

### Ownership follow-up (mock only)
MockService now reloads the controller snapshot after acquiring the OS ownership
lock and validates persisted binding scope again. Controller construction no
longer rewrites an existing snapshot. API Coordinator reads and commands require
an active MockService owner; before startup or after close they return conflict
without journaling or sending. API callers must keep the service alive for the
whole request lifetime. Takeover and unowned API regression tests are included.
The low-level controller/pipeline helpers remain explicit test interfaces, not
process-safe public endpoints. Ownership alone is not startup reconciliation:
use the durable executor database and inspect unresolved broker/outbox state;
a fresh in-memory executor cannot recover prior requests. Windows locking still
requires target-platform validation. All real MT5 actions remain hard-locked out.
