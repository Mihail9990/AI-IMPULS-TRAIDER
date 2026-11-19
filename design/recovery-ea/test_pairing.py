import unittest
from uuid import uuid4

from pairing import pairing_status


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.scope = {field: str(uuid4()) for field in ("terminal_id", "context_id", "challenge")}
        self.trader = dict(self.scope, role="trader", chart_id=10, instance_id=str(uuid4()),
                           can_submit_orders=True, healthy=True)
        self.verifier = dict(self.scope, role="verifier", chart_id=20, instance_id=str(uuid4()),
                             can_submit_orders=False, healthy=True)

    def check(self, value, *reports):
        self.assertEqual(pairing_status(reports, **self.scope), value)

    def test_two_charts_and_order_independent(self):
        self.check("ready", self.trader, self.verifier)
        self.check("ready", self.verifier, self.trader)

    def test_same_chart_or_same_instance_not_ready(self):
        self.check("same_chart", self.trader, dict(self.verifier, chart_id=10))
        self.check("same_instance", self.trader,
                   dict(self.verifier, instance_id=self.trader["instance_id"]))

    def test_removal_and_old_report_do_not_establish_readiness(self):
        self.check("missing_peer", self.trader)
        self.check("missing_peer", self.trader, dict(self.verifier, challenge=str(uuid4())))
        self.check("ready", dict(self.verifier, challenge=str(uuid4())), self.verifier, self.trader)

    def test_reject_duplicate_or_an_unhealthy_peer(self):
        self.check("duplicate_role", self.trader, self.verifier, self.verifier)
        self.check("unhealthy_peer", self.trader, dict(self.verifier, healthy=1))

    def test_scope_authority_and_exact_types(self):
        for field in ("terminal_id", "context_id"):
            self.check("scope_mismatch", self.trader, dict(self.verifier, **{field: str(uuid4())}))
        self.check("wrong_role_permission", self.trader, dict(self.verifier, can_submit_orders=True))
        self.check("wrong_role_permission", self.trader, dict(self.verifier, can_submit_orders=0))
        self.check("invalid_chart", self.trader, dict(self.verifier, chart_id=True))

    def test_probe_id_validation(self):
        self.assertEqual(pairing_status([], **dict(self.scope, challenge="")), "invalid_probe")


if __name__ == "__main__":
    unittest.main()
