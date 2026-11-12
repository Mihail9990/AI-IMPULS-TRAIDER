# Read-only analysis diagnostics

`GET /api/analysis?timeframe=M1` (also M15, M30, H1) keeps the existing authentication
and freshness contract. The additive `diagnostics` object reports:

- `proof_status`, `proof_reason`, `proof_age_seconds`: current parent proof.
- `expected_closed_through`, `actual_closed_through`: UTC close boundaries.
  `MISSING_LAST_CLOSED_PERIOD` means native history lacks the expected close;
  `PROOF_STALE` means the feed proof expired. Worker proof expiry during a
  calculation or before publication retains its own reason even when the parent
  now has a fresh proof. Missing history is reported only with actual < expected.
  No candles are synthesized.
- `reason`: calculation, cache reuse, history backoff or proof expiration stage.
  Backoff retains the safe `error_code`, `stage` and `last_failure_reason` across
  repeated waits; it does not erase the underlying failure.
- `history_ms`, `calculation_ms`, `total_ms`: worker monotonic durations for
  that attempt. Total is elapsed across the worker's four-timeframe batch.
  Zero on a cache hit means no native read/calculation in that attempt.
- `generated_at`: original analysis generation UTC (feed-derived), preserved on
  cache reuse. `received_at`: parent wall-clock UTC when the latest batch arrived.
  `cache_age_seconds`: monotonic age since that receipt, not candle freshness.
- `retry_in_seconds`: worker history backoff at calculation time;
  `supervisor_retry_in_seconds`: current parent restart backoff.
- `supervisor_state`, `failures`, `ipc_ms`, `error_code`: current supervisor state,
  bounded restart count, completed round-trip time, and literal safe error code.
  Watchdog startup/response timeout, child exit and IPC reader failure differ.

The existing authenticated LIVE log export includes four cached analysis metadata
rows on its existing state-change/60-second heartbeat schedule. Analytics changes
are excluded from the log-change signature, so polling cannot turn export into a
per-second tick log. Export is historical, not a live response; proof metadata in
those rows describes calculation time. Separate `current_proof_status`,
`current_proof_reason`, `current_proof_age_seconds` and `cache_age_seconds`
describe the parent proof and receipt age at capture time. Existing queue, record, rotation and
export byte limits remain in effect. Metadata access never starts the worker or
calls MT5. The owner loop performs only fixed-size cached projection.

Only allowlisted error codes are retained. No exception strings, credentials,
account/feed identities, terminal paths, raw stdout/stderr, native responses or
bar arrays are added to logs. Existing framed subprocess output and stderr
suppression remain unchanged. No new trading API, retry, threshold or permission
behavior is introduced. Browser diagnostics are provided separately by the UI.

Offline verification: `python -m unittest discover -s tests -p 'test*analysis*.py'`
and `python -m unittest discover -s tests -p '*logs*.py'`. These do not initialize
MT5. Live Windows/native-library behavior requires separate operator testing.

## Browser stages and download

The browser keeps a memory-only journal for this tab, capped at 120 records and
64 KiB per export. Repeated identical outcomes are coalesced for up to 60 seconds;
counts, last observation and maximum request duration remain visible. A reload
clears the journal. Device UTC labels are explicitly unverified and do not prove
market freshness.

`STATE` records distinguish `FETCH`, `PARSE`, `VALIDATE`, `RECONCILE`, `RENDER`
and fallback `RECOVERY_RENDER` failures. Facts include HTTP status, allowlisted
exception class, stage timings, feed status/sequence/age, read-only mode and the
unchanged freshness gate. Render failures identify a fixed UI component such as
`QUOTE`, `TICKETS`, `V3` or `CHART`; no DOM content is captured. Success is recorded
as `COMPLETE`. A display error after HTTP 200/FRESH no longer masquerades as a
transport outage, but still sets the same fail-closed gate to false.

Analysis records distinguish the two cards and main-chart overlays, their
request/parse/validation/render stages, timeframe, returned analysis status,
original generation/close times and bar count. The browser journal also retains
allowlisted backend reason/error codes, proof/worker status, expected close and
history/calculation/IPC timings from each visible response. This preserves a
transient analytical warning seen by the UI even between server heartbeats. Intentional cancellation after
navigation or a timeframe switch is recorded as `CANCELLED`, not a network error.
No diagnostic field is consulted by an order or freshness decision.

Settings → Logs → Show adds browser diagnostics to the first server-log page.
Download appends them to the existing sanitized LIVE/runtime file. The server
section remains capped at 1 MiB; the combined file is at most 1 MiB + 64 KiB +
two separator bytes. Select **Диагностика браузера · эта вкладка** to show or
download only local diagnostics without making a server request, including when
the server-log request fails. Download files are local browser Blobs; nothing is
posted to a collector or another service.

The browser allowlist excludes exception messages/stacks, URLs, cookies,
authorization headers, bodies, account/feed IDs, paths, email addresses, storage
contents and arbitrary extra fields. Diagnostics do not read localStorage or
persist to it. Failures inside the diagnostic collector cannot change the gate.

Offline frontend checks:

```
node tools/test_ui_diagnostics.js
node tools/test_main_analysis_unit.js
```

These use actual source with DOM/HTTP doubles. They are not a live-browser or
Windows/MT5 verification. A local Chromium attempt was blocked before page load
by the environment's socket restriction; no screenshot or visual pass is claimed.


## Cross-interpreter Windows clock

The previous release ran LIVE on Python 3.13 and analysis on Python 3.12. On Windows,
`time.monotonic()` changed from GetTickCount64 in 3.12 to QueryPerformanceCounter
in 3.13. Absolute readings from those clock domains cannot be subtracted across
IPC. A small negative delta previously produced `TIME_UNVERIFIED` with no bars;
the next parent proof could already be `READY`, so the two statuses in diagnostics
could differ without a worker crash.

Analysis now uses its own `analysis_clock()` backed by `time.perf_counter()` for
proof creation, dispatch, calculation, final publication and all local analysis
deadlines. This is system-wide QPC on both historical Windows interpreters. The
current release uses Python 3.13 for both processes, in separate environments;
the shared clock and regression coverage remain necessary transport guarantees. The
same owner proof object is used for validation and dispatch, and each response
uses one captured proof and a subsequent clock sample. The original maximum age,
negative elapsed rejection, native identity, closed-bar boundaries and retry
limits remain unchanged. Trading clocks/gates and frontend rendering are not
modified. Unknown/expired data still fail closed; no last-good chart is disguised
as current. `received_at` remains an untrusted display wall-clock timestamp.

An offline operator probe, using already-installed interpreters and no MT5:

```
py -3.13 tools/check_analysis_clock.py --worker-python .venv-analysis-313/Scripts/python.exe
```

Use the actual analysis environment path for a managed release. The probe reports
Python versions, clock implementations and whether the child sample falls inside
the parent round trip. It never installs software, reads LIVE configuration,
opens a terminal, sends orders, or changes the system clock. A Linux pass does
not verify Windows or the live installation. Fully stop/restart the service after
an update so both processes use the same release code.

`MISSING_LAST_CLOSED_PERIOD` is separate: a native M1 history ending one minute
before the expected close remains `STALE`. Missing bars are never synthesized.

Sources: [Python 3.13 clock change](https://docs.python.org/3.13/whatsnew/3.13.html#time)
and [Python 3.12 perf_counter](https://docs.python.org/3.12/library/time.html#time.perf_counter).
