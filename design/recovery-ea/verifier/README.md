# RECOVERY verifier: read-only inventory prototype

This source is not part of the installer. It reads a bounded projection of the
terminal's current positions and orders, not broker history or command effects.
It does not send, modify, cancel or close anything. No terminal or VPS operation
is part of the offline tests.

## Transport

The peer puts a create-only UTF-8 file in that terminal's `MQL5/Files/RECOVERY_VERIFY`,
named `<nonce>.inv`. The request is one tab-separated line (LF or CRLF):

```text
RECOVERY_INVENTORY_REQUEST_V1  nonce  executor_epoch  verifier_epoch  generation  login  server  symbol  magic  END
```

Replace the spaces above with single tabs. Nonce and launch epochs are 32
lowercase hexadecimal digits. The caller generates a new nonce for each
observation, never reusing one. For this prototype the verifier is explicitly
configured with expected peer and verifier launch tokens, account, server,
symbol and strategy magic; the chart must match the symbol. The terminal-local
folder is not shared across terminals. Install/ACL verification and automatic
discovery of the expected peer chart and new launch epochs are future work.
These files carry account identity and positions; keep them local, do not commit
real requests or snapshots or expose the folder to a web server.

The verifier checks the loaded request against those settings, reads the scoped
inventory twice, and writes a UTF-8 LF-only `<nonce>.snapshot`:

```text
RECOVERY_INVENTORY_V1  nonce  executor_epoch  verifier_epoch  generation  login  server  symbol  magic  verifier_chart  sequence  coverage  reason  position_count  order_count
P  identifier  ticket  magic  BUY_or_SELL  volume  open_price  SL  TP
O  ticket  magic  position_identifier  MT5_order_type  MT5_order_state  initial_volume  remaining_volume  trigger_price  SL  TP  stop_limit
END
```

Possible coverage/reason combinations are `complete/ok` (including a legitimate
empty snapshot), and `unavailable/{read,changed,limit,session}`. If disconnected
or connected to a different account, a response may not be published at all.
An absent or invalid response is not an empty inventory. The search cursor
persists across timer events, visits at most 32 filenames per event, and performs
at most one inventory probe. Inventory is limited to 128 positions and 128 orders
for the whole account; larger lists are unavailable, never truncated and
reported complete.

A complete record covers all visible positions and orders **of the configured
symbol** regardless of magic, not missing history, terminal data, or orders of
other symbols. Do not infer strategy ownership from this scope. IDs and magic
use integer strings; prices and volumes are decimal encodings of MT5 double
properties, not price-tick integers. The consumer must separately map observed
values to the instrument's actual tick/volume grid before using them for
comparisons. The change check compares both row sets in terminal enumeration
order, conservatively reporting unavailable if order or observed data changed.
Two equal samples cannot exclude a change in between or immediately afterwards.

Response publication is create-only, using a chart-specific temporary file,
flush/close, and a move with no overwrite flag ([MetaQuotes FileMove
reference](https://www.mql5.com/en/docs/files/filemove)). Neither atomicity nor a
completed broker effect is inferred. Readers must bind the response to the
full requested scope, expected chart, new nonce and launch tokens and reject
malformed or incomplete records. The immutable response can be read directly
from the terminal by the trading EA and separately by Python without forwarding,
but an MQL consumer and the runtime wiring are **not** implemented here. Neither
consumer should delete a response while there is unresolved work.

## Offline verification

```console
cd design/recovery-ea/verifier
python -m unittest -v test_record.py
```

The tests check the strict Python parser using synthetic files and statically
check that the verifier source has no order mutation calls. These are not
MetaEditor compilation or broker/terminal tests. Before release: compile in
MetaEditor, run on a separate demo chart, test wrong identity, shutdown/restart,
multiple chart instances, concurrent inventory changes and Windows file
visibility, then integrate effect and historical evidence and the MQL reader.
