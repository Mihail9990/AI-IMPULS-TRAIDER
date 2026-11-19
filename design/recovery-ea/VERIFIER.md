# Two-EA RECOVERY verifier design

Status: read-only source audit/design, not an implemented or compiled verifier. Source baseline: RECOVERY WEB 0.1.7 under `release-web-fetch-fix`. No trading, terminal attachment, deployment or publication performed.

## Responsibilities and trust

- Executor EA is the sole strategy owner and sole broker writer. It owns durable commands, range generations, entry genealogy, counters and reconciliation. Python must not retain an active trading scheduler or an order-send fallback.
- Verifier EA independently reads terminal orders, positions and history. It reports observations directly to executor and separately to Python. It never creates/modifies/cancels orders, modifies position SL/TP, closes positions or authorizes trading by itself.
- Both EAs use the same MT5 terminal/broker data. This is independent validation code, not independent market truth or isolation against a compromised terminal. Mailbox ACLs do not authenticate one component against another process under the same Windows identity.
- Python serves control/UI and displays verified results. Loss of Python must not break the EA-to-EA verification path. Missing/ambiguous verifier evidence never means an order was not sent and never authorizes a resend.

## Exactly what exists and is reusable

`runtime/server/recovery/mql5/RecoveryEvidenceBridge.mq5` v0.10:

- A read-only `OnTimer` mailbox server with one-second timer; no trade API calls.
- Checks connected terminal and expected account/server before and after historical reads; expected symbol is checked against the selected deal. The EA itself does not enforce REAL mode, dedicated magic, terminal path or mailbox ACL. Those checks currently belong to Python preflight.
- Accepts bounded `RECOVERY_REQUEST_V1` records from `MQL5/Files/RECOVERY/<32-lowercase-hex>.request`. Filename and nonce must match.
- Requires one exact historical SL exit: deal, order, position identifier, symbol, BUY/SELL exit side, broker millisecond timestamp and volume. Only `DEAL_ENTRY_OUT`, `DEAL_REASON_SL`, positive finite volume and `DEAL_SL` are exported.
- Emits `RECOVERY_DEAL_SL_V1` with exact binding, fixed 16-place numeric fields and END marker. Writes temp, flushes, closes and moves to response. Existing comments explicitly do NOT assume atomic rename; consumer must reject incomplete data.
- Persistent directory enumeration cursor, at most 128 visited filenames per timer callback, including invalid/completed records. Retain this fairness behavior, not an unbounded scan or repeatedly restarting from the first request.

`RecoveryDealEvidence.mq5` is the one-explicit-deal variant using input parameters and a `.tsv` output. It stops its timer after export. Reuse its field-reading/validation logic as a fixture/reference; do not attach both old exporters as two additional production authorities.

`local_evidence_bridge.py` and `ea_stop_evidence.py` provide reusable protocol rules and test vectors: exact immutable request binding, bounded UTF-8 records, LF/CRLF validation, END/field-count validation, nonce collision rejection, strict decimal comparison and no inferred identifiers. Python request publication uses flush/fsync and hard-link publication without overwriting a nonce; an MQL implementation must independently establish safe publication behavior rather than assume identical primitives.

## Existing freshness / ACL / handshake limits

`native_evidence_preflight.verify_native_prerequisites` verifies exact REAL account/server/terminal/data folder, reads a qualifying recent historical SL, publishes a fresh nonce, consumes a matching EA reply, rereads the broker deal and rechecks identity/ACL. No qualifying history is a startup blocker in this legacy check.

The hardened ACL policy inventories mailbox, ancestors and existing files; rejects reparse/null-DACL/untrusted writers and unsafe future child permissions. Windows identity trust includes the current SID, SYSTEM and Administrators, with narrowly handled filesystem-root cases. Reuse the policy and tests, not a blanket permissive ACL repair. Do not change permissions as a side effect of verifier startup.

A fresh nonce means a response belongs to a new local request. It does not make the historical deal new, establish current quote age, or prove a broker event causally occurred after a user pressed “wait”. Replaying identical historical evidence is idempotent and must not increment counters again. There is currently NO full live-inventory snapshot/epoch handshake in these two EAs.

## Extension contract (new version, retain V1 compatibility)

Define separate typed requests, not an overloaded SL record:

