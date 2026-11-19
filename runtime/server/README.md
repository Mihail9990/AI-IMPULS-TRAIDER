# MT5 Widget VPS bridge (prototype)

Separate from the trading bot. No real orders were executed in development. Default is read-only (`WIDGET_LIVE=0`). Windows MT5 integration remains untested.

## Configuration

Install Python requirements in a virtual environment. Set `WIDGET_TOKEN` (random secret, >=32 characters), `MT5_ACCOUNT` (login number), `MT5_SERVER` (exact broker server), `MT5_TERMINAL_PATH` (full terminal64.exe path). Start `python run.py`. MT5 must already be signed into the intended account. The program never logs in or changes accounts. It binds **127.0.0.1:8766 only**. Publish solely behind a properly configured HTTPS reverse proxy with trusted TLS; do not forward 8766 to the Internet. No reverse proxy, firewall, credentials, deployment or live enablement is supplied or performed.

`WIDGET_LIVE=1` enables broker SL/TP updates. Set this only intentionally after native testing. No market/limit/stop entry orders or explicit closing operations are exposed.

All routes require `Authorization: Bearer <token>`. Do not put token in URL. Keep token/config private. No permissive CORS or API docs endpoint.

GET `/health`: status and mode.
GET `/api/positions`: account login, server, mode and positions (ticket, symbol, side, volume, price_open, sl, tp, profit).
POST `/api/positions/{ticket}/protection`: `request_id` UUID, `expected_symbol`, `expected_entry`, `expected_side` (BUY/SELL), `expected_volume`, `expected_identifier` (from position response), optional `tp_distance`, `sl_distance`. Numeric distances are raw price units (0.25 = price change 0.25), not symbol points. Omitted/null means preserve, zero is invalid. BUY TP=entry+d/SL=entry-d; SELL inverse. Values round to valid symbol tick size.

Response status: `verified` only after successful MT5 response and matching reread; `rejected` for refusal/read-only; `unknown` for incomplete evidence. HTTP 409 means changed/closed position or account mismatch; refresh the UI. HTTP 422 is invalid levels. Never display success for an unknown result. Refresh actual position; do not blindly repeat an unknown command. A persistent SQLite journal prevents resending the same UUID even after restart. Do not delete this file to retry. Rejected responses also remain idempotent; a deliberate new command requires a new UUID.

Position selection and account are checked again on the server. No automatic selection of the latest position; every update names its ticket. Calls are serialized within this one process. **MT5 does not provide atomic compare-and-swap for protection levels. Avoid simultaneously modifying the same position from another EA/terminal/manual UI.** The service rereads immediately before sending to preserve the latest untouched side, but cannot eliminate the final broker race.

Tests: `python -m unittest discover -s tests -v`. API test additionally needs `httpx`. Tests use only a fake MT5 object and an in-memory journal. Never run multiple bridge processes against the same terminal.

## Backend selection

Set `WIDGET_BACKEND=python` (default) for direct official Python MT5 API, or `WIDGET_BACKEND=ea` for EA bridge. Selection is local to VPS and requires a service restart, not a phone command. In EA mode set `WIDGET_EA_DIRECTORY` to the absolute MT5 Common Files `MT5ProtectionWidget` directory. `MT5_TERMINAL_PATH` is only required in Python mode. See the EA README for setup. Both modes remain read-only until deliberately enabled; EA also requires its own EnableChanges input.
