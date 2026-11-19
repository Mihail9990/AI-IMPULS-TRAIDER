"""Explicit user-run Windows read-only connection check. Never run by tests natively."""
import argparse
import json
import os
import sys


def main(argv=None):
    parser=argparse.ArgumentParser(description='READ-ONLY MT5 diagnostic; never enables RECOVERY trading.')
    parser.add_argument('--connect-read-only',action='store_true',required=True,
                        help='Connect to explicit terminal (may launch it and its existing EAs).')
    parser.add_argument('--terminal',required=True)
    parser.add_argument('--account',required=True,type=int)
    parser.add_argument('--server',required=True)
    parser.add_argument('--symbol',required=True)
    parser.add_argument('--history-days',type=int,default=1,choices=range(1,31))
    args=parser.parse_args(argv)
    if sys.platform!='win32':
        print('Windows MT5 environment required.',file=sys.stderr);return 2
    if not os.path.isabs(args.terminal) or not os.path.isfile(args.terminal):
        print('An existing absolute terminal executable path is required.',file=sys.stderr);return 2
    try:
        import MetaTrader5 as mt5
        from recovery.vps_diagnostics import diagnose
        result=diagnose(mt5,terminal_path=args.terminal,account=args.account,
                        server=args.server,symbol=args.symbol,history_days=args.history_days)
    except Exception as exc:
        # Do not dump API objects, account identifiers, credentials or paths.
        print(json.dumps({'mode':'read_only_diagnostic','live_trading':False,
            'safe_to_trade':False,'status':'failed','error_type':type(exc).__name__}),file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
