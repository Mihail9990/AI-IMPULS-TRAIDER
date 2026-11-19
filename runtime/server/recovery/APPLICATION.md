# Connected test application (not live deployment)

`MockApplication` joins the existing Controller, durable Executor/outbox,
MockPipeline, ScopedMockService owner lifecycle, Coordinator and authenticated HTTP API.
It does not join the separate IsolatedRuntime, native preflight, or EA evidence
reader: those remain separate unfinished production components.

Pass an explicitly marked MT5 **test double**, scope and a 32+ character token.
Use its `app` through ASGI lifespan (`with TestClient(assembly.app) as client:` in
tests). Lifespan acquires the existing process owner lock and performs startup
reconciliation; exit releases ownership and closes both SQLite connections.
The assembly is single-use; create another instance for a process restart.
`database` selects the same file for controller and outbox durability. Do not
move just one of those tables to a separate persistence mechanism.

The local driver calls `assembly.step(bid, ask)`. `run_steps` consumes a bounded
number (default 1000, maximum 10000) of explicit quotes; excess quotes are not
processed. There is no quote HTTP endpoint, background task, terminal import,
login, MT5 initialization or automatic trade activation. API commands and ticks
use the same controller lock. A caller must retain and clean up an instance
that is constructed but never started (`assembly.close()`).

End-to-end tests now exercise HTTP range configuration → fake fill history →
verified support SL → fake support exit → pending reentry, durable restart and
API request deduplication, competing ownership, authentication and lifecycle.
These are fake broker tests, not evidence of Windows or broker readiness.

## Still required for real MT5

- Real service lifecycle, native execution transport and authenticated deployment
  API, never simply deleting the mock locks.
- Implement explicit quarantine recovery; legacy MockService remains global.
- Integrate authoritative stop-at-close evidence (EA DEAL_SL), normalize to exact
  symbol tick grid, preserve account/deal/position binding, and compile/test EA.
- Resolve UNKNOWN/partial/out-of-order and restart histories without blindly
  retrying sends; long gaps/late fees still need reconciliation.
- Windows MT5 terminal permissions, account mode/symbol settings, reconnect and
  failure drills, phone↔VPS integration, TLS/auth and operational shutdown.

No real account was connected and no real orders were sent by these tests.

## Scoped test cycle

MockApplication now uses ScopedMockService (subclass of the owner lifecycle), not
an IsolatedRuntime adapter. All work remains under controller.lock. One history
batch is checked for account stability/timestamps and routed by exact mapped
entry order or position identifier, including IN→OUT within a single batch.
A local range error is durably quarantined; subsequent ranges continue. A global
history/account error disables startup_ready until explicit startup reconciliation.
An error saving quarantine closes service ownership. No failed range automatically
retries after restart. Quarantine alone may allow a healthy range to run after a
clean startup snapshot; unknown intents and any snapshot mismatch still stop all.

The global history cursor remains unchanged while any range is blocked. Healthy
deals are deduplicated durably. This does NOT solve long gaps/30-day history
windows or authorize unblocking a range. Tests show failure, restart persistence,
healthy-range protection and explicit global reconciliation recovery.

**Partial entry:** after verifying protection, scoped mode cancels the exact
remaining entry ticket and re-polls history before accepting actual filled volume.
The re-poll routes only the affected range but validates account/global history
identities. Additional IN/OUT race evidence is retained and prevents unsafe
promotion. Unconfirmed cancellation quarantines that range; it never claims the
remaining order was removed. Subsequent opposite volume uses the schedule, not
the partial actual size. HTTP+tick integration tests cover success, timeout and
IN/OUT race with a second range still present.

Startup report distinguishes `safe_to_resume` (all ranges) from `scoped_ready`
(healthy ranges may resume), and lists `quarantined_ranges`. Quarantined ranges
remain disabled until an explicit verified recovery workflow exists.

## Strict EA-format evidence test mode

Construct `MockApplication(..., require_stop_evidence=True)` to require a bound
EA-format record for every SL exit before classification. Default legacy mock
mode is retained for old fixtures and is NOT production-safe evidence handling.
Once strict mode starts on a database, restarting that database in legacy mode
is rejected. `service.prepare_stop_evidence_requests()` reads test-broker history
and durably records request IDs/bindings without ingesting exits. The local test
driver supplies bytes through `pipeline.stop_evidence.supply(deal_id, payload)`.
There is no HTTP upload endpoint or trusted filesystem bridge.

Strict ingestion revalidates identity, side, volume, timestamp and exact stop
against the persisted strategy SL; missing/changed evidence quarantines the range
without incrementing full-SL count. Supplied evidence is immutable and duplicate
exit history is deduplicated. Supplying evidence after quarantine does not unblock
it. A restart with an unprocessed missing broker position still fails startup
reconciliation even if evidence is present; this is not a recovery bypass.

This integrates the parser into TEST classification, not the MQL5 EA runtime.
EA/Windows has not been compiled/run here. File provenance/authentication, Windows verification of exact
symbol tick-grid normalization and an explicit verified recovery
workflow remain blockers. Unrecognized binary tails remain rejected. Locally supplied bytes are trusted test fixtures only.

## Exact binary64 export canonicalization

`price_evidence.canonical_exported_price` now permits an exact symbol-grid price
or the exact fixed-16-decimal IEEE binary64 representation of that grid price.
For example `3999.3000000000001819` resolves to grid level `3999.3` at tick `.01`.
There is no absolute/relative tolerance and no arbitrary rounding of input prices.
A nearby but nonmatching decimal remains rejected. Tick grids indistinguishable
at binary64 precision and nonfinite/unsupported magnitudes are rejected.

Strict SL ingestion uses current symbol tick metadata and still requires the
canonical level to equal the strategy's confirmed SL. This is a functional core
step, covered by unit and full-SL lifecycle tests, not a claim of tested MetaEditor
formatting. If actual EA formatting differs from Python's fixed-16 rendering at
an edge, evidence fails closed; Windows-produced fixtures remain required.

## Initial SL minimum (updated user rule)

Initial SL distance must be **at least 0.60**, inclusive. API validation, engine
range creation, fill/quote/close lifecycle, maintenance/startup and pending planner
reject smaller initial distances. The minimum does **not** apply to the separate
support reentry distance (which must only remain positive), or to support SL
movement of an already valid position.

Persisted settings/ranges below 0.60 are NOT rounded up or migrated automatically.
Service startup rejects them before trading/strategy mutation; existing position
SLs and saved data remain unchanged. The local simulation engine permits an
explicit valid replacement when no position is open. With old open positions,
manual reconciliation and an explicit safe configuration transition are required;
no silent widening of SL or cancellation is performed.
