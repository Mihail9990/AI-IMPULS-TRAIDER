"""User-run native Windows entrypoint. Starting this process enables strategy dispatch.

Not a diagnostic: persisted active ranges can resume management on startup.
Never import this module to initialize a terminal. No login/account switching.
"""
import argparse
import os
import sys
import socket


def reserve_loopback_listener(port):
    """Reserve the actual listening socket before terminal initialization."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description='Native RECOVERY dispatcher: can send real orders on the bound account.')
    p.add_argument('--run-native', action='store_true', required=True,
                   help='Explicitly start native strategy, including persisted active ranges.')
    p.add_argument('--terminal', required=True)
    p.add_argument('--account', required=True, type=int)
    p.add_argument('--server', required=True)
    p.add_argument('--symbol', required=True)
    p.add_argument('--magic', required=True, type=int)
    p.add_argument('--database', required=True)
    p.add_argument('--port', type=int, default=8767)
    p.add_argument('--interval', type=float, default=0.25)
    args = p.parse_args(argv)
    if sys.platform != 'win32':
        print('Windows MT5 environment required.', file=sys.stderr)
        return 2
    if not os.path.isabs(args.terminal) or not os.path.isfile(args.terminal):
        print('Existing absolute terminal executable path required.', file=sys.stderr)
        return 2
    if not os.path.isabs(args.database) or not 1 <= args.port <= 65535:
        print('Absolute persistent database path and valid port required.', file=sys.stderr)
        return 2
    token = os.environ.get('RECOVERY_API_TOKEN', '')
    if not token.isascii() or len(token) < 32:
        print('Set RECOVERY_API_TOKEN to at least 32 ASCII characters.', file=sys.stderr)
        return 2
    runtime = None
    mt5 = None
    listener = None
    try:
        from recovery.native_preflight import TerminalBinding
        from recovery.native_runtime import NativeRuntime, NativeScheduler
        import uvicorn
        # Validate all local parameters before initialize can launch terminal/EAs.
        binding = TerminalBinding(args.account, args.server, args.symbol, frozenset({args.magic}))
        NativeScheduler(None, interval=args.interval)
        listener = reserve_loopback_listener(args.port)
        import MetaTrader5 as mt5
        if not mt5.initialize(path=args.terminal):
            raise RuntimeError('Terminal initialization failed')
        runtime = NativeRuntime(mt5, binding, database=args.database, magic=args.magic,
                                token=token, interval=args.interval)
        app = runtime.open()
        # Caddy must terminate TLS. No reload, multiple workers or public bind.
        config = uvicorn.Config(app, host='127.0.0.1', port=args.port, workers=1, access_log=False)
        uvicorn.Server(config).run(sockets=[listener])
        return 0
    except Exception as exc:
        print('Native runtime failed: ' + type(exc).__name__, file=sys.stderr)
        return 1
    finally:
        if runtime is not None:
            runtime.close()
        if mt5 is not None:
            mt5.shutdown()
        if listener is not None:
            listener.close()


if __name__ == '__main__':
    raise SystemExit(main())
