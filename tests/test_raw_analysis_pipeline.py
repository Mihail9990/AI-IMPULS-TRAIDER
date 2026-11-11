"""Offline raw MT5 observations never become UTC or expire with host time."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tradfi.chart_feed import ChartFeed
from tradfi.analysis_service import AnalysisService, calculate, read_bars, proof_status, TIMEFRAMES
from tradfi.analysis_adapters import _prepare, analyze_bars
from tradfi.analysis_patterns import _prepare as prepare_patterns

NOW = 1800000000
CONFIG = dict(symbol='TEST', terminal_path='C:/mt5/terminal64.exe', expected_login=123,
              expected_server='test', time_mode='mt5_raw_v1', offset=0)

class API:
    TIMEFRAME_M1=1; TIMEFRAME_M15=15; TIMEFRAME_M30=30; TIMEFRAME_H1=60
    is_test_binding=True
    def __init__(self): self.calls=[]; self.rows=None
    def account_info(self): return SimpleNamespace(login=123, server='test')
    def terminal_info(self): return SimpleNamespace(connected=True, path='C:/mt5')
    def copy_rates_from_pos(self, symbol, tf, start, count):
        self.calls.append((tf,start,count))
        return self.rows if self.rows is not None else [dict(time=NOW-i*tf*60,open=10,high=12,low=9,close=11) for i in (5,2,1,0)]

def proof():
    return dict(connected=True, timestamp_semantics='BROKER_RAW', status='OBSERVED',
                broker_time=NOW+1, instance='raw', monotonic=10, age=0, max_age=1)

class RawAnalysisTests(unittest.TestCase):
    def test_old_observation_never_expires_or_extrapolates(self):
        p=proof()
        self.assertEqual(proof_status(p, 10**20),'READY')
        self.assertEqual(proof_status(p, -1000),'READY')
        values=calculate(API(),CONFIG,p,{},mono=10**20,analyzer=lambda *args: {})
        for value in values.values():
            self.assertEqual(value['status'],'READY')
            self.assertEqual(value['as_of'],NOW+1)
            self.assertEqual(value['timestamp_semantics'],'BROKER_RAW')
            self.assertEqual(len(value['bars']),3)
            self.assertTrue(all(b['utc_time'] is None and b['closed'] for b in value['bars']))
    def test_native_closed_positions_and_gaps(self):
        api=API(); bars=read_bars(api,CONFIG,'M1',NOW+1)
        self.assertEqual(api.calls,[(1,0,501)])
        self.assertEqual([b['broker_time'] for b in bars],[NOW-300,NOW-120,NOW-60])
        self.assertTrue(all(b['broker_time'] < NOW for b in bars))
        for prepare in (_prepare,prepare_patterns):
            rows,now=prepare(bars,'M1',NOW+1)
            self.assertEqual(len(rows),3)
            self.assertTrue(all('utc_time' not in row for row in rows))
    def test_invalid_native_order_ohlc_and_future_rejected(self):
        for change in ('order','ohlc','future'):
            api=API(); rows=api.copy_rates_from_pos('TEST',1,0,10)
            if change=='order': rows.reverse()
            if change=='ohlc': rows[0]['high']=1
            if change=='future': rows[-1]['time']=NOW+60
            api.rows=rows
            with self.assertRaises(ValueError): read_bars(api,CONFIG,'M1',NOW+1)
    def test_raw_observation_service_and_chart_ignore_wall_clock(self):
        def forbidden(): raise AssertionError('host quote clock must not be used')
        broker=SimpleNamespace(api=API(),symbol='TEST',config={'market_time_mode':'mt5_raw_v1'},
            max_age=1, path='C:/mt5/terminal64.exe',login=123,server='test',_context=lambda **kw:None,
            _quote_clock=forbidden, _timestamp_semantics_verified=forbidden)
        ticks=[0]
        feed=ChartFeed(broker,clock=lambda:-100000,monotonic=lambda:ticks[0])
        value=feed.capture({'quote':{'broker_time':NOW+1,'utc_time':None}})
        self.assertEqual(value['status'],'OBSERVED')
        self.assertEqual(value['timestamp_semantics'],'BROKER_RAW')
        self.assertFalse(value['candles'][-1]['closed'])
        ticks[0]=10**15
        self.assertEqual(feed.view()['status'],'OBSERVED')
        service=AnalysisService(broker);service.observe(value)
        self.assertEqual(service.config['time_mode'],'mt5_raw_v1')
        self.assertNotIn('utc',service.proof)
        self.assertEqual(proof_status(service.proof,10**20),'READY')
        self.assertEqual(feed.disconnected()['status'],'DISCONNECTED')
    def test_disconnected_invalid_and_unverified_fail_closed(self):
        for changes in ({'connected':False},{'broker_time':float('nan')},{'timestamp_semantics':'UNVERIFIED'}):
            self.assertNotEqual(proof_status(dict(proof(),**changes),10),'READY')
    def test_raw_analyzer_never_labels_utc(self):
        bars=read_bars(API(),CONFIG,'M1',NOW+1)
        result=analyze_bars(bars,'M1',NOW+1)
        self.assertNotEqual(result['modules']['geometry']['status'],'ERROR')
        for marks in result['overlays'].values():
            self.assertTrue(all('utc_time' not in mark for mark in marks))

if __name__=='__main__': unittest.main()
