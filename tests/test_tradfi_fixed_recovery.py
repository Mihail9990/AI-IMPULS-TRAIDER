import unittest
from decimal import Decimal as D
from tradfi.strategies.fixed_recovery import RecoveryConfig,initial,installment,resize,settle,net_money

class FixedRecoveryTest(unittest.TestCase):
    def test_three_remaining_installments_not_redivided_by_original_n(self):
        s=settle(initial(RecoveryConfig()),round_id='1',reason='SL',net='-54')
        self.assertEqual(installment(s),D(18))
        s=settle(s,round_id='2',reason='TP',net='18');self.assertEqual(installment(s),D(18))
        s=settle(s,round_id='3',reason='TP',net='18');self.assertEqual(installment(s),D(18))
        s=settle(s,round_id='4',reason='TP',net='17.99')
        self.assertTrue(s['active']);self.assertEqual(s['debt'],'0.01')
        s=settle(s,round_id='5',reason='TP',net='0.01');self.assertFalse(s['active'])

    def test_sl_adds_actual_loss_and_repartitions(self):
        s=settle(initial(RecoveryConfig()),round_id='1',reason='SL',net='-54')
        s=settle(s,round_id='2',reason='TP',net='18')
        s=settle(s,round_id='3',reason='SL',net='-18')
        self.assertEqual(s['debt'],'54');self.assertEqual(s['parts_remaining'],3)

    def test_n_change_preserves_money_and_uses_new_n(self):
        s=settle(initial(RecoveryConfig()),round_id='1',reason='SL',net='-54')
        s=settle(s,round_id='2',reason='TP',net='18');s=settle(s,round_id='3',reason='TP',net='18')
        updated=resize(s,5)
        self.assertEqual(updated['debt'],'18');self.assertEqual(updated['realized_net'],s['realized_net'])
        self.assertEqual(installment(updated),D('3.6'))

    def test_all_actual_costs_and_no_missing_cost_assumption(self):
        self.assertEqual(net_money([dict(profit='18',commission='-1',swap='-.5',fee='-.1')]),D('16.4'))
        with self.assertRaises(ValueError):net_money([dict(profit='18')])

    def test_manual_and_duplicate_round_are_explicit(self):
        s=settle(initial(RecoveryConfig()),round_id='1',reason='SL',net='-54')
        s=settle(s,round_id='2',reason='MANUAL',net='10')
        self.assertEqual(s['debt'],'44');self.assertEqual(s['rounds'][-1]['reason'],'MANUAL')
        with self.assertRaises(ValueError):settle(s,round_id='2',reason='TP',net='10')
