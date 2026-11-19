# Experimental local EA evidence mailbox

Python `LocalEvidenceBridge(directory)` publishes an immutable exact ExitBinding request
as `<nonce>.request`; `collect(binding)` returns a parsed Decimal DEAL_SL only after
an exact matching response. A nonce is generated and persisted by the native ledger,
not this bridge. Missing, truncated, foreign-account and mismatched records fail closed.
Identical historical evidence is idempotent, never a second close event.

EA `mql5/RecoveryEvidenceBridge.mq5` reads the terminal-local `MQL5/Files/RECOVERY`
folder every second. Configure ExpectedLogin, ExpectedServer, ExpectedSymbol. It has
no trade calls. It verifies current account before and after reading the exact exit,
requires DEAL_ENTRY_OUT/DEAL_REASON_SL and checks all identifiers, side, volume and time.
The existing V1 response parser accepts CRLF/LF and exact binary64 output. It does not
make a classification or change counters. Requests are UTF-8 with exactly one CRLF
terminator, matching FILE_TXT FileReadString's documented line delimiter. Existing LF-only
requests from the experimental prior version are not silently overwritten: archive those
records and create a fresh persisted request nonce before retrying.

At most 128 filenames are visited per timer, including invalid/completed/unresolved ones.
The search handle and next filename persist across events, so a finite stable directory
is traversed fairly instead of restarting at its first 128 unresolved requests. At EOF
(or search error) the handle closes; the next event starts a new traversal. New files
not seen in an in-progress traversal are picked up on a subsequent pass. Disconnect and
EA deinitialization close the cursor. Continuous directory mutation and search errors do
not have a bounded-latency guarantee. No automatic cleanup is provided; archive old
records operationally so a large completed backlog does not increase latency.

The runtime MUST derive the directory from the selected terminal's data_path, verify
account/server before and after publish/collect, and bind evidence to its durable
historical broker snapshot. Do not expose mailbox operations through an unauthenticated
HTTP route or accept a stop price submitted by a phone as evidence. Restrict directory
write permissions to the terminal/service identities before deployment.

This is NOT cryptographic authentication. A local process able to write the directory
can forge records or race path checks. Symlink rejection and nonce identity are checks
against accidental path/replay mistakes, not a hostile writer security boundary. The
caller must handle replay and restart in durable storage. Hard-link publication requires
local filesystem support (e.g. NTFS); unsupported filesystems fail rather than fall back
to partially visible writes. EA rename atomicity is not assumed; parser rejects partial
records. Keep one EA instance per mailbox and selected account.

Test coverage includes Python local-file tests and a C++ stub harness executing the
unchanged EA cursor functions (not a native MQL5 test). MQL5 syntax/compilation,
FileReadString EOF/UTF-8 behavior, Windows hard links/ACLs, live terminal formatting,
actual broker data and end-to-end requests are UNVERIFIED. It is not a completed live
runner and must not be represented as proven source authenticity.

References: [FileReadString](https://www.mql5.com/en/docs/files/filereadstring),
[FileFindNext](https://www.mql5.com/en/docs/files/filefindnext).
