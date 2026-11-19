# MT5 integration design (not an implemented executor)

## Findings

The official Python API supports pending-order creation, position SL/TP changes,
removing pending orders, and ticket-specific closes via MqlTradeRequest. Deal history
can be queried by order or position. Thus the requested integration is feasible.
References checked during development:
- https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py

`mt5_plan.py` now builds OFFLINE request templates from supplied snapshots. It does
not import MetaTrader5, initialize, log in, call order_check, or call order_send.
Templates use symbolic enum names and decimal strings deliberately: they cannot be
mistaken for completed execution. No live environment-variable bypass exists.

## Why the simulation engine must not simply be wired to order_send

The current simulation engine changes its local order ledger synchronously. Broker
creation/modification/cancellation is asynchronous and can partly fail. For example,
replacing a reverse trigger after reentry currently removes the old simulated order
and creates the new one in one step. A broker cannot atomically do those two actions;
a timeout may mean the first already happened. Treating simulation success as broker
success would either duplicate orders or leave an unprotected exposure.

Likewise quote() advances simulated stop_stage immediately, whereas a real broker
might reject an SL change. An adapter must distinguish requested and CONFIRMED stops;
only confirmed support stops classify a subsequent close as support_sl.

## Required architecture

1. Persist desired strategy state separately from observed broker state. Persist an
   outbox intent before each transmission: operation ID, account+server, symbol,
   range generation/session, logical order ID, exact payload, expected ticket and
   position identifier, state PREPARED/SENT/ACKNOWLEDGED/VERIFIED/UNKNOWN/REJECTED.
2. Use unique dedicated magic and a persisted ticket mapping. Comments assist
   diagnosis but are not authoritative ownership. Never operate on all orders of a
   symbol, all account positions, or a guessed "latest" ticket. Verify terminal,
   account/server, hedging mode, symbol metadata and current snapshot before action.
3. Translate PREPARED intent through the offline planner. Preserve exact trigger
   price; if outside tick/volume grid or too close/crossed, reject visibly. Do not
   change to market/limit, silently widen stops, or alter volume. Call order_check
   but do not treat check success as execution.
4. Persist SENT before a single order_send call. Timeout/None/transport failure is
   UNKNOWN, not a reason to send again. Reconcile order/position/deal history using
   mapped identifiers. A command UUID in local SQLite alone cannot guarantee
   exactly-once broker effects across a crash.
5. Apply engine events only after confirmed broker observations. Use deal ticket
   as event ID; history cursor must overlap reads and deduplicate. Missing position
   from one snapshot is not evidence of closing. Inspect full position history,
   entry/exit classification, reason, volume and fees. Partial fills/partial closes
   must not be mapped to the current full-fill/full-close methods.
6. Post-fill SL adjustment: reread mapped position and latest confirmed stop stage.
   If closed, skip. If support protection already advanced, never overwrite it with
   initial SL. After send, verify actual level; if stop already fired, reconcile
   closure rather than recreating or altering a different position.
7. After support stop, preserve opposite trigger and place same-side reentry only
   once. Both may fill during races/gaps before cancellation; snapshot reconciliation
   must detect simultaneous exposure and halt new entries without guessing which
   position to liquidate.
8. Replace ranges via a generation switch: stop generating intents for old generation,
   cancel old owned tickets, verify absence and no concurrent fill, then create new
   primary orders. Do not declare success if only one of two orders is placed. Keep
   partially applied state visible. Automatic rollback can itself trade/cancel and
   needs an explicit product policy.
9. Explicit close+cancel first disarms that range's strategy, cancels pending owned
   entries (which may already fill), then requests ticket-specific close under an
   approved policy and verifies both positions and orders absent. No fake P/L.
10. On restart do not replay pending sends. Reconcile UNKNOWN before enabling any
    new writes. On read error/disconnect preserve known state and show unavailable;
    do not infer empty account, reset counters, or cancel another range.

## Product choices still required

- Can two ranges have simultaneous positions? Current simulation rejects it. User
  confirmed both triggers placed ahead, not a race-resolution or exposure policy.
- If opposite and same-side reentry are simultaneously executed in a gap, what may
  the bot close/cancel and which volume is authoritative?
- Fixed lot for every reversal vs another progression (only initial 0.01 example
  exists). No martingale or loss-based volume change should be invented.
- When replacing ranges while positions exist, current conservative guard rejects;
  user explicitly disabled cancel-all while any position exists.
- Primary BUY below current ASK / SELL above current BID: stop order would not be
  valid. Do not guess limit order semantics or immediate market execution.
- Gross loss vs net deficit is displayed separately; no trading decision uses it.

## Work not performed

