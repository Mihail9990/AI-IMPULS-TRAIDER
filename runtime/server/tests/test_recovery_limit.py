import unittest
from recovery.engine import Engine

class LimitTests(unittest.TestCase):
    def make(self):
        e=Engine();e.replace_ranges([{'id':'1','price':'4000','side':'BUY','volume':'.01'}]);return e
    def full_stop(self,e,n):
        o=e.data['orders'][0]; e.fill('fill'+str(n),o['id'],o['price'])
        e.close('close'+str(n),e.data['positions'][0]['id'],'initial_sl','-1')
    def test_only_full_closes_count_and_no_unconfirmed_manual(self):
        e=self.make()
        for n in range(24):self.full_stop(e,n)
        r=e._range('1');self.assertEqual(r['full_sl_count'],24)
        self.assertEqual(r['run_state'],'cancellation_pending')
        self.assertFalse(e.data['orders'])
        with self.assertRaises(ValueError):e.confirm_limit_cancellations('1',['unrelated'])
        with self.assertRaises(ValueError):e.fill('race','unknown-broker-ticket','4000')
        e.confirm_limit_cancellations('1',[])
        self.assertEqual(r['run_state'],'manual');self.assertFalse(e.data['orders'])
        e.replace_ranges([{'id':'1','price':'4000','side':'BUY','volume':'.01'}])
        self.assertEqual(e._range('1')['full_sl_count'],0)
    def test_support_and_manual_not_counted(self):
        e=self.make();o=e.data['orders'][0];e.fill('f',o['id'],'4000')
        e.quote('4000.5','4000.7');e.close('c',e.data['positions'][0]['id'],'support_sl','0')
        self.assertEqual(e._range('1')['full_sl_count'],0)
        o=e.data['orders'][0];e.fill('f2',o['id'],o['price'])
        e.close('c2',e.data['positions'][0]['id'],'manual','-1')
        self.assertEqual(e._range('1')['full_sl_count'],0)
