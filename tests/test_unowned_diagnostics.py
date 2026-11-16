import unittest
from tradfi.reconciliation_diagnostics import obligation_descriptor, unowned_diagnostics
from tradfi.context_diagnostics import project

class UnownedDiagnosticsTest(unittest.TestCase):
    def test_relationship_is_not_ownership(self):
        meta=obligation_descriptor('ORDER',{'type':1,'position_id':123,'ticket':456,'comment':'SECRET','price':99}, {'123'})
        row={'owned':False,'obligation_diagnostic':meta}
        result=unowned_diagnostics({'orders':[row],'positions':[]})
        self.assertFalse(row['owned'])
        self.assertEqual(result['unowned_obligations'],[{'record_kind':'ORDER','broker_order_type':'SELL','linked_to_known_position':True}])
        self.assertNotIn('123',str(result));self.assertNotIn('456',str(result));self.assertNotIn('SECRET',str(result))

    def test_bounded_and_unknown_safe(self):
        meta=obligation_descriptor('ORDER',{'type':'SECRET','position_id':0},{'0'})
        self.assertEqual(meta['broker_order_type'],'UNKNOWN')
        self.assertFalse(meta['linked_to_known_position'])
        result=unowned_diagnostics({'orders':[{'owned':False,'obligation_diagnostic':meta}]*25,'positions':[{'owned':True}]})
        self.assertEqual(result['unowned_count'],25)
        self.assertEqual(len(result['unowned_obligations']),20)
        self.assertTrue(result['truncated'])

    def test_project_retains_cause_without_raw_fields(self):
        value=unowned_diagnostics({'orders':[], 'positions':[{'owned':False,'id':'SECRET','price':99}]})
        safe=project({'diagnostics':{'last_error':{'reason':'UNOWNED_BROKER_OBLIGATIONS','diagnostics':value}}})
        nested=safe['diagnostics']['last_error']['diagnostics']
        self.assertEqual(nested['unowned_count'],'1')
        self.assertEqual(nested['unowned_obligations'][0]['record_kind'],'POSITION')
        self.assertEqual(nested['unowned_obligations'][0]['broker_order_type'],'UNKNOWN')
        self.assertNotIn('SECRET',str(safe));self.assertNotIn('price',str(safe))

    def test_actual_cause_is_detached(self):
        row={'owned':False,'obligation_diagnostic':{'broker_order_type':'BUY_STOP','linked_to_known_position':False}}
        snap={'orders':[row],'positions':[]}
        value=unowned_diagnostics(snap)
        row['obligation_diagnostic']['broker_order_type']='SELL'
        snap['orders'].clear()
        self.assertEqual(value['unowned_obligations'][0]['broker_order_type'],'BUY_STOP')

    def test_pause_retains_cause_after_next_snapshot(self):
        from tradfi.engine import Controller
        controller=object.__new__(Controller)
        controller.state={'cycle':None}
        controller.notify=lambda message:None
        controller.save=lambda event:None
        controller.reconciliation_diagnostics={'reason':'SNAPSHOT_VALIDATED',**unowned_diagnostics({'orders':[{'owned':False}], 'positions':[]})}
        controller.pause('Неизвестные ордера/позиции')
        controller.reconciliation_diagnostics={'reason':'SNAPSHOT_VALIDATED'}
        safe=project({'diagnostics':{'last_error':controller.pause_diagnostics}})
        self.assertEqual(safe['diagnostics']['last_error']['reason'],'UNOWNED_BROKER_OBLIGATIONS')
        self.assertEqual(safe['diagnostics']['last_error']['diagnostics']['unowned_count'],'1')
        self.assertEqual(controller.state['phase'],'SAFE_PAUSE')
