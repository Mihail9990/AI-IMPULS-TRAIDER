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
and state. The EA holds the current intent journal only in RAM. Other users/services must not have write access. The EA
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

`sessions/<boot_id>/commands/<command_id>.json`: immutable create-only publication. Write temp, flush,
then atomic same-volume link/create; final name must never overwrite another ID.
Temporary `.pending-*` files ignored. Crash before final publication has no effect;
lost response after publication is uncertain: inspect by ID in the same EA session,
never automatically resubmit. A new EA session does not read this file.
On Windows antivirus/sharing/NTFS failure, fail closed, not copy-overwrite fallback.
Python uses hard-link publish; actual NTFS/File sandbox compatibility not tested.
EA publishes root `state.json` and `sessions/<boot_id>/receipts/<command_id>.json`
via same-directory temp+atomic replacement. Create fresh private session folders
before publishing the new state; never scan or import a prior queue.

Command fields (all required, no extras): version, scope_id, boot_id, context_id,
generation_id, command_id, created_at UTC, operation, payload, content_sha256.
Time is diagnostic, not a command expiry or permission check. Validate live context,
boot and strategy generation. Numeric strategy prices will be decimal strings with
symbol digit validation when configure_ranges is specified. That command currently
fails closed in Python, not guessed or partially implemented.

State contains version, scope_id, boot_id, context_id, generation_id, written_at UTC seconds,
mode (`ready`, `manual_wait`, `manual_cancel`, `unknown`), plus EA facts.
No heartbeat-age gate. A fresh EA gets a new boot_id and starts from empty RAM.
Context changes invalidate the prior scope. The working chart has a fixed
period; see [EA session lifecycle](../execution/SESSION_LIFECYCLE_RU.md).

Receipt echoes version/scope/boot/context/generation/command_id/content_sha256 and has status
`accepted`, `applied`, `rejected`, or `unknown`, reason code and optional broker fact
references. `accepted` means admission within the current session, not broker success. `applied` only
after broker facts are reconciled. Partial or timeout outcome is `unknown`, never
"failed so retry". Reader refuses mismatched identity. No receipt -> status-only.

## EA execution contract (not wired)
 
Only one EA may write to the chosen account/server/symbol/magic scope. In the
current session it binds each validated command ID to its immutable payload and
reserves the matching broker intent before sending. Same ID and same payload
read current status; conflicting payload or off-scope observations do not get
sent. A timeout does not authorize resending the same action in that session.
Exact broker observation, not the transport receipt, determines the effect.

After a true unload and fresh load, do not reconstruct trading state, reload an
intent journal, replay an old command, or cancel or modify broker orders as
part of startup. Already accepted orders and SL remain with the broker. Panel
settings are stored separately from the temporary trading state.
 
## Explicit local commands

`manual_wait {}`: explicitly wait to adopt one eligible manual position outside
ranges, only when no RECOVERY cycle is active. Does not itself close a position,
cancel a broker order or remove SL. Selection and adoption live in the EA.

`manual_cancel {}`: cancel manual waiting before position adoption only. Does not touch live
ranges, open positions, or pending broker orders. If adoption already happened, do not
silently reinterpret it as a close/cancel trade command.

`configure_ranges`: RESERVED; unavailable until strategy owner/EA author agree exact
fields and transitions. Old commands do not arm a new EA session.

## Remaining release gates

Agree MQL wire/parser, manual command semantics and configure payload; implement EA
single-writer session integration; actual Windows ACL/reparse validation and atomic
IO tests; broker-free lifecycle, fill and cancel race tests;
independent safety review. Nothing in this folder changes production or authorizes
live trading. This is a transport prototype, not an EA or completed trading system.

## Three-EA separation

Trading EA is still the ONLY broker writer. Verification EA is read-only and owns
a DIFFERENT verified-snapshot journal, not this commands/receipts directory. Its
observations go directly to trading EA and independently to Python dashboard.
Verification EA cannot acknowledge command execution on behalf of trading EA, issue
commands, retry trades or cancel orders. Trading EA compares observation scope,
sequence, boot, context, broker position ticket AND position identifier. Conflicting or wrong-context observations never prove execution.
The price watcher is also read-only and only reports support-threshold observations.
Separate message types prevent dashboard snapshots or watcher signals being
mistaken for authoritative applied receipts. Verification-channel schema is outside this
prototype and must be defined independently.

The offline manual-adoption planner requires login/server/symbol and exact
position ticket+identifier, snapshot SL/TP/entry/volume. This transport does NOT yet
expose adoption. The EA must bind those immutable snapshot facts when adopting a position;
SL and TP must remain exactly unchanged, reverse trigger exactly existing SL,
off-grid rejected rather than normalized. No conversion or interpretation here.
