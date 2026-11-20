import dataclasses
from decimal import Decimal
from pathlib import Path
import re
import unittest

from history_record import HistoryScope, decode_history
from test_record import SCOPE

S = HistoryScope(SCOPE, 9007199254740997, 9007199254740999)
ORDER = f"H\t{S.anchor_order}\t745\t5\t4\t10000\t0.0200000000000000\t0.0000000000000000\t3009.5000000000000000\t3009.5000000000000000\t0.0000000000000000"
DEAL = f"D\t9007199254740886\t{S.anchor_order}\t745\tSELL\t1\t4\t10001\t0.0100000000000000\t3009.3000000000000000\t3009.5000000000000000\t0.0000000000000000"
SECOND = DEAL.replace("9007199254740886", "9007199254740887").replace("10001", "10002")

def wire(orders=(), deals=(), *, coverage="observed", reason="ok", scope=S):
    header = "\t".join(("RECOVERY_HISTORY_V1", *scope.inventory_scope._wire(), "73",
                        str(scope.position_identifier), str(scope.anchor_order),
                        coverage, reason, str(len(orders)), str(len(deals))))
    return "\n".join((header, *orders, *deals, "END", "")).encode()

class HistoryTest(unittest.TestCase):
    def test_request_exact_position_and_order(self):
        raw = S.request_bytes().decode().split("\t")
        self.assertEqual(raw, ["RECOVERY_HISTORY_REQUEST_V1", *SCOPE._wire()[:-1],
                               str(S.position_identifier), str(S.anchor_order), "END\n"])

    def test_partial_deals_keep_distinct_tickets_observed_sl_and_actual_price(self):
        hist = decode_history(wire([ORDER], [DEAL, SECOND]), S)
        self.assertTrue(hist.observed)
        self.assertEqual(hist.orders[0].ticket, S.anchor_order)
        self.assertNotEqual(*[d.ticket for d in hist.deals])
        self.assertEqual(sum(d.volume for d in hist.deals), Decimal("0.02"))
        self.assertEqual(hist.deals[0].sl, Decimal("3009.50"))
        self.assertEqual(hist.deals[0].price, Decimal("3009.30"))

    def test_deal_only_anchor_and_unknown(self):
        self.assertTrue(decode_history(wire(deals=[DEAL]), S).observed)
        unknown = decode_history(wire(coverage="unavailable", reason="anchor_missing"), S)
        self.assertFalse(unknown.observed)
        self.assertFalse(unknown.deals)

    def test_wrong_position_anchor_or_chart_is_rejected(self):
        for scope in (dataclasses.replace(S, position_identifier=5),
                      dataclasses.replace(S, anchor_order=5),
                      dataclasses.replace(S, inventory_scope=dataclasses.replace(SCOPE, verifier_chart=5))):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                decode_history(wire([ORDER], [DEAL], scope=scope), S)

    def test_non_evidence_and_structural_invalidity(self):
        for raw in (wire(), wire(deals=[DEAL.replace(str(S.anchor_order), "8")]),
                    wire([ORDER, ORDER]), wire(deals=[DEAL, DEAL]),
                    wire(deals=[DEAL.replace("\t10001\t", "\t0\t")]),
                    wire(deals=[DEAL], coverage="unavailable", reason="read"),
                    wire(deals=[DEAL])[:-1], wire(deals=[DEAL])+b"\xff", b"0"*65537):
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                decode_history(raw, S)

    def test_static_no_writes_or_history_list_reset(self):
        source = Path(__file__).with_name("RecoveryHistory.mqh").read_text()
        self.assertIn("HistorySelectByPosition(position)", source)
        self.assertIsNone(re.search(
            r"\b(?:OrderSend(?:Async)?|CTrade|TRADE_ACTION_\w+|PositionClose\w*|OrderDelete|HistoryOrderSelect|HistoryDealSelect)\b",
            source,
        ))


if __name__ == "__main__":
    unittest.main()
