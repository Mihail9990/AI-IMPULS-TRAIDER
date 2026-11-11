"""Offline diagnostics regression: bounded, private, no new polling or log writes."""
import json
import unittest
from test_tradfi_analysis_service import API, CONFIG, NOW, proof
import test_tradfi_analysis_service as source_tests
from tradfi.analysis_service import calculate
from tradfi.context_diagnostics import project
from tradfi.web_logs import stable_context


class DiagnosticsTests(unittest.TestCase):
    def test_cache_and_native_gap_reasons(self):
        api = API()
        values = calculate(api, CONFIG, proof(), {}, mono=10, analyzer=lambda *a: {})
        self.assertEqual(values['M1']['diagnostics']['reason'], 'CALCULATED')
        self.assertEqual(values['M1']['diagnostics']['actual_closed_through'], NOW)
        again = calculate(api, CONFIG, proof(), values, mono=11)
        self.assertEqual(len(api.calls), 4)
        self.assertEqual(again['M1']['diagnostics']['reason'], 'CACHE_REUSED')
        api.rows = api.copy_rates_from_pos('', 60, 1, 500)[:-1]
        gap = calculate(api, CONFIG, proof(), {}, mono=10, analyzer=lambda *a: {})['H1']
        self.assertEqual(gap['diagnostics']['reason'], 'MISSING_LAST_CLOSED_PERIOD')
        self.assertLess(gap['diagnostics']['actual_closed_through'], gap['diagnostics']['expected_closed_through'])

    def test_proof_failure_and_retry_have_explicit_reason(self):
        api = API()
        values = calculate(api, CONFIG, proof(), {}, mono=16)
        self.assertEqual(values['M1']['diagnostics']['reason'], 'PROOF_STALE')
        self.assertFalse(api.calls)
        api.rows = []
        retry = {}
        values = calculate(api, CONFIG, proof(), {}, mono=10, retry_state=retry)
        self.assertEqual(values['M1']['diagnostics']['error_code'], 'HISTORY_UNAVAILABLE')
        again = calculate(api, CONFIG, proof(), values, mono=11, retry_state=retry)
        self.assertEqual(again['M1']['diagnostics']['reason'], 'HISTORY_RETRY_WAIT')
        self.assertEqual(again['M1']['diagnostics']['retry_in_seconds'], 4)

    def test_exception_and_export_privacy(self):
        def bad(*args): raise RuntimeError('password /private/account 123')
        result = calculate(API(), CONFIG, proof(), {}, mono=10, analyzer=bad)
        self.assertEqual(result['M1']['diagnostics']['error_code'], 'ANALYSIS_ERROR')
        self.assertNotIn('/private', json.dumps(result))
        row = dict(result['M1']['diagnostics'], timeframe='M1', login=123, terminal_path='/private', cookies='secret', feed_instance='secret')
        exported = project({'analysis': [row]})['analysis'][0]
        self.assertEqual(exported['reason'], 'ANALYSIS_SOURCE_UNAVAILABLE')
        self.assertEqual(exported['timeframe'], 'M1')
        for key in ('login', 'terminal_path', 'cookies', 'feed_instance'): self.assertNotIn(key, exported)

    def test_export_is_bounded_no_start_and_no_log_churn(self):
        from tradfi.analysis_service import AnalysisService
        service = AnalysisService(source_tests.ServiceTests().broker())
        rows = service.diagnostic_view()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]['supervisor_state'], 'IDLE')
        self.assertIsNone(service.thread)
        self.assertEqual(stable_context({'phase': 'IDLE', 'analysis': rows}), {'phase': 'IDLE'})
        self.assertLess(len(json.dumps(rows)), 8192)

    def test_cached_proof_expiry_is_not_reported_as_missing_history(self):
        from tradfi.analysis_service import AnalysisService, empty_snapshot
        import time
        service = AnalysisService(source_tests.ServiceTests().broker())
        service.enabled = True
        service.thread = object()
        service.proof = dict(proof(), monotonic=time.monotonic())
        for reason in ('PROOF_EXPIRED_DURING_CALCULATION', 'PROOF_EXPIRED_BEFORE_PUBLISH'):
            cached = empty_snapshot('XAUUSD.s', 'M1', 'STALE')
            cached.update(closed_through=NOW, bars=[{'utc_time': NOW-60}],
                          diagnostics={'reason': reason})
            service.cache = {'M1': cached}
            actual = service.view('M1')
            self.assertEqual(actual['status'], 'STALE')
            self.assertEqual(actual['diagnostics']['reason'], reason)
        cached['closed_through'] = NOW-60
        self.assertEqual(service.view('M1')['diagnostics']['reason'], 'MISSING_LAST_CLOSED_PERIOD')

    def test_worker_final_proof_expiry_updates_metadata(self):
        from tradfi.analysis_service import _worker, empty_snapshot
        from unittest.mock import patch
        import queue
        import sys
        api = API()
        api.initialize = lambda *a, **kw: True
        api.shutdown = lambda: None
        incoming = queue.Queue()
        incoming.put(proof()); incoming.put(None)
        class Output:
            def __init__(self): self.messages = []
            def send_bytes(self, data): self.messages.append(json.loads(data))
            def close(self): pass
        output = Output()
        cache = {'M1': empty_snapshot('XAUUSD.s', 'M1', 'READY')}
        with patch.dict(sys.modules, {'MetaTrader5': api}), patch('tradfi.analysis_service.calculate', return_value=cache):
            _worker(CONFIG, incoming, output)
        value = output.messages[0]['M1']
        self.assertEqual(value['status'], 'STALE')
        self.assertEqual(value['diagnostics']['reason'], 'PROOF_EXPIRED_BEFORE_PUBLISH')
        self.assertEqual(value['diagnostics']['proof_status'], 'STALE')

    def test_backoff_retains_safe_failure_and_export_current_proof(self):
        from tradfi.analysis_service import AnalysisService
        import time
        api = API(); api.rows = []; retry = {}
        values = calculate(api, CONFIG, proof(), {}, mono=10, retry_state=retry)
        for mono in (11, 12):
            values = calculate(api, CONFIG, proof(), values, mono=mono, retry_state=retry)
            diag = values['M1']['diagnostics']
            self.assertEqual(diag['error_code'], 'HISTORY_UNAVAILABLE')
            self.assertEqual(diag['stage'], 'HISTORY')
            self.assertEqual(diag['last_failure_reason'], 'ANALYSIS_SOURCE_UNAVAILABLE')
        service = AnalysisService(source_tests.ServiceTests().broker())
        service.cache = values
        service.proof = dict(proof(), monotonic=time.monotonic())
        service.received_mono = time.monotonic()-3
        rows = project({'analysis': service.diagnostic_view()})['analysis']
        self.assertEqual(rows[0]['current_proof_status'], 'READY')
        self.assertEqual(rows[0]['error_code'], 'HISTORY_UNAVAILABLE')
        self.assertGreaterEqual(float(rows[0]['cache_age_seconds']), 3)
        self.assertIsNone(service.thread)
