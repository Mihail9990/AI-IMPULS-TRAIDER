import unittest
from tradfi.engine import Controller
from tradfi.reconciliation_diagnostics import reason_code, evidence_codes
from tradfi.context_diagnostics import project

class DiagnosticCodesTest(unittest.TestCase):
    def test_codes_never_emit_unknown_text(self):
        self.assertEqual(reason_code('password SECRET', 'OTHER_VALIDATION_FAILURE'), 'OTHER_VALIDATION_FAILURE')
        self.assertEqual(reason_code('Terminal obligation evidence changed; reconcile before execution','OTHER_VALIDATION_FAILURE'), 'TERMINAL_OBLIGATION_EVIDENCE_CHANGED')
    def test_evidence_status_and_id_projection(self):
        value=project({'diagnostics':{'commands':evidence_codes({'obligations':{'raw-ticket':{'status':'EXTERNAL_EXIT','comment':'SECRET'},'raw-other':{'status':'SECRET'}}})}})
        self.assertEqual(value['diagnostics']['commands'][0]['status'],'EXTERNAL_EXIT')
        self.assertNotIn('raw-ticket',str(value));self.assertNotIn('SECRET',str(value))
    def test_pause_projection_retains_nested_reason(self):
        value=project({'diagnostics':{'last_error':{'reason':'BROKER_FACTS_NOT_CONFIRMED','diagnostics':{'reason':'TERMINAL_OBLIGATION_EVIDENCE_CHANGED','commands':[{'id':'secret-id','status':'EXTERNAL_EXIT'}]}}}})
        self.assertEqual(value['diagnostics']['last_error']['diagnostics']['reason'],'TERMINAL_OBLIGATION_EVIDENCE_CHANGED')

    def test_invalid_evidence_shapes_are_diagnostic_only(self):
        self.assertEqual(evidence_codes({'obligations':None}),[])
        self.assertEqual(evidence_codes({'obligations':[]}),[])
        self.assertEqual(evidence_codes({'obligations':{'x':{'status':{}}}})[0]['status'],'OTHER_EVIDENCE_STATUS')
    def test_failed_snapshot_latches_evidence_for_pause(self):
        from types import SimpleNamespace
        controller=object.__new__(Controller)
        controller.observation=SimpleNamespace(begin=lambda:0,fail=lambda:None)
        def validate(**kw):
            controller.reconciliation_diagnostics={'commands':[{'id':'secret-ticket','status':'EXTERNAL_EXIT'}]}
            raise ValueError('Terminal obligation evidence changed; reconcile before execution')
        controller._validated_snapshot=validate
        with self.assertRaises(ValueError):controller.snapshot()
        controller.state={'cycle':None};controller.notify=lambda x:None;controller.save=lambda x:None
        controller.pause('Не удалось подтвердить брокерские факты')
        value=project({'diagnostics':controller.pause_diagnostics})
        self.assertEqual(value['diagnostics']['diagnostics']['reason'],'TERMINAL_OBLIGATION_EVIDENCE_CHANGED')
        self.assertEqual(value['diagnostics']['diagnostics']['commands'][0]['status'],'EXTERNAL_EXIT')
        self.assertNotIn('secret-ticket',str(value))
