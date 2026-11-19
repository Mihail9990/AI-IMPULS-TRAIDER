# Offline EA channel proposal v1 (NOT wired or ready for live use)

Exactly one autonomous EA owns all broker writes and strategy state for one account,
server, symbol and magic-number scope. Python never imports MT5 or sends orders on
this route. Python publishes explicit confirmed control intents and reads facts.
No Python fallback trading path exists. The EA continues its strategy independently
of the web server, browser, and Python availability.

## Deployment prerequisite, not automated here

Use a private local NTFS directory beneath the chosen terminal's Files sandbox.
Installer must establish and verify Windows ACLs: designated Python service identity
can publish commands; EA terminal identity can read them; only EA can write receipts,
state and durable ledger. Other users/services must not have write access. The EA
and Python may share an OS identity only if that weaker trust boundary is accepted.
No network share, synced folder, junction/reparse point or public Common Files root.
No credentials in protocol files. UUID scope is an opaque installer-established
mapping to account/server/symbol/magic, not authority by itself. Python helper does
not validate Windows ACLs or every ancestor reparse point: deployment is blocked
until installer implements this. Filesystem permissions, not hashes, authenticate.

## Encoding and files

UTF-8 JSON, no BOM, no duplicate keys, no NaN/Infinity, max64KiB. Exact version1.
IDs canonical lower-case UUID. Python writer serializes ASCII escaped JSON with keys
sorted and separators comma/colon. `content_sha256` is SHA256 of these bytes before
adding the content_sha256 field. This checksum binds receipts, not a signature.
MQL parser/hash equivalence still needs an interoperability fixture test.

`commands/<command_id>.json`: immutable create-only publication. Write temp, flush,
then atomic same-volume link/create; final name must never overwrite another ID.
Temporary `.pending-*` files ignored. Crash before final publication has no effect;
crash after final publication is uncertain: inspect by ID, NEVER auto-resubmit.
On Windows antivirus/sharing/NTFS failure, fail closed, not copy-overwrite fallback.
Python uses hard-link publish; actual NTFS/File sandbox compatibility not tested.
EA publishes `state.json` and receipts via same-directory temp+atomic replacement.

Command fields (all required, no extras): version, scope_id, boot_id, context_id,
generation_id, command_id, created_at UTC, operation, payload, content_sha256.
Time is diagnostic, not a command expiry or permission check. Validate live context,
boot and strategy generation. Numeric strategy prices will be decimal strings with
symbol digit validation when configure_ranges is specified. That command currently
fails closed in Python, not guessed or partially implemented.

State contains version, scope_id, boot_id, context_id, generation_id, written_at UTC seconds,
mode (`ready`, `manual_wait`, `manual_cancel`, `reconciling`, `unknown`), plus EA facts.
Python needs heartbeat <=5 seconds and never treats it as execution confirmation.
EA generates new boot_id on every restart and new context_id on scope/range/settings
changes. This invalidates all old intents. Same-account restart still invalidates.

Receipt echoes version/scope/boot/context/command_id/content_sha256 and has status
`accepted`, `applied`, `rejected`, or `unknown`, reason code and optional broker fact
references. `accepted` means durable admission, NOT broker success. `applied` only
after broker facts are reconciled. Partial or timeout outcome is `unknown`, never
"failed so retry". Reader refuses mismatched identity. No receipt -> status-only.

## EA execution and crash contract (EA NOT implemented here)

EA must acquire exclusive scope writer ownership before enabling broker writes.
Second EA/chart/terminal must fail closed. Durable ledger stores command identity,
canonical digest, lifecycle and broker operation intent BEFORE any effect. Same ID
and digest returns stored receipt; same ID different digest rejected. EA consumes
one command at a time, validates exact schema, scope, boot, context, expiry and
current state, then journals admission. Broker intent also needs unique operation
identity and broker reconciliation; file idempotency alone cannot make broker
execution exactly-once. Unknown broker result forbids replay/new competing action.

On restart: new boot, reconciling/status-only; reconcile positions/orders/deals with
durable journal. Old unconsumed files are rejected as old_boot, never replayed.
If intent might have reached broker, report unknown until facts settle; do not
invent success from file presence. Corrupt/missing ledger forces manual recovery.
Ledger is never auto-pruned until durable completion plus retention decision.

## Explicit local commands

`manual_wait {}`: latch new entries/reentries OFF, preserve open positions and their
existing protections; does not close positions, cancel orders or strip SL. It is
not "no further fills": already resting broker orders may still execute, and EA
must continue reconciling/protecting them. The user must be told this distinction.

`manual_cancel {}`: cancel manual waiting before position adoption only. Does not touch live
ranges, open positions, or pending broker orders. If adoption already happened, do not
silently reinterpret it as a close/cancel trade command.

`configure_ranges`: RESERVED; unavailable until strategy owner/EA author agree exact
fields and transitions. Manual state cannot resume automatically from heartbeat,
refresh, Python restart, login or an old command. Fresh explicit range configuration
and confirmation required, subject to current state policy.

## Remaining release gates

Agree MQL wire/parser, manual command semantics and configure payload; implement EA
single-writer durable journal + restart reconciliation; actual Windows ACL/reparse
validation and atomic IO tests; broker-free simulated crash/fill/cancel race tests;
independent safety review. Nothing in this folder changes production or authorizes
live trading. This is a transport prototype, not an EA or completed trading system.

## Two-EA separation (latest architecture)

Trading EA is still the ONLY broker writer. Verification EA is read-only and owns
a DIFFERENT verified-snapshot journal, not this commands/receipts directory. Its
observations go directly to trading EA and independently to Python dashboard.
Verification EA cannot acknowledge command execution on behalf of trading EA, issue
commands, retry trades or cancel orders. Trading EA compares observation scope,
sequence, boot, context, broker position ticket AND position identifier. Stale or
conflicting observations mean status-only/reconciliation, never inferred execution.
Separate ACLs/journals and message types prevent dashboard snapshots being mistaken
for authoritative applied receipts. Verification-channel schema is outside this
prototype and must be defined independently.

Build agent's offline manual-adoption planner requires login/server/symbol and exact
position ticket+identifier, snapshot SL/TP/entry/volume. This transport does NOT yet
expose adoption. Future adoption command must carry those immutable snapshot facts;
SL and TP must remain exactly unchanged, reverse trigger exactly existing SL,
off-grid rejected rather than normalized. No conversion or interpretation here.
