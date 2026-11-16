"""Read-only history experiments against a fake API; no native MT5 import."""
import ast
from datetime import datetime
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as Row
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1]/'tools'/'mt5_history_probe.py'
spec = importlib.util.spec_from_file_location('history_probe', PATH)
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)


class Fake:
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    def __init__(self, filter_shift=0):
        self.shift = filter_shift
        self.now = 1791809547
        self.calls = []
        self.closed = False
        self.empty = self.none = self.changed = False
        raw = (self.now-20*86400+10800)*1000
        self.orders = [Row(symbol='XAUUSD.s', ticket=987654321, position_id=1122334455,
                           time_setup_msc=raw-60000, time_done_msc=raw,
                           comment='PRIVATE_COMMENT', profit=99887766)]
        self.deals = [Row(symbol='XAUUSD.s', ticket=123456789, order=987654321,
                          position_id=1122334455, time_msc=raw, profit=99887766, comment='PRIVATE_COMMENT')]
    def initialize(self, *args, **kwargs): return True
    def shutdown(self): self.closed = True
    def terminal_info(self):
        return Row(connected=True, path='C:/Program Files/MetaTrader 5')
    def account_info(self):
        return Row(login=55667788 if not self.changed else 88776655, server='Bybit-Live-6', currency='UST', margin_mode=2)
    def history_orders_get(self, *args, **kwargs): return self.query('orders', args, kwargs)
    def history_deals_get(self, *args, **kwargs): return self.query('deals', args, kwargs)
    def query(self, kind, args, kwargs):
        self.calls.append((kind, args, kwargs))
        if self.none: return None
        if self.empty: return []
        rows = self.orders if kind=='orders' else self.deals
        if 'ticket' in kwargs:
            return [r for r in rows if (r.ticket if kind=='orders' else r.order)==kwargs['ticket']]
        if 'position' in kwargs:
            return [r for r in rows if r.position_id==kwargs['position']]
        lo,hi = [x.timestamp() if isinstance(x,datetime) else x for x in args]
        field = 'time_done_msc' if kind=='orders' else 'time_msc'
        return [r for r in rows if lo <= getattr(r,field)/1000-self.shift <= hi]


class HistoryProbeTest(unittest.TestCase):
    def inspect(self, api):
        output=[]
        probe.inspect_history(api,output.append,wall=lambda:api.now)
        return output

    def test_narrow_windows_distinguish_filter_from_output_timestamp_scale(self):
        for shift, matching in ((0,'RAW_ENCODING'),(10800,'BYBIT_UTC_HYPOTHESIS')):
            api=Fake(shift); output=self.inspect(api)
            checks=[r for r in output if r['event']=='narrow_comparison' and r['field'] in ('time_done_msc','time_msc')]
            self.assertEqual(len(checks),8)
            for r in checks:
                self.assertEqual(r['selected_present'],r['hypothesis']==matching)
                self.assertLessEqual(r['end_epoch']-r['start_epoch'],5)
            # Both epoch and aware-UTC datetime forms tested, independently of raw output.
            self.assertEqual({r['argument_form'] for r in checks},{'epoch','utc_datetime'})

    def test_older_event_and_ticket_position_queries_are_private(self):
        api=Fake(); output=self.inspect(api)
        self.assertTrue(all(r['selected_record_matches'] for r in output if r['event']=='direct_comparison'))
        self.assertIn(('deals',(),{'ticket':987654321}),api.calls)
        self.assertIn(('deals',(),{'position':1122334455}),api.calls)
        text=json.dumps(output)
        for value in ('987654321','123456789','1122334455','55667788','99887766','PRIVATE_COMMENT'):
            self.assertNotIn(value,text)
        window=next(r for r in output if r['event']=='window')
        self.assertEqual(window['start_epoch'],api.now-90*86400-10800)

    def test_empty_and_none_are_distinct_and_never_approve_profile(self):
        for mode in ('empty','none'):
            api=Fake(); setattr(api,mode,True)
            output=self.inspect(api)
            self.assertFalse(any(r['event']=='narrow_comparison' for r in output))
            status='EMPTY_SYMBOL_INCONCLUSIVE' if mode=='empty' else 'API_NONE_INCONCLUSIVE'
            self.assertTrue(any(r.get('status')==status for r in output))
            self.assertEqual(output[0]['history_time_status'],'UNVERIFIED')

    def test_other_symbols_not_dumped_or_used_as_samples(self):
        api=Fake(); api.orders[0].symbol='PRIVATE_OTHER_SYMBOL'; api.deals=[]
        output=self.inspect(api)
        coverage=next(r for r in output if r['event']=='coverage')
        self.assertEqual(coverage['total_count'],1)
        self.assertEqual(coverage['exact_symbol_count'],0)
        self.assertNotIn('PRIVATE_OTHER_SYMBOL',json.dumps(output))

    def test_context_switch_after_history_read_stops_and_shutdowns(self):
        api=Fake(); original=api.history_orders_get
        def switched(*args,**kwargs):
            result=original(*args,**kwargs); api.changed=True; return result
        api.history_orders_get=switched
        output=[]
        self.assertEqual(probe.run(api,output.append),1)
        self.assertTrue(api.closed)
        self.assertFalse(any(r['event']=='sample' for r in output))

    def test_exception_is_redacted_and_shutdown_runs(self):
        api=Fake(); output=[]
        with patch.object(api,'history_orders_get',side_effect=RuntimeError('PRIVATE_COMMENT')):
            self.assertEqual(probe.run(api,output.append),1)
        self.assertTrue(api.closed)
        self.assertNotIn('PRIVATE_COMMENT',json.dumps(output))

    def test_dst_inverse_ambiguity_is_not_guessed(self):
        raw=datetime.fromisoformat('2026-11-01T04:30:00+00:00').timestamp()
        self.assertEqual(len(probe.candidates(raw)),2)
        raw=datetime.fromisoformat('2026-03-08T04:30:00+00:00').timestamp()
        self.assertEqual(probe.candidates(raw),[])

    def test_readonly_surface(self):
        tree=ast.parse(PATH.read_text())
        attrs={n.attr for n in ast.walk(tree) if isinstance(n,ast.Attribute)
               and isinstance(n.value,ast.Name) and n.value.id=='api'}
        self.assertEqual(attrs,{'initialize','shutdown','terminal_info','account_info',
                               'ACCOUNT_MARGIN_MODE_RETAIL_HEDGING','history_orders_get','history_deals_get'})
        self.assertNotIn('order_send',PATH.read_text())


if __name__ == '__main__': unittest.main()
