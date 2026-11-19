# Native runtime (Windows, not broker-validated)

`run_recovery_native.py` is a native dispatcher, NOT the simulation server.
Starting it may resume protection and pending-order management for persisted ranges.
It accepts real or demo accounts; it does not log in, switch accounts, enable
AlgoTrading, select symbols, compile/install the EA, or open initial ranges by itself.
Account/server/symbol/magic must match the existing terminal and durable database.

Use `windows/Start-Native.ps1` after completing [Windows setup](NATIVE_SETUP_RU.md).
It requests the app token using hidden input; do not put the token in command-line
arguments or PowerShell history. In a separate PowerShell window, replace every
placeholder and leave the existing Caddy window running:

```powershell
cd C:\Recovery\server\windows
.\Start-Native.ps1 -RunNative -Terminal 'C:\MT5\terminal64.exe' -Account 123456 -BrokerServer 'EXACT-SERVER' -Symbol 'EXACT-SYMBOL' -Magic 123 -Database 'C:\RecoveryData\state.db'
```

Keep the database AND its journals between restarts. Restrict the state directory
and environment secret to the service identity. Do not copy a running database,
run multiple workers, run a second strategy process, or reuse its magic elsewhere.
The process holds the existing owner file lock and binds 127.0.0.1:8767 only.
Caddy terminates HTTPS; never expose the Python port directly. Bearer authentication
is mandatory. EA evidence mailbox must already be installed and writable for the
same terminal/account; missing evidence can pause dispatch rather than guess.

The timer starts only during ASGI lifespan. It serializes with API mutations via
the executor lock. Startup reconciliation is read-only: no interrupted HTTP
command is replayed, no staged range is automatically submitted. Uncertain
checkpoints require explicit investigation. There is no automatic terminal
reconnect. Shutdown waits for any in-flight tick before closing SQLite and the
terminal API; it intentionally does not cancel broker stops or positions. A hung
native broker call can therefore delay shutdown. Inspect broker state before any
restart, especially after forced termination.

This entrypoint and its lifecycle are tested only with terminal doubles on Linux.
Windows initialization, MQL compilation, real broker execution, EA mailbox ACLs,
service installation, TLS deployment and phone-to-native integration are NOT
verified by these tests. The native API state format requires the native UI
projection; an old simulation-only APK is not proof of end-to-end compatibility.

For HTTPS by bare public IP, do not assume Caddy's default certificate is publicly
trusted: its default IP local CA is not automatically trusted by Android. Public
IP certificates need an appropriate ACME issuer/profile (for example the supported
Let's Encrypt short-lived profile), and must be verified on the deployed version.
Do not disable TLS verification. Validate issuance, automatic renewal and Android
trust on the actual VPS; this document does not claim those checks have passed.

The CLI reserves the actual exclusive loopback listening socket before terminal
initialization and passes that socket to Uvicorn. An occupied port aborts before
MT5 is initialized or the scheduler starts. Default port 8767 matches
`Caddyfile.example`; if overridden, update the reverse proxy destination too.
