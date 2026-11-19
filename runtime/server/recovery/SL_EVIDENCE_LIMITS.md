# SL evidence boundary

`closing_order_evidence.resolve_closing_order` is a read-only diagnostic resolver.
It reads `history_orders_get(ticket=deal.order)` and checks account/server before
and after the call, deal/order/position/symbol/side/SL-reason and completed state.
Missing, partial, ambiguous or mismatched evidence raises `EvidenceUnavailable`.
Even a matched order returns `STOP_LEVEL_UNRESOLVED` and no classification/level.
This helper does not authorize any counter transition, recovery order or live send.
It is deliberately not connected to the simulation pipeline's execution path.

## Why this is unresolved

MQL5 exposes `HistoryDealGetDouble(ticket, DEAL_SL)`, documented as the position
SL at the time of an exit. The Python `history_deals_get` TradeDeal shape does not
provide that field. The pipeline's optional `deal.sl` check is therefore only a
check of enriched/test data, NOT a production solution.

Historical `TradeOrder.sl` is documented as an order SL value; it is not a documented
guarantee of the closed position's SL. `price_open`, `price_current`, deal execution
price, comments, profit and locally remembered stop stage cannot replace that
missing evidence. In particular, slippage and external/manual stop modifications
make inference unsafe.

A production implementation needs a verified EA bridge exporting the authoritative
`DEAL_SL` with account/server, deal ticket, closing order and position identifier,
or another demonstrably authoritative historical source. The bridge must preserve
identity, missing-data/error distinction and reconnect/replay semantics. Until that
exists, ambiguous exits must halt the affected range without increasing its full-SL
counter or creating a successor order. The exact `.25` equal-level policy remains
a separate unresolved strategy issue.

Sources checked 2026-11-18:
- https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties
- https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties
- https://www.mql5.com/en/docs/python_metatrader5/mt5historyordersget_py
- https://www.mql5.com/en/docs/python_metatrader5/mt5historydealsget_py

No broker/account connection or trading was performed. Verification is API-double
unit testing only, not Windows/MT5 integration testing.
