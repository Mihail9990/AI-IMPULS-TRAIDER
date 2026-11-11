"""Offline reproduction of Windows Python 3.13 -> 3.12 clock domains.

No broker connection, clocks set, tolerance extension, or stale promotion.
"""
import json
import queue
import sys
import unittest
from unittest.mock import patch, MagicMock

from test_tradfi_analysis_service import API, CONFIG, NOW
import test_tradfi_analysis_service as source_tests
from tradfi.analysis_service import AnalysisService, TIMEFRAMES, _worker, calculate, empty_snapshot, proof_status


class AnalysisClockTests(unittest.TestCase):
    def observed_proof(self):
        service = AnalysisService(source_tests.ServiceTests().broker())
        # Python 3.13 on Windows: monotonic and perf_counter both use QPC.
        with patch('time.monotonic', return_value=1000.009), patch('time.perf_counter', return_value=1000.009):
            service.observe(dict(connected=True, status='FRESH', timestamp_semantics='VERIFIED',
                                 timestamp=NOW, age_seconds=0, instance='one'))
        return service.proof

    def test_quantized_worker_monotonic_cannot_reject_fresh_qpc_proof(self):
        p = self.observed_proof()
        # Python 3.12 monotonic can remain behind the QPC timestamp until its
        # next ~15.6ms tick, even though IPC delivered the proof in the future.
        with patch('time.monotonic', return_value=1000.000), patch('time.perf_counter', return_value=1000.011):
            values = calculate(API(), CONFIG, p, {}, analyzer=lambda *a: {})
        for item in values.values():
            self.assertEqual(item['status'], 'READY')
            self.assertTrue(item['bars'])
            self.assertEqual(item['timestamp_semantics'], 'VERIFIED')
            self.assertAlmostEqual(item['diagnostics']['proof_age_seconds'], .002)
            self.assertEqual(item['closed_through'], NOW)

    def test_final_publish_uses_same_clock_as_observed_proof(self):
        api = API(); api.initialize = lambda *a, **kw: True; api.shutdown = lambda: None
        incoming = queue.Queue(); incoming.put(self.observed_proof()); incoming.put(None)
        payloads = []
        class Output:
            def send_bytes(self, payload): payloads.append(json.loads(payload))
            def close(self): pass
        values = {tf: empty_snapshot(CONFIG['symbol'], tf, 'READY') for tf in TIMEFRAMES}
        with patch.dict(sys.modules, {'MetaTrader5': api}), patch('tradfi.analysis_service.calculate', return_value=values), \
                patch('time.monotonic', return_value=1000.000), patch('time.perf_counter', return_value=1000.011):
            _worker(CONFIG, incoming, Output())
        self.assertTrue(all(value['status'] == 'READY' for value in payloads[0].values()))

    def test_real_expiry_and_clock_rollback_still_fail_closed(self):
        p = self.observed_proof()
        for clock, expected in ((1005.010, 'STALE'), (1000.008, 'TIME_UNVERIFIED')):
            api = API()
            with patch('time.monotonic', return_value=clock), patch('time.perf_counter', return_value=clock):
                values = calculate(api, CONFIG, p, {}, analyzer=lambda *a: {})
            self.assertTrue(all(value['status'] == expected and not value['bars'] for value in values.values()))
            self.assertEqual(api.calls, [])

    def test_native_missing_minute_stays_stale_without_synthesizing(self):
        p = self.observed_proof(); api = API(); api.now = NOW - 60
        with patch('time.monotonic', return_value=1000.000), patch('time.perf_counter', return_value=1000.011):
            value = calculate(api, CONFIG, p, {}, analyzer=lambda *a: {})['M1']
        self.assertEqual(value['status'], 'STALE')
        self.assertEqual(value['diagnostics']['reason'], 'MISSING_LAST_CLOSED_PERIOD')
        self.assertEqual(value['closed_through'], NOW - 60)
        self.assertEqual(value['diagnostics']['expected_closed_through'], NOW)
        self.assertEqual(len(value['bars']), 5)

    def test_unverified_disconnected_and_changed_identity_still_block(self):
        for changes, expected in (({'timestamp_semantics': 'UNVERIFIED'}, 'TIME_UNVERIFIED'),
                                  ({'connected': False}, 'DISCONNECTED')):
            p = self.observed_proof(); p.update(changes); api = API()
            with patch('time.perf_counter', return_value=1000.011):
                values = calculate(api, CONFIG, p, {}, analyzer=lambda *a: {})
            self.assertTrue(all(value['status'] == expected and not value['bars'] for value in values.values()))
            self.assertEqual(api.calls, [])
        api = API(); api.login = 999
        with patch('time.perf_counter', return_value=1000.011):
            values = calculate(api, CONFIG, self.observed_proof(), {}, analyzer=lambda *a: {})
        self.assertTrue(all(value['status'] == 'DISCONNECTED' and not value['bars'] for value in values.values()))
        self.assertEqual(api.calls, [])

    def test_dispatch_validates_and_sends_same_observation(self):
        service = AnalysisService(source_tests.ServiceTests().broker())
        service.enabled = True; service.last_access = 1000; service.external_python = 'unused-offline-fixture'
        p = self.observed_proof(); service.proof = p
        replacement = dict(p, timestamp_semantics='UNVERIFIED', instance='changed')
        sent = []; process = MagicMock(); process.is_external = True
        process.is_alive.side_effect = lambda: not process.stopped
        process.stopped = False
        process.terminate.side_effect = lambda: setattr(process, 'stopped', True)
        def send(value):
            sent.append(value); service.stop.set()
        process.input.put_nowait.side_effect = send
        def validate(observation, now):
            status = proof_status(observation, now)
            service.proof = replacement
            return status
        reader = MagicMock(); reader.is_alive.return_value = False
        with patch('tradfi.analysis_process.ExternalWorker', return_value=process), \
                patch('tradfi.analysis_service.threading.Thread', return_value=reader), \
                patch('tradfi.analysis_service.proof_status', side_effect=validate), \
                patch('time.perf_counter', return_value=1000.011):
            service._supervise()
        self.assertEqual(len(sent), 1)
        self.assertIs(sent[0], p)
        self.assertEqual(sent[0]['timestamp_semantics'], 'VERIFIED')
        self.assertTrue(process.stopped)

    def test_view_uses_one_proof_then_rejects_new_instance_on_next_read(self):
        service = AnalysisService(source_tests.ServiceTests().broker())
        service.enabled = True; service.thread = object()
        p = self.observed_proof(); service.proof = p
        value = empty_snapshot(CONFIG['symbol'], 'M1', 'READY')
        value.update(bars=[{'utc_time': NOW-60}], closed_through=NOW, feed_instance='one')
        service.cache = {'M1': value}
        replacement = dict(p, timestamp_semantics='UNVERIFIED', instance='changed')
        def replace_after_first_check(observation, now):
            service.proof = replacement
            return proof_status(observation, now)
        with patch('tradfi.analysis_service.proof_status', side_effect=replace_after_first_check), \
                patch('time.perf_counter', return_value=1000.011):
            first = service.view('M1')
            second = service.view('M1')
        self.assertEqual(first['status'], 'READY')
        self.assertEqual(first['diagnostics']['proof_status'], 'READY')
        self.assertAlmostEqual(first['diagnostics']['proof_age_seconds'], .002)
        self.assertEqual(second['status'], 'WARMING_UP')
        self.assertEqual(second['bars'], [])


if __name__ == '__main__': unittest.main()
