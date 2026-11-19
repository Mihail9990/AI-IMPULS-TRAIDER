# Native execution boundary and owner session (not finished RECOVERY deployment)

Unlike the older mock-only transport, `NativeMT5Transport` contains a real
`api.order_send` path. It takes an already initialized API, immutable account /
server / symbol / magic binding, and an active runtime owner guard. There is no
filter distinguishing demo versus real accounts; hedging remains a strategy
requirement. It does not initialize, login, change accounts or toggle permissions.

`NativeExecutorSession` supplies process ownership (same `.owner.lock` convention
as the test owner), persistent Executor/outbox, immutable database scope and
serialized explicit operations. Entering the context performs evidence-only
reconciliation; it does not send or replay strategy actions. Only explicit
`execute(key, request, expected)` can send. Each request uses native validation,
one-use preflight, re-check/revalidation immediately before send, and post-send
account/owner checks. Unknown/partial/lost results persist as UNKNOWN and block
new actions; restart does not resubmit them. An order check is not a fill or a
readiness guarantee. Final broker races cannot be eliminated by local checks.

## Verified here

Injected API doubles only: native mapping without a test-double marker, no reuse
of approval, ownership/account/policy changes, timeout/partial result handling,
durable no-retry across restart, competing owner exclusion, persistent scope
binding and resource shutdown. No real MT5 API was initialized or called.

## Not yet connected / not verified

- This owner/session is NOT wired into Controller/Coordinator/ScopedMockService,
  Android API or EA evidence lifecycle. Those continue to use their test paths.
- It is not an unattended trading runner or a deployable strategy service.
- Native snapshot history remains bounded to 30 days; unresolved histories stay
  blocked. Operator-assisted UNKNOWN resolution is unfinished.
- EA evidence file transport/provenance and actual MQL5 compilation are unfinished.
- Windows file locking, MT5 broker responses, fills, precision and phone↔VPS flow
  have not been tested in their target environment.

Do not expose this low-level execute method directly as a public HTTP endpoint.
Do not route old mock code through it by removing mock safeguards. A deliberate,
reviewed strategy owner integration and Windows verification are still required.
