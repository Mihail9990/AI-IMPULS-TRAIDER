"""Offline bounded lookbacks: real recomputation, independent worker caches."""
import json
import queue
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tradfi.analysis_service import (BAR_LIMIT, LOOKBACKS, AnalysisService,
    DisabledAnalysisService, _worker, calculate, read_bars, read_raw_bars,
    empty_snapshot)
from tradfi.web_control import WebTransport
from tradfi.control import OwnerMailbox
from tests.test_tradfi_analysis_service import API, CONFIG, NOW, proof


class HistoryAPI(API):
    def copy_rates_from_pos(self, symbol, tf, start, count):
        self.calls.append((symbol, tf, start, count))
        end = (NOW + 7200) // (tf * 60) * tf * 60
        return [dict(time=end-tf*60*i, open=10, high=12, low=9, close=11,
                     tick_volume=5, real_volume=0)
                for i in range(count-1+start, start-1, -1)]


class LookbackTests(unittest.TestCase):
    def test_native_bounds_and_analyzer_inputs(self):
        for depth in LOOKBACKS:
            api = HistoryAPI(); seen = []
            result = calculate(api, dict(CONFIG, analysis_bars=depth), proof(), {},
                mono=10, analyzer=lambda bars, tf, now: seen.append(len(bars)) or {})
            self.assertEqual(seen, [depth]*4)
            self.assertTrue(all(len(v['bars']) == depth for v in result.values()))
            self.assertTrue(all(c[-1] == depth for c in api.calls))
            raw = read_raw_bars(api, dict(CONFIG, analysis_bars=depth), 'M1', NOW+7200)
            self.assertEqual(len(raw), depth)
            self.assertEqual(api.calls[-1][-2:], (0, depth+1))

    def test_invalid_bounds_rejected_before_native_read(self):
        for depth in (0, 1, 49, 301, 501, 1000000, True, '100', 100.0):
            api = HistoryAPI()
            with self.assertRaises(ValueError):
                read_bars(api, dict(CONFIG, analysis_bars=depth), 'M1', NOW)
            self.assertEqual(api.calls, [])
            with self.assertRaises(ValueError): DisabledAnalysisService('X').view('M1', depth)

    def test_worker_recomputes_and_reuses_separate_depths(self):
        self.check_worker_depths(CONFIG, proof())

    def test_raw_worker_recomputes_and_reuses_separate_depths(self):
        self.check_worker_depths(dict(CONFIG, time_mode='mt5_raw_v1'),
            dict(proof(), timestamp_semantics='BROKER_RAW', broker_time=NOW+7200))

    def check_worker_depths(self, config, observation):
        api = HistoryAPI(); api.initialize = lambda *a, **k: True
        api.shutdown = lambda: None
        incoming = queue.Queue(); messages = []; seen = []
        # A repeated 50 must reuse its own cached result, not the intervening 100.
        for depth in (50, 100, 50): incoming.put(dict(observation, analysis_bars=depth))
        incoming.put(None)
        output = SimpleNamespace(send_bytes=lambda v: messages.append(json.loads(v)), close=lambda: None)
        def analyze(bars, tf, now):
            seen.append((tf, len(bars)))
            return {'overlays': {'calculated_depth': len(bars)}}
        with patch.dict(sys.modules, {'MetaTrader5': api}), \
                patch('tradfi.analysis_service.analysis_clock', return_value=10), \
                patch('tradfi.analysis_adapters.analyze_bars', side_effect=analyze):
            _worker(config, incoming, output)
        self.assertEqual(len(seen), 8)
        for response, depth in zip(messages, (50, 100, 50)):
            for value in response.values():
                self.assertEqual(value['requested_bars'], depth)
                self.assertEqual(len(value['bars']), depth)
                self.assertEqual(value['overlays']['calculated_depth'], depth)
        self.assertTrue(messages[2]['M1']['diagnostics']['cache_reused'])

    def test_route_allowlist_default_and_read_only(self):
        calls = []
        cache = SimpleNamespace(view=lambda tf, bars=BAR_LIMIT: calls.append((tf,bars)) or empty_snapshot('X',tf))
        box = OwnerMailbox(); transport = WebTransport(box, password='offline-test-secret'*2, analysis=cache)
        headers = {'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        _,_,extra = transport.request('POST','/api/login',headers,json.dumps({'password':'offline-test-secret'*2}).encode())
        headers['Cookie'] = extra['Set-Cookie'].split(';')[0]
        for query in ('timeframe=M1', *(f'timeframe=M1&bars={n}' for n in LOOKBACKS)):
            self.assertEqual(transport.request('GET','/api/analysis?'+query,headers)[0],200)
        self.assertEqual(calls, [('M1',500)]+[('M1',n) for n in LOOKBACKS])
        for value in ('', '0', '501', '100.0', '+100', '0100', '-50', 'true', '100&bars=200'):
            self.assertEqual(transport.request('GET','/api/analysis?timeframe=M1&bars='+value,headers)[0],400)
        self.assertTrue(box.requests.empty())

    def test_service_views_never_calculate_or_mix_depths(self):
        broker = SimpleNamespace(account='123@test', login=123, symbol='X', path='C:/mt5/terminal64.exe',
            server='test', market_time_offset_seconds=7200, max_age=5, api=SimpleNamespace(is_test_binding=True))
        service = AnalysisService(broker)
        service.cache = {'M1': dict(empty_snapshot('X','M1'), notes=['default'])}
        service.lookback_cache = {50: {'M1': dict(empty_snapshot('X','M1'), notes=['fifty'])}}
        with patch('tradfi.analysis_service.calculate', side_effect=AssertionError('HTTP calculation')):
            self.assertEqual(service.view('M1')['notes'], ['default'])
            value = service.view('M1',50)
            self.assertEqual(value['notes'], ['fifty'])
            value['notes'].append('mutated')
            self.assertEqual(service.view('M1',50)['notes'], ['fifty'])
            self.assertEqual(service.view('M1',100)['requested_bars'],100)
            self.assertEqual(service.view('M1',100)['bars'],[])


class AnalysisResourceTests(unittest.TestCase):
    def test_scenario_resource_is_served_from_loopback_allowlist(self):
        from io import BytesIO
        from pathlib import Path
        from tradfi.web_control import NativePanel

        class OfflineServer:
            def __init__(self, address, handler):
                self.handler = handler
            def serve_forever(self):
                pass

        panel = NativePanel.__new__(NativePanel)
        panel.authority = '127.0.0.1:8765'
        panel.controller = SimpleNamespace(state={'basket_settings': {}})
        panel.transport = WebTransport(OwnerMailbox(), authority=panel.authority,
                                       password='offline-test-secret'*2)
        # Capture the real nested handler without opening sockets or threads.
        with patch('http.server.ThreadingHTTPServer', OfflineServer), \
                patch('tradfi.web_control.threading.Thread'):
            panel.start()
        handler = panel.server.handler.__new__(panel.server.handler)
        handler.command = 'GET'
        handler.path = '/analysis-scenario.js'
        handler.headers = {'Host': panel.authority}
        handler.rfile = BytesIO()
        replies = []
        handler.reply = lambda *args: replies.append(args)
        handler.handle_call()
        code, body, mime, _ = replies.pop()
        self.assertEqual(code, 200)
        self.assertEqual(mime, 'text/javascript')
        self.assertEqual(body, (Path(__file__).resolve().parents[1] /
            'web/basket-panel/analysis-scenario.js').read_bytes())
        handler.headers = {'Host': 'evil.invalid'}
        handler.handle_call()
        self.assertNotEqual(replies.pop()[0], 200)