No MT5 executor loop, live API, order_send wrapper, EA protocol extension, VPS
installation, terminal initialization or account connection. Offline planner tests
pass, but that does not validate broker-specific symbol/trading conditions.

## Implemented follow-up: durable executor core

`executor.py` implements a transport-injected SQLite outbox and reconciliation for
pending creation, protection updates and cancellation. Tests inject a fake only.
PREPARED -> SENT is committed before transmission; accepted receipt becomes
ACKNOWLEDGED, not success. Only matching observed evidence becomes VERIFIED. Lost
responses, mismatched levels, missing positions or unavailable snapshots remain
UNKNOWN with no resend. A restart cannot turn SENT/UNKNOWN into PREPARED. A lost
creation receipt has no known ticket: never guesses a binding from comment/magic.
Cancellation requires absent live order plus matching cancelled order history.
SL verification binds position identifier as well as ticket/magic/symbol.

This is working executor infrastructure, NOT the finished live adapter. No concrete
MT5 transport, enum conversion, market-close execution or strategy-controller loop
is supplied. A future transport must fulfill its preflight contract for fresh
metadata, account/server, symbol, ownership and broker checks. The fake only tests
state-machine behavior; it does not prove broker preflight correctness.

Final-slot policy is now specified: do not create a real opposite pending trigger
for slot24. Engine implements this intent. Existing owned broker orders must be
cancelled with confirmation; no phantom success from local removal. A fill from an
old/in-flight order still requires observed-position reconciliation, not ignoring
the fill event. This removes the need to prearm a new 25th order, not all broker
race risk. No actual transport/controller has been installed.

## Concrete transport added, mock-only locked

`mt5_transport.py` implements MT5 enum/numeric request conversion, fresh
account/server/hedging/trade permission checks, symbol/quote planning checks,
position and order ownership validation, order_check and repeated preflight before
send. It normalizes live order/position/history-shaped objects for Executor.
Tests run it through the durable executor using FakeMT5. The real MetaTrader5
module cannot construct this transport: `_RECOVERY_TEST_DOUBLE is True` is mandatory
and rechecked at every entry. There is no CLI or API live enablement path.

Still incomplete: production trade-mode/allowed-order flags and execution-policy
coverage, persistent long-range history cursor (snapshot prototype reads 30 days),
deal/partial-fill aggregation and confirmed strategy-controller event application,
market close, full startup reconciliation. These are production blockers, not
claims of real broker compatibility. No real module was initialized or called.

## Mock confirmed-deal controller implemented

`controller.py` persists engine snapshots, verified ticket bindings and all accepted
deal evidence in one SQLite record/transaction. Unique deal tickets deduplicate
across restart. Partial entry volumes aggregate to a weighted fill; only exact full
entry advances Engine. Partial exits accumulate realized results (including supplied
entry and exit charges); full SL count advances only at exact full closure with a
single confirmed reason. Scope/identifier mismatch rejects without mutation.
Overfill, exit-before-complete-entry, mixed close reasons and engine race errors
persist evidence and block that range instead of fabricating a strategy transition.

Limitations: this controller consumes explicitly supplied fake events; no real deal
listener, cursor, mapping discovery or unblock/recovery workflow. A broker can
partially fill then cancel the remainder: that lifecycle is deliberately blocked.
During partial exits engine's strategy position retains original logical volume;
`bindings.observed_open_volume` is the current observed remaining amount. It must
NOT be replaced by engine volume when implementing an actual close request. Early
partial exposure protection, changed average entry during filling, fees posted
separately later, multi-position netting and out-of-order event buffering are not
implemented. Quote-to-support-stop transition is simulated, not a broker SL ack.

## Review fixes

- Engine checks monotonic SL price, not stage alone. If a short initial stop is
  already better than stage1, quote leaves both stop and confirmed stage unchanged.
- Controller prohibits multiple broker tickets for one logical order and reuse of
  a position identifier across bindings. Unsupported multi-ticket exposure must be
  reconciled instead of coalesced into one fictional engine position.
- Controller quote rolls memory back if persistence fails.
- Executor uses a conditional SQLite PREPARED→SENT update as atomic send claim;
  process-local locking is not the sole defense. Losing claim cannot send. Stale
  preflight rejection cannot overwrite another claimant's SENT state.

Regression tests pass using independent Executor database connections racing in
threads. This does not add multi-writer safety for Controller/API snapshots: those
still require one owner process. Partial exposure protection and real close-reason
classification remain unresolved production integration work.

## End-to-end mock pipeline

`pipeline.py` now joins pending planning, durable Executor, MT5-shaped Fake transport,
verified order binding, normalized IN/OUT deal consumption and confirmed protection.
Support stage advances only after a fresh matching broker snapshot, not after the
request/ack alone. Even cached VERIFIED results are freshly revalidated before
changing controller protection state. A lost SL response can be confirmed from
actual matching SL/TP; unverified change blocks SL-reason classification.

