"""Offline tests for the standalone time probe; never import native MT5."""
import ast
import importlib.util
import json
import contextlib
import io
import tempfile
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
import unittest

PATH = Path(__file__).resolve().parents[1]/'tools'/'mt5_time_probe.py'
spec = importlib.util.spec_from_file_location('mt5_time_probe', PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Fake:
    TIMEFRAME_M1 = 1

    def __init__(self):
        self.now = 1791808511.0
        self.elapsed = 0
        self.closed = False
        self.calls = []
        self.history = True
        self.missing = False
        self.error = False
        self.initialized = True

    def sleep(self, seconds): self.now += seconds; self.elapsed += seconds
    def initialize(self, path, **kwargs):
        self.calls.append(('initialize', path, kwargs))
        return self.initialized
    def shutdown(self): self.closed = True
    def symbol_info_tick(self, symbol):
        if self.error: raise RuntimeError('SECRET_ACCOUNT_TOKEN')
        if self.missing: return None
        return SimpleNamespace(time=int(self.now)+10800, time_msc=int((self.now+10800)*1000),
                               bid=123456.78, account='SECRET_ACCOUNT_TOKEN')
    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        self.calls.append(('bars', symbol, timeframe, start, count))
        if self.missing: return None
        current = (int(self.now)+10800)//60*60
        return [dict(time=current-120), dict(time=current-60), dict(time=current)]
    def history_orders_get(self, *args, **kwargs): return self._history('orders', args, kwargs)
    def history_deals_get(self, *args, **kwargs): return self._history('deals', args, kwargs)
    def _history(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))
        if self.missing: return None
        if not self.history: return []
        record = SimpleNamespace(symbol='XAUUSD.s', ticket=987654321,
                                 order=1122334455, time_msc=int((self.now+10800)*1000),
                                 time_done_msc=int((self.now+10800)*1000),
                                 profit=99887766, comment='SECRET_ACCOUNT_TOKEN')
        if 'ticket' in kwargs: return [record]
        return [record] if args[1] > self.now+10800 else []


class TimeProbeTests(unittest.TestCase):
    def execute(self, api):
        output = []
        result = probe.run(api, output.append, wall=lambda:api.now,
                           monotonic=lambda:api.elapsed, sleep=api.sleep)
        return result, output

    def test_90_seconds_raw_offset_and_bar_rollover_without_correction(self):
        api = Fake()
        code, output = self.execute(api)
        self.assertEqual(code, 0)
        ticks = [x for x in output if x['event']=='tick']
        self.assertEqual(len(ticks), 10)
        self.assertEqual(ticks[-1]['elapsed_s'], 90)
        self.assertTrue(all(x['age_s']==-10800 for x in ticks))
        self.assertEqual(ticks[1]['delta_raw_ms'], 10000)
        self.assertTrue(any(x.get('current_bar_advanced') for x in output))
        self.assertTrue(api.closed)
        self.assertEqual(api.calls[0][1], probe.TERMINAL)
        self.assertTrue(all(c[-2:]==(0,3) for c in api.calls if c[0]=='bars'))

    def test_history_bounds_and_internal_order_ticket_for_deals(self):
        api = Fake()
        _, output = self.execute(api)
        for kind in ('orders', 'deals'):
            calls = [c for c in api.calls if c[0]==kind]
            self.assertEqual(calls[0][1][0], int(api.now-86400))
            self.assertEqual(calls[1][1][1]-calls[0][1][1], 10800)
            expected = 987654321 if kind=='orders' else 1122334455
            self.assertEqual(calls[2][2], {'ticket':expected})
        comparisons = [x for x in output if x['event']=='ticket_comparison']
        self.assertTrue(all(x['selected_record_matches'] for x in comparisons))
        text = json.dumps(output)
        for private in ('987654321', '1122334455', '99887766', 'SECRET_ACCOUNT_TOKEN', '123456.78'):
            self.assertNotIn(private, text)

    def test_empty_history_is_inconclusive_and_no_ticket_queries(self):
        api = Fake(); api.history = False
        _, output = self.execute(api)
        records = [x for x in output if x['event']=='history']
        self.assertEqual([x['status'] for x in records], ['EMPTY_INCONCLUSIVE']*2)
        self.assertFalse(any(x['event']=='ticket_comparison' for x in output))

    def test_none_data_is_not_empty_history(self):
        api = Fake(); api.missing = True
        _, output = self.execute(api)
        self.assertTrue(any(x.get('status')=='TICK_NONE' for x in output))
        self.assertEqual([x['status'] for x in output if x['event']=='history'],
                         ['API_NONE_INCONCLUSIVE']*2)
        self.assertTrue(api.closed)

    def test_exception_and_initialize_failure_are_redacted_and_closed(self):
        for failure in ('error', 'initialize'):
            api = Fake()
            if failure=='error': api.error = True
            else: api.initialized = False
            code, output = self.execute(api)
            self.assertEqual(code, 1)
            self.assertNotIn('SECRET_ACCOUNT_TOKEN', json.dumps(output))
            self.assertTrue(api.closed)

    def test_main_creates_new_report_without_overwriting_existing_files(self):
        api = Fake(); api.initialized = False
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp)/'mt5_time_probe.py'
            existing = Path(tmp)/'mt5_time_report_old.txt'
            existing.write_text('keep')
            with patch.object(probe, '__file__', str(script)), \
                    patch.object(probe.importlib, 'import_module', return_value=api), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            reports = list(Path(tmp).glob('mt5_time_report_*.txt'))
            self.assertEqual(len(reports), 2)
            self.assertEqual(existing.read_text(), 'keep')
            report = next(p for p in reports if p != existing)
            rows = [json.loads(line) for line in report.read_text().splitlines()]
            self.assertEqual(rows[-1], {'event':'shutdown', 'status':'DONE'})
            self.assertNotIn('SECRET_ACCOUNT_TOKEN', report.read_text())

    def test_api_surface_is_readonly_and_no_strategy_imports(self):
        tree = ast.parse(PATH.read_text())
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                 and isinstance(n.value, ast.Name) and n.value.id=='api'}
        self.assertEqual(attrs, {'initialize', 'shutdown', 'symbol_info_tick',
            'copy_rates_from_pos', 'TIMEFRAME_M1', 'history_orders_get', 'history_deals_get'})
        imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imports, {'datetime', 'pathlib'})


if __name__ == '__main__':
    unittest.main()
