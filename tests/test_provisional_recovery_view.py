import unittest
from copy import deepcopy
from tradfi.web_control import provisional_recovery_view


class ProvisionalRecoveryViewTests(unittest.TestCase):
    def test_estimate_separate_from_confirmed_and_no_ids(self):
        cycle = dict(recovery='2', realized='-2', recovery_effective='4.25',
                     provisional_recovery={'private-position': {'profit': '-2.25'}})
        before = deepcopy(cycle)
        self.assertEqual(provisional_recovery_view(cycle), dict(pending=True, amount='4.25'))
        self.assertEqual(cycle, before)
        self.assertNotIn('private-position', str(provisional_recovery_view(cycle)))

    def test_absent_or_settled_hidden_even_if_old_effective_remains(self):
        for cycle in ({}, {'provisional_recovery': {}, 'recovery_effective': '4.25'}):
            self.assertIsNone(provisional_recovery_view(cycle))

    def test_unknown_invalid_amount_not_presented_as_number(self):
        for amount in (None, '-1', 'NaN', 'Infinity', 'invalid'):
            self.assertEqual(provisional_recovery_view(dict(provisional_recovery={'id': {}},
                             recovery_effective=amount)), dict(pending=True, amount=None))
