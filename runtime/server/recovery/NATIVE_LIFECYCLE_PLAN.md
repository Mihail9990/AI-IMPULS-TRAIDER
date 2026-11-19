# Next native lifecycle boundary (not implemented)

The initial submission bridge is not an autonomous runner. The next component must
consume broker history itself; arbitrary caller-provided `fill` assertions cannot
be evidence for placing an opposite order.

## Entry observation

1. Under NativeExecutorSession ownership lock, load the generation's schema-v2
   strategy config and exact VERIFIED order mapping. Missing legacy config must
   not be filled with defaults.
2. Fetch deals for the exact mapped entry-order ticket, with account/server checks
   before and after reads. Match symbol, magic, entry direction, position identifier
   and deal type. Persist each deal's immutable fingerprint. Duplicate identical
   tickets are no-ops; changed payloads are reconciliation faults.
3. Aggregate actual filled volume and weighted price. Match historical order and
   current position identifier, not a guessed position ticket. A current position
   ticket can differ from its persistent identifier. Do not invent a fill merely
   from pending disappearance.
4. Partial fill: keep filled exposure, cancel exact unfilled remainder through the
   same durable dispatch boundary, reconcile cancellation and any late extra fills,
   then finalize actual volume. No second initial order and no volume top-up.
5. Persist observed exposure and an explicit protection-required transition. Keep
   the broker-attached initial stop until correction is verified. If already closed,
   do not modify or fabricate a surviving position. Restore these phases on restart.

## Protection and reversal

- Compute actual-entry SL with the saved range distance. Check current position
  identity and broker stop. Use protect() and NativeExecutorSession for the request;
  retain TP. Commit dispatch before send and confirmed stop only after VERIFIED.
- Generate reversal intent from the confirmed entry/state and approved volume
  schedule, independently of the other range. A protective SL update must not be
  counted as a full-SL close. Never arm slot 25.
- Quote-driven support targets use BUY BID and SELL ASK, .25 -> entry minus/plus
  .25, .50 -> entry. A proposal is not confirmed protection. After a verified support
  update, keep the opposite trigger at its old level until reentry actually fills.
- Close classification needs authoritative stop-at-close evidence, including the
  EA evidence path already present. A deal's realized price/profit or today's SL is
  insufficient. Missing evidence leaves the event unresolved, not guessed.
- On support close, plan same-direction reentry from the confirmed support stop
  level using the saved reentry distance. On reentry fill retire the old opposite
  broker ticket before submitting the replacement at the new initial SL.

## Required failure/restart tests before wiring to an API

Full and partial entry; partial cancel raced by late fill; immediate fill-and-close;
slippage with changed SL; account switch during reads; missing historical entry;
duplicate/conflicting deal; position ticket change; protection write/ACK loss;
support update raced by close; manual close with opposite left pending; both ranges
active; reversal 23/24 boundary; restart at every durable transition; journal and
strategy checkpoint commit failures. Unknown outcomes never authorize a resend.

Existing Controller/MockPipeline guards remain unchanged. Their test-only event
ledger is a reference for policy and cases, not proof that native history ingestion
or native protection/reversal is complete.
