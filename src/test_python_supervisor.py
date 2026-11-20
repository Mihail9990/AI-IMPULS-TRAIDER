from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest

from python_supervisor import BoundTerminal, legacy_locks


class Terminal:
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2
    TRADE_ACTION_REMOVE=8
    ORDER_TYPE_BUY_STOP=4
    ORDER_TYPE_SELL_STOP=5

    def __init__(self, data):
        self.info=NS(connected=True,data_path=str(data),trade_allowed=True,tradeapi_disabled=False)
        self.account=NS(login=123,server="Test",margin_mode=2,trade_allowed=True,trade_expert=True)
        self.sent=[]

    def terminal_info(self):
        return self.info

    def account_info(self):
        return self.account

    def orders_get(self, *, ticket):
        return (NS(symbol="XAUUSD.s"),) if ticket==7 else ()

    def order_send(self, req):
        self.sent.append(req)
        return "sent"

    def order_check(self, req):
        return "checked"


class SupervisorTest(unittest.TestCase):
    def test_remove_checks_exact_live_strategy_stop_again_at_send(self):
        with TemporaryDirectory() as directory:
            raw = Terminal(Path(directory))
            api = BoundTerminal(raw, dict(login=123, server="Test", data_dir=directory, symbol="XAUUSD.s", magic=17))
            stop = NS(ticket=73, type=raw.ORDER_TYPE_BUY_STOP, symbol="XAUUSD.s", magic=17)
            rows = [stop]
            raw.orders_get = lambda *, ticket: tuple(rows)
            request = dict(action=raw.TRADE_ACTION_REMOVE, order=73, symbol="XAUUSD.s")
            self.assertEqual(api.order_check(request), "checked")
            for name, old in vars(stop).copy().items():
                with self.subTest(field=name):
                    setattr(stop, name, "EURUSD" if name == "symbol" else 0)
                    with self.assertRaises(RuntimeError):
                        api.order_send(request)
                    setattr(stop, name, old)
            rows.clear()
            with self.assertRaises(RuntimeError):
                api.order_send(request)
            self.assertEqual(raw.sent, [])
            stop.type = raw.ORDER_TYPE_SELL_STOP
            rows.append(stop)
            self.assertEqual(api.order_send(dict(action=raw.TRADE_ACTION_REMOVE, order=73)), "sent")

    def test_revalidates_binding_and_permission_immediately_at_broker_boundary(self):
        with TemporaryDirectory() as directory:
            data=Path(directory)
            raw=Terminal(data)
            api=BoundTerminal(raw,dict(login=123,server="Test",data_dir=str(data),symbol="XAUUSD.s"))
            req=dict(action=1,symbol="XAUUSD.s")
            self.assertEqual(api.order_check(req),"checked")
            for target,name,bad in [(raw.account,"login",124),(raw.account,"server","Other"),
                                    (raw.account,"margin_mode",0),(raw.info,"tradeapi_disabled",True),
                                    (raw.info,"connected",False),(raw.info,"data_path",str(data.parent)),
                                    (raw.account,"trade_expert",False),(raw.info,"trade_allowed",False)]:
                with self.subTest(name=name):
                    good=getattr(target,name)
                    setattr(target,name,bad)
                    with self.assertRaises(RuntimeError):api.order_send(req)
                    setattr(target,name,good)
            with self.assertRaises(RuntimeError): api.order_send(dict(action=1,symbol="EURUSD"))
            with self.assertRaises(RuntimeError): api.order_send(dict(action=1,order=8))
            self.assertEqual(raw.sent,[])
            self.assertEqual(api.order_send(dict(action=1,order=7)),"sent")

    def test_legacy_writer_exclusion(self):
        with TemporaryDirectory() as directory:
            with legacy_locks(Path(directory)):
                with self.assertRaises(RuntimeError):
                    with legacy_locks(Path(directory)):
                        self.fail("A second owner took the MT5 writer locks")


if __name__ == "__main__":
    unittest.main()

