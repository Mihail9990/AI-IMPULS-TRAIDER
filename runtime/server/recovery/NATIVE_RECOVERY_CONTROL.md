# Native initial-range control boundary

`NativeRecoveryControl` joins the offline RECOVERY initial-range planner to
`NativeExecutorSession`. It can send actual pending orders if a caller explicitly
invokes `submit_initial` with an initialized native API. No real API was used in
development; tests inject doubles. Do not deploy as an autonomous bot.

- Construction requires an active process-owned session and owned magic.
- `stage_initial_ranges` plans one or two exact-price initial triggers at 0.01 lot,
  enforces initial SL >=0.60, keeps positive reentry distance independent, rejects
  existing owned exposure, and persists without sending.
- `submit_initial(range_id)` explicitly submits ONE trigger after current symbol
  planning and native validation. It commits a dispatch checkpoint first and maps
  a broker ticket only from exact VERIFIED executor evidence.
- Every generation/key is durable. An existing generation cannot be replaced by
  this component. Calling submit again never sends again, even after restart.
- `reconcile` reads existing executor outcomes and updates local mapping only. A
  crash before outbox preparation leaves an unresolved dispatch checkpoint; it is
  not permission to retry under another key. A failed mapping write after a known
  result can be recovered from the same journal key.

This is NOT a replacement for Controller/MockPipeline. Their simulation guards
are unchanged. It deliberately does not expose simulation state as real positions.

## Remaining integration

No unattended native deal listener, position/SL support/reverse lifecycle, EA
bridge, Android route, unattended loop or range cancellation/replacement is
provided here. Explicit bounded entry observation and partial-cancel late-fill
reconciliation are described below. In particular, after a trigger fills, this boundary alone does not
place the opposite trigger or move support SL. Those are mandatory before the
requested autonomous RECOVERY can be deployed. Windows broker behavior remains
untested. A dedicated terminal and account synchronization are still necessary;
no sequence of local reads makes terminal account switching atomic with send.

## Durable strategy configuration

Schema version 2 stores validated exact range IDs, sides, prices and initial
volumes plus both independent distances. `strategy_config()` validates and returns
an isolated copy after restart. It is configuration only, not a live position
ledger. Legacy generations without this information remain reconcilable but cannot
supply lifecycle configuration: missing reentry distance is never guessed from
an order SL or silently reset to a default.

## Explicit native entry observation (added)

`NativeEntryObserver(control).observe(range_id)` reads the actual native API through
`NativeEntryReader`, rather than accepting caller-supplied fills. It persists exact
entry/exit deal fingerprints, actual fill volume and weighted price. Repeated
observations/restarts do not duplicate fills; disappeared/changed history is an
error. It distinguishes pending, partial remainder, terminal unfilled, closed before
protection and awaiting protection. These are observations, not execution promises.

`cancel_partial_remainder(range_id)` is an explicit one-shot broker cancellation
of the mapped pending remainder after partial fill. It journals the dispatch before
sending, re-reads history afterwards to include late fills, and never tops up the
filled position. `reconcile_partial` recovers existing outcomes without sending.
A checkpoint with no outbox, ambiguous cancel outcome, or failed commit stops
further commands until reconciliation. An identical call never sends twice.

Test doubles cover full fill/restart, partial fill plus late fill during cancellation,
changed deal payloads, interruption before cancellation and commit failure after
verified cancellation. No real account was connected. This is still NOT an
unattended lifecycle: actual-entry SL correction, support SL, reverse placement,
close classification and range management are not connected to these observations.
`awaiting_protection` explicitly means that work remains; it does not mean protected
by a newly corrected stop. The original broker-attached stop is not removed here.

### Partial execution before initial verification

`resolve_initial_partial(range_id)` now supports the case where the initial order
partially fills before the executor first verifies it. It requires the original
journal receipt's exact positive order ticket, matching original request and order
fields, and reconciled entry/position history. `NativeEntryReader.read_receipt`
returns evidence only; control then commits `partial_entry_verified` to the same
journal key before saving the mapping. No order is sent by resolution.

A timeout with no exact ticket remains unresolved, never matched by comment/magic.
Failed control commit after verified evidence recovers from the same journal on
restart. Generic Executor's pending volume equality was not loosened. Restart also
keeps unresolved partial-cancellation checkpoints blocking new range submissions,
even when no corresponding outbox row exists yet.

## Native protection and scoped multi-position lifecycle (added)

Explicit, caller-driven native components now exist, tested only through native
API doubles (not a Windows terminal):

