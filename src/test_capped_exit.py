"""Offline RECOVERY stop classification against native and independent history."""

import json
import unittest

import test_python_strategy as fixtures
from recovery_book import Broker, Wait


class CappedExitTest(unittest.TestCase):
    def classify(self, side, initial, cap, levels, **leg_fields):
        api = fixtures.Native()
        broker = Broker(api, fixtures.Verifier(api), 17, "test", "X", 99)
        entry = 100.0
        opening = api.pending(side, entry, initial / 100, volume=len(levels) / 100)
        position = api.fill(opening.ticket)
        broker.refresh()
        leg = dict(broker.verified_positions[position.ticket], anchor=opening.ticket, initial=initial, **leg_fields)
        if cap is not None:
            leg["risk_cap_ticks"] = cap
        # Individual deal prices are deliberately different from the reported
        # SL. Multiple deals may complete the same position's exit.
        for level in levels:
            position.sl = level / 100
            position.volume = 0.01
            api.p[position.ticket] = position
            api.closep(position, api.DEAL_REASON_SL, price=level / 100 + 0.12)
        broker.refresh()
        recorded = []
        proof = broker.closed(leg, lambda _, records: recorded.extend(records))
        self.assertEqual([row["sl_ticks"] for row in recorded], levels)
        return proof["cause"]

    def test_full_stop_before_or_after_tightening(self):
        for side, initial, cap in [(0, 9859, 9920), (1, 10141, 10080)]:
            for levels in [[initial], [cap], [initial, cap]]:
                with self.subTest(side=side, levels=levels):
                    self.assertEqual(self.classify(side, initial, cap, levels), "full")
            with self.subTest(side=side, legacy=True):
                self.assertEqual(self.classify(side, initial, None, [initial]), "full")

    def test_tighter_original_stop_is_preserved(self):
        for side, initial, nominal in [(0, 9960, 9920), (1, 10040, 10080)]:
            with self.subTest(side=side):
                self.assertEqual(self.classify(side, initial, nominal, [initial]), "full")
                with self.assertRaisesRegex(Wait, "unconfirmed_stop_level"):
                    self.classify(side, initial, nominal, [nominal])

    def test_support_needs_breakeven_evidence_and_only_entry_stops(self):
        self.assertEqual(self.classify(1, 10141, 10080, [10000], be_ack=True), "support")
        self.assertEqual(self.classify(1, 10141, 10080, [10080], be_ack=True), "full")
        for levels, fields in [([10000], {}), ([10000, 10080], {"be_proven": True}), ([10079], {})]:
            with self.subTest(levels=levels):
                with self.assertRaisesRegex(Wait, "unconfirmed_stop_level"):
                    self.classify(1, 10141, 10080, levels, **fields)

    def test_existing_breakeven_or_profitable_stop_is_not_a_full_exit(self):
        for side, initial in [(0, 10001), (1, 9999), (1, 10000)]:
            with self.subTest(side=side, initial=initial):
                with self.assertRaisesRegex(Wait, "unconfirmed_stop_level"):
                    self.classify(side, initial, None, [initial])

    def test_slipped_successor_corrected_exit_advances_once_after_late_history(self):
        case = fixtures.StrategyTest()
        case.setUp()
        self.addCleanup(case.tearDown)
        case.arm()
        parent = case.api.market(0, 3526.43)
        case.tick(4)
        sell_stop, = case.owned()
        self.assertEqual((sell_stop.price_open, sell_stop.sl), (3525.63, 3526.43))
        case.api.closep(parent, case.api.DEAL_REASON_SL, 3525.59)
        sold = case.api.fill(sell_stop.ticket, at=3525.02)
        case.tick(4)
        leg = case.e.s["leg"]
        self.assertEqual((leg["ticket"], leg["rung"], leg["initial"]), (sold.ticket, 2, 352643))
        self.assertEqual(leg["risk_cap_ticks"], 352582)
        sold.sl = 3525.82
        buy_stop, = case.owned()
        case.api.closep(sold, case.api.DEAL_REASON_SL, 3525.91)
        bought = case.api.fill(buy_stop.ticket, at=buy_stop.price_open)
        case.helper.complete = False
        case.tick(2)
        self.assertEqual(case.e.s["leg"]["rung_parent"], sold.identifier)
        self.assertFalse(case.owned())
        case.helper.complete = True
        case.tick(4)
        self.assertEqual((case.e.s["leg"]["ticket"], case.e.s["leg"]["rung"]), (bought.ticket, 3))
        self.assertFalse(case.e.s["detached"])
        self.assertEqual(len(case.owned()), 1)
        evidence, = case.e.db.execute(
            "SELECT evidence FROM confirmed_exits WHERE position=?", (str(sold.identifier),)
        ).fetchone()
        exit_row, = json.loads(evidence)["exits"]
        self.assertEqual((exit_row["sl_ticks"], exit_row["price_ticks"]), (352582, 352591))


if __name__ == "__main__":
    unittest.main()
