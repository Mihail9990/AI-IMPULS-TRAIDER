import dataclasses
from decimal import Decimal
from pathlib import Path
import re
import unittest

from record import InventoryScope, decode_snapshot

SCOPE = InventoryScope("1"*32, "2"*32, "3"*32, 0, 123, "Test-МТ5", "XAUUSD.s", 745, 314159)
P = "P\t9007199254740993\t772\t0\tBUY\t0.0100000000000000\t3010.0000000000000000\t3009.7000000000000000\t3011.0000000000000000"
O = "O\t773\t745\t0\t5\t1\t0.0200000000000000\t0.0100000000000000\t3009.7000000000000000\t3010.4000000000000000\t0.0000000000000000\t0.0000000000000000"

def wire(positions=(), orders=(), *, coverage="complete", reason="ok", scope=SCOPE):
    head = ["RECOVERY_INVENTORY_V1", *scope._wire(), "17", coverage, reason, str(len(positions)), str(len(orders))]
    return "\n".join(("\t".join(head), *positions, *orders, "END", "")).encode()

class InventoryTest(unittest.TestCase):
    def test_request_binds_all_except_chart(self):
        self.assertEqual(SCOPE.request_bytes().decode().split("\t"),
                         ["RECOVERY_INVENTORY_REQUEST_V1", *SCOPE._wire()[:-1], "END\n"])

    def test_mixed_magic_and_partial_order_retained(self):
        fact = decode_snapshot(wire([P], [O]), SCOPE)
        self.assertTrue(fact.complete)
        self.assertEqual(fact.positions[0].identifier, 9007199254740993)
        self.assertEqual(fact.positions[0].magic, 0)
        self.assertEqual(fact.positions[0].sl, Decimal("3009.70"))
        self.assertEqual(fact.orders[0].mt5_type, 5)
        self.assertEqual(fact.orders[0].volume_current, Decimal("0.01"))
        self.assertEqual(fact.orders[0].open_price, fact.positions[0].sl)

    def test_unavailable_is_not_empty_complete(self):
        fact = decode_snapshot(wire(coverage="unavailable", reason="limit"), SCOPE)
        self.assertFalse(fact.complete)
        self.assertTrue(decode_snapshot(wire(), SCOPE).complete)
        with self.assertRaises(ValueError):
            decode_snapshot(wire([P], coverage="unavailable", reason="read"), SCOPE)

    def test_scope_mismatch_rejected(self):
        for field, changed in (("nonce", "4"*32), ("executor_epoch", "4"*32),
                               ("verifier_epoch", "4"*32), ("login", 1), ("server", "old"),
                               ("symbol", "old"), ("magic", 16), ("generation", 1), ("verifier_chart", 1)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                decode_snapshot(wire(scope=dataclasses.replace(SCOPE, **{field: changed})), SCOPE)

    def test_invalid_utf8_truncation_extra_data_limits(self):
        for raw in (b"\xff"+wire(), wire()[:-1], wire()+b"END\n", b"x"*65537,
                    wire().replace(b"\t0\t0\n", b"\t129\t0\n")):
            with self.subTest(raw_len=len(raw)), self.assertRaises(ValueError):
                decode_snapshot(raw, SCOPE)

    def test_count_and_invalid_decimal(self):
        for raw in (wire([P]).replace(b"\t1\t0\n", b"\t0\t0\n"),
                    wire([P.replace("0.0100000000000000", "NaN")]),
                    wire([P.replace("0.0100000000000000", "-0.0100000000000000")]),
                    wire([P.replace("0.0100000000000000", "0.0000000000000000")]),
                    wire(orders=[O.replace("0.0200000000000000", "0.0010000000000000")])):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_snapshot(raw, SCOPE)

    def test_duplicate_id_ticket_and_wrong_record_kind(self):
        for raw in (wire([P, P]), wire([P, P.replace("9007199254740993", "1")]),
                    wire([P, P.replace("772", "777")]),
                    wire(orders=[O, O]), wire([O], [P])):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_snapshot(raw, SCOPE)

    def test_no_broker_mutation_api_in_verifier_source(self):
        source = Path(__file__).with_name("RecoveryVerifier.mq5").read_text()
        self.assertIsNone(re.search(
            r"\b(?:OrderSend(?:Async)?|CTrade|TRADE_ACTION_\w+|PositionClose\w*|OrderDelete)\b",
            source,
        ))

if __name__ == "__main__":
    unittest.main()