Manual mobile/web/client closes with magic0 map by known position identifier, not
closing-order ticket. Unknown owned entry mapping blocks observation/cursor advance.
`poll_history()` performs one explicitly invoked fake polling cycle: persistent
millisecond high-water mark with 5-minute overlap, dedupe by deal ticket, no cursor
advance on missing/empty history. Initial lookback is 30 days, not infinite.

`protect_partial()` uses observed broker remaining volume and actual average entry;
for partial IN it verifies initial-distance SL without inventing a full strategy
position, preserving tighter existing protection. Partial OUT preserves previously
confirmed strategy SL. Ledger/broker volume mismatch blocks writes. Verified partial
SL/volume are exposed on binding. It does not synthesize support stages for partial
IN or resolve partial-fill-then-cancel-remainder automatically.

Remaining blockers: pipeline is serial/mock-only and not connected to phone API;
create/cancel/replace transactions and changed pending volume require explicit
reconciliation rather than automated recovery; market close is absent; late events
older than overlap, separately posted fees, partial remainder cancellation and
unblock workflow need design/testing. There is no autonomous loop or live startup.

## API/coordinator integration added

`coordinator.py` + `integrated_api.py` bind HTTP commands to the same Controller and
MockPipeline. Range replacement validates candidate first, checks actual positions,
verifies all mapped owned-order cancellations, rechecks absence, commits candidate,
then synchronizes new fake pending orders. Request status is persisted before side
effects; interrupted/failed request cannot replay blindly. Partial creation errors
are explicitly failures with state available for inspection, not atomic rollback.
Seven integrated HTTP tests cover create/replace/cancel, auth, idempotence, lost
cancel outcome, late position appearance and stale-empty engine with actual position.
Final current suite: 115 tests passed; all broker calls used injected fake objects.

## Additional mock maintenance primitives

`maintenance.py` adds:
- verified pending-volume cancel/replace at unchanged price with a NEW logical
  identity/ticket, avoiding two bindings for the same logical order;
- explicit partial-entry remainder cancellation, preserving actual position and
  blocking disposition instead of pretending the partial position is a full entry;
- explicit full remaining-position market-close primitive with required filling
  policy, ownership/preflight and closure proof (exit deals for returned order plus
  absence of that position identifier). Partial result remains UNKNOWN; no replay;
- startup read-only report of unknown/missing mapped orders/positions and unresolved
  outbox intents. It does not autonomously reconstruct or alter broker state.

Executor and hard-locked transport support market-close mock requests. This primitive
is not wired to the phone close-cancel button: confirmed closure still must flow
through deal ingestion and range disposal. The primitive deliberately does not fake
Engine closure/P&L. Explicit bot-close reason mapping and complete close-cancel
workflow remain unfinished. Partial remainder disposition is a user policy question.
Pending-volume replacement conservatively refuses while ANY broker position exists,
including the other range; supporting independent-range races needs more work.
Final current test suite: 121 passed, all fake-only.

## Mock close-cancel API finalized

Integrated factory accepts an explicit tested `market_filling` policy. Without it,
close-cancel fails without sending. With it, coordinator disarms the selected range,
verifies cancellation of its pending orders, obtains full market-close proof, ingests
actual-shaped deal history/P&L, then archives/removes ONLY that range. The other range
is preserved. Partial close/fill races/unknown results retain blocked state; command
UUID cannot replay blindly. Bot-generated EXPERT close is recognized only by an
owned persisted close request with matching receipt order, identifier and scope.
Unrelated EXPERT/TP/stopout is not classified as manual. All mock only.

Future-history timestamps are now validated for the entire batch BEFORE durable
ingestion; invalid batch cannot alter Engine, deal journal or cursor.
Current complete suite: 126 tests pass. No actual terminal or account was used.

## Sequential fake lifecycle

`service_loop.py` adds a bounded, explicitly driven mock cycle: history ingestion,
partial protection, full-position confirmed support protection, pending synchronization.
It shares Controller's lock with the API coordinator. Persistent databases acquire
an OS owner-file lock; a second service instance cannot own the same controller DB.
Windows locking code exists but tests here ran on Linux; no Windows deployment claim.

Create that fills before verification now binds only from matching receipt/history
(type + initial volume required), waits for actual fill history, then resumes.
Reentry retirement cancels the exact old reverse ticket and requires verification
before placing its new opposite order. Timeout leaves old order and blocked state,
not a duplicate replacement. These paths have end-to-end fake lifecycle tests.
No real scheduler/daemon/MT5 instance is initialized; hard test-double lock remains.
Current suite: 131 tests passed.