1. `HELLO`: protocol/build compatibility, executor launch epoch, verifier launch epoch, account/server/symbol and scoped ownership. New nonce per handshake. No trade side effects.
2. `INVENTORY`: bounded complete scoped positions/orders, exact position identifiers AND current tickets, order tickets/types, direction, volume, open price, SL, TP, and known manual-root ownership. Declare coverage/completeness explicitly. Include visibility of unmapped relevant exposure; never silently omit it or turn failure into empty arrays.
3. `EFFECT`: command UUID + generation + immutable canonical intent digest + concrete expected effect. Verifier checks fresh broker state/history; it must not simply repeat an executor-provided success flag. Examples: exact pending order; exact SL/TP on identified position; cancellation with terminal order evidence; close with complete exit-deal coverage.
4. `DEAL_SL`: preserve the existing exact historical proof with explicit verifier epoch in the new envelope.

Each response carries schema, account/server/symbol, range generation, executor/verifier epochs, request nonce, verifier observation sequence, coverage, result (`verified`, `pending`, `mismatch`, `unavailable`), concrete evidence and bounded machine-readable reasons. Command-effect responses also bind UUID and intent digest. Broker identifiers stay integers/decimal strings without lossy conversion.

Both consumers read the SAME immutable response identity/digest. Executor receives it without Python forwarding, e.g. through the terminal-local mailbox. Notifications between charts may wake the reader, but durable records remain authoritative. Separate acknowledgement channels prevent one consumer deleting a response before the other reads it. No cleanup while an executor checkpoint remains unresolved.

Executor must revalidate ownership and affected objects immediately before a new write. A report can become outdated after publication. Inventory collection is not an atomic broker snapshot: use repeat/read-consistency checks, detect mutation and report `pending`/`unavailable`; do not claim atomicity. Restart epochs reject old live-state reports, while historical deal evidence may be deliberately revalidated and reused.

## Latest manual-position rule

For “Ожидать ручную позицию”, verifier must independently identify the eligible root and its actual broker SL/TP. Executor preserves existing nonzero SL and TP; opposite trigger must equal the confirmed existing SL exactly. Only absent SL permits configured-distance protection, retaining TP. Store the adopted initial stop explicitly for later full-SL/support classification; never infer it solely from configured distance. Any change between root observation and effect verification is a conflict, not permission to normalize SL or move the trigger.

The current manual prototype's existing-SL branch eventually rewrites a nonmatching SL and modifies the reverse (`manual_cycle_store.py` around 388–467). That branch contradicts the latest instruction and must not be ported.

## Acceptance tests before enabling the pair

- Static/runtime double: verifier emits ZERO broker-write calls on every handler, timer, error and restart path. Python likewise cannot submit strategy orders in EA mode.
- Exact account/server/symbol/path/magic/manual-root binding; account switch during read; second verifier instance; duplicate executor; wrong generation/build/epoch.
- New nonce + old response, same nonce changed payload, response substitution, replayed deal, truncated/oversized/malformed UTF-8, duplicate records, symlink/reparse and ACL failures. No success/no-empty defaults.
- Empty but complete inventory versus unavailable inventory; hidden/unmapped owned exposure; duplicate/changed position identifiers; live inventory changing mid-scan; partial fills and reordered/duplicate events.
- Pending create, SLTP modify, cancel and close each verified from concrete broker evidence. A send receipt alone is not proof. Reject price/volume/SL/TP mismatches and preserve uncertain checkpoints without resend.
- Full SL versus support SL using actual DEAL_SL and confirmed stage history; ambiguous/equal levels stay unresolved. Multi-deal partial close increments the full-close count once, only after full classified closure.
- Manual close leaves existing reverse pending; support reentry preserves old opposite until successor protection is verified; independent ranges never cancel or classify each other's objects.
- Manual root with existing SL+TP: no initial SLTP write, reverse exactly at SL. Missing SL: configured SL only, TP unchanged. Concurrent external SL/TP edit and invalid STOP distance reject without offset/market fallback.
- Crash before write, after broker send, before receipt persistence, during verifier report and before either consumer ACK. After restart reconcile actual evidence; never blindly recreate a possibly-sent command.
- Stop/restart verifier or Python independently: executor sees explicit evidence unavailability, Python cannot forge readiness, and exact confirmed broker-side protection remains intact. Test recovery once verifier returns without replay.
- Executor/Python both receive the same report; losing one consumer does not starve or erase the other's report. Sustained invalid-mailbox flood still allows valid requests to progress within a documented bound.
- Compile both EAs and run terminal test scenarios separately. Python doubles/protocol tests cannot establish MQL compilation, terminal event ordering, file move semantics or live readiness.
