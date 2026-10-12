"""Capture ONE existing closed native chain, without trading or persistent credentials."""
import argparse
import json
from pathlib import Path
import sys

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tradfi.mt5_adapter import MT5Broker
from tradfi.native_fixture import capture_chain


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--order-ticket',required=True,type=int)
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    broker=None
    try:
        config=json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
        config['enable_trading']=False
        broker=MT5Broker(config); broker.connect()
        result=capture_chain(broker,args.order_ticket)
        payload=json.dumps(result,indent=2,allow_nan=False)
        with Path(args.output).open('x',encoding='utf-8') as handle: handle.write(payload+'\n')
        print('Existing chain fixture exported; IDs/prices/sizes/dates transformed. No trading actions.')
    except Exception:
        parser.exit(2,'Fixture capture failed; check existing order, complete chain, config and fresh quote. No raw details emitted.\n')
    finally:
        if broker is not None: broker.close()

if __name__=='__main__': main()
