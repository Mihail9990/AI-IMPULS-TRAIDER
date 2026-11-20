# Position-linked history observation, V1

Read-only extension to the inventory verifier, not a complete command
reconciler. No wall-clock age checks and no assumption that a missing history
row means that a command was never submitted. Native MT5 tests remain required.

Separate create-only request `<nonce>.hist` under the same terminal-local
`RECOVERY_VERIFY` directory; one UTF-8 tab-separated line (LF or CRLF):

```text
RECOVERY_HISTORY_REQUEST_V1 nonce executor_epoch verifier_epoch generation login server symbol magic position_identifier anchor_order END
```

Both IDs are canonical positive decimal integers, not the mutable position
ticket and not a guessed order. The verifier uses
[HistorySelectByPosition](https://www.mql5.com/en/docs/trading/historyselectbyposition)
and checks the symbol and position identifier independently on every record.
It does not reset the selected lists by selecting individual orders/deals.
Reports preserve all returned position-linked orders/deals, including multiple
deal tickets from one order. No filtering by magic, as manual and broker
actions can have a different magic from the strategy.

Each probe is bounded by 128 history orders and 128 history deals for that
position; scan twice and require identical evidence. Changes between the two
samples, invalid properties, wrong scope, missing exact anchor, and over-limit
lists do not produce a positive report. Two identical reads do not prove the
server's full history is available or that no data changed before/between reads.
Current exposure and position identity must also be checked separately.

Create-only response `<nonce>.history`, same wire rules and chart binding as
inventory:

```text
RECOVERY_HISTORY_V1 nonce executor_epoch verifier_epoch generation login server symbol magic verifier_chart sequence position_identifier anchor_order coverage reason order_count deal_count
H order_ticket magic order_type order_state order_time_done_msc initial_volume remaining_volume order_price order_SL order_TP
D deal_ticket order_ticket magic BUY_or_SELL entry_type reason deal_time_msc volume actual_price DEAL_SL DEAL_TP
END
```

`observed/ok` requires the exact anchor in an H row or as the order ticket of a
D row. `unavailable/{anchor_missing,read,changed,limit,session}` provides no
rows. A disconnected or re-scoped terminal may not return anything. Missing,
changed or unavailable evidence does not authorize any retry. Raw MT5 reason,
entry, order type and state are preserved; timestamps are descriptive broker
fields, not requirements to place new orders. Order price, execution price and
[DEAL_SL](https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties)
are distinct fields, including after slippage or partial fills. This reader
does not classify or count stop losses, establish final close volume, or infer
effect on its own.

Orders not linked to the position (e.g. canceled unfilled orders with position
ID 0) may be absent. Close-by and nonstandard deal types require separately
validated semantics; unsupported properties fail closed. This channel is not a
generic search by time, price or symbol and cannot discover unknown identities.
Each request type has a separate persistent cursor, visits up to 32 filenames
and attempts at most one full probe per timer event.

```console
cd design/recovery-ea/verifier
python -m unittest -v test_record.py test_reader_compat.py test_history_record.py
```

These offline tests exercise a Python history parser with synthetic split
fills, slippage, identity mismatch, duplicates, missing anchors and malformed
records. They cannot establish MetaEditor compilation, MT5 file behavior or
history visibility at a broker.