- `NativeEntryProtection.initial/support` confirms actual-entry initial SL and
  support levels. BUY uses BID, SELL uses ASK. Threshold .25 targets entry minus/
  plus .25; .50 targets entry. TP is preserved. Unknown dispatches survive restart
  and cannot be replayed. Initial widening requires `InitialStopProof` tied to
  durable stage-zero state and exact original attached stop; see
  `INITIAL_STOP_PROOF.md` for non-atomic/ABA limitations.
- `NativeInitialReverse.arm` places the opposite while the current position is
  still open, after initial protection is confirmed. Support SL does not move
  that pending. An explicit `order_key` can select a mapped reverse/reentry fill;
  subsequent lots use classified full-SL closes only.
- `NativeCloseLedger.resolve_sl` uses exact MQL5 DEAL_SL responses from the bound
  terminal's local mailbox, not exit execution price or current SL. See
  `LOCAL_EVIDENCE_BRIDGE.md`: trusted directory ACLs are a deployment prerequisite,
  not a cryptographic guarantee. Ambiguous evidence cannot advance the count.
- `NativeSupportReentry.arm` places same-direction reentry at the support stop plus/
  minus saved reentry distance, leaving the old opposite untouched.
- After actual reentry fill and protection, `NativeReentryRetirement.retire`
  cancels the exact old opposite, checks that it did not fill, then permits a new
  opposite at the reentry's initial SL. No market-entry fallback or lot top-up.

End-to-end double tests cover a support/reentry/replacement cycle and all 24 full
SL closes with the approved lot schedule. There is no 25th pending trigger.
These tests do NOT demonstrate a production-ready unattended runner. The symmetric
race where the old opposite fills while same-side reentry remains pending, complete
manual/range-reset controls, scheduled event coordinator, Android/native API hookup
and Windows/MQL compilation/live-broker validation still require work. Historical
scope notes above describe the incremental boundary at the time each part landed.

### Additional branch and explicit manual controls

The symmetric support race is now covered: if the old opposite already filled,
new same-side reentry is refused. If reentry was pending before the opposite fills,
retirement can cancel that remaining reentry after the opposite fill/protection,
before the next opposite is created. Final broker reads and sends remain non-atomic.

`NativeCloseRange.close_and_cancel(range_id)` is an EXPLICIT action. It retires
only that range's mapped pending orders, then freshly reads and closes its selected
position. Other ranges remain untouched. Ordinary manual closes observed from
Bybit/MT5 do not invoke cancellation and preserve existing triggers. Partial market
close or cancellation raced by another fill remains unresolved, with no blind
repeat and no closing a newly appeared position. A crash can leave a planned group
incomplete; read-only reconciliation does not silently finish new actions.

`NativeRangeManagement.cancel_ranges` is allowed only with no owned open positions;
unknown/unmapped owned pending orders block it. Confirmed cancellation enters a
waiting state that disables old-generation sends. `replace_ranges` requires that
state plus fresh broker-flat/terminal history, archives the complete generation and
loss ledger in the same SQLite transaction, then stages a new UUID generation with
zero counters. It does not itself send the new orders. Historical ordinary closes
and the explicit cancellation/reset paths remain separate.

Cancel-all carries an explicit no-owned-positions condition into native preflight
and the final transport boundary, so an observed fill after planning blocks REMOVE.
The unavoidable interval after the final read is still not atomic with the broker.
A never-dispatched cancellation whose order was externally cancelled/expired can
resolve from exact terminal history without inventing an executor receipt. Missing
history remains blocked. Dispatching/unknown cancellation still never resends.

### Runtime partial-entry recovery and missing EA mailbox

Coordinator range submission and ticks now call the strict receipt-bound resolver
for pre-verification partial entries. This is evidence-only mapping, never another
entry request. The ordinary tick then cancels the remainder and retains the actual
filled volume. A missing receipt or dispatch checkpoint is still not guessed.
Regression tests exercise this through the native scheduler with API doubles.

A missing terminal RECOVERY mailbox directory is reported as pending EA deployment
(`waiting_evidence`), not a sticky execution fault. No close classification or next
lot is guessed, but already-open positions can still receive support protection.
Once the directory and correctly bound response exist, later ticks resume evidence
processing. Invalid/symlink paths and mismatched evidence are not treated as a
successful response or silently trusted.

Partial exit deals are now accounted on each tick, before the position is fully
closed; restart/deduplication do not add the same loss twice. Full-SL counts still
advance only for fully closed, classified positions. `EXPERT` reason alone remains
unclassified. An EXPERT exit can be treated as an explicit manual close only when
this control has the exact VERIFIED close-button action, scoped executor journal,
broker receipt order and matching complete exit-deal set/volume. Another EA's close
is not assumed to be ours. Standalone balance adjustments outside this position's
deal history remain outside these PnL totals.
