"""Offline analysis source and transport tests. Never imports a live MT5 binding."""
import json
import queue
import sys
import os
import struct
import threading
import multiprocessing
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tradfi.analysis_service import (AnalysisService, TIMEFRAMES, calculate,
    empty_snapshot, proof_status, read_bars)
from tradfi.control import OwnerMailbox
from tradfi.web_control import WebTransport

NOW = 1800000000  # H1 boundary
CONFIG = dict(symbol='XAUUSD.s', expected_login=123, expected_server='test',
              terminal_path='C:/mt5/terminal64.exe', time_mode='fixed', offset=7200)

def proof():
    return dict(connected=True, status='FRESH', timestamp_semantics='VERIFIED',
                monotonic=10, utc=NOW, age=0, max_age=5, instance='one')

def partial_hang_worker(config, incoming, outgoing):
    # Real POSIX Connection frame header, then a permanently incomplete body.
    incoming.get()
    os.write(outgoing.fileno(), struct.pack('!i', 1000000) + b'x' * 100)
    time.sleep(30)


def partial_crash_worker(config, incoming, outgoing):
    incoming.get()
    os.write(outgoing.fileno(), struct.pack('!i', 1000000) + b'x' * 100)
    os._exit(1)


def complete_large_worker(config, incoming, outgoing):
    p=incoming.get()
    values={}
    for tf,duration in TIMEFRAMES.items():
        value=empty_snapshot(config['symbol'],tf,'READY')
        value.update(feed_instance=p['instance'],closed_through=NOW)
        value['bars']=[dict(raw_time=NOW+7200-duration*i,utc_time=NOW-duration*i,
                            open=10,high=12,low=9,close=11,tick_volume=10,real_volume=0)
                       for i in range(500,0,-1)]
        values[tf]=value
    outgoing.send_bytes(json.dumps(values).encode())
    incoming.get()
    outgoing.close()


class API:
    TIMEFRAME_M1=1; TIMEFRAME_M15=15; TIMEFRAME_M30=30; TIMEFRAME_H1=60
    def __init__(self): self.calls=[]; self.login=123; self.rows=None;self.now=NOW
    def account_info(self): return SimpleNamespace(login=self.login, server='test')
    def terminal_info(self): return SimpleNamespace(connected=True,path='C:/mt5')
    def copy_rates_from_pos(self,symbol,tf,start,count):
        self.calls.append((symbol,tf,start,count))
        return self.rows if self.rows is not None else [dict(time=int((self.now+7200)//(tf*60))*tf*60-tf*60*i,open=10,high=12,low=9,close=11,tick_volume=5,real_volume=0) for i in range(5,0,-1)]

class SourceTests(unittest.TestCase):
    def test_exact_native_timeframes_and_offset(self):
        api=API(); seen=[]
        def analyze(bars,tf,asof):seen.append(tf);return {}
        result=calculate(api,CONFIG,proof(),{},mono=10,analyzer=analyze)
        self.assertEqual(set(seen),set(TIMEFRAMES))
        self.assertEqual([x[1] for x in api.calls],[1,15,30,60])
        for tf,item in result.items():
            self.assertEqual(item['status'],'READY');self.assertEqual(item['closed_through'],NOW)
            self.assertEqual(item['bars'][-1]['raw_time']-item['bars'][-1]['utc_time'],7200)
            self.assertEqual(item['as_of'],NOW)
        self.assertTrue(all(x[0]=='XAUUSD.s' and x[2:]==(1,500) for x in api.calls))
        json.dumps(result,allow_nan=False)
        def unexpected(*a):raise AssertionError('Cached candles recalculated')
        again=calculate(api,CONFIG,proof(),result,mono=11,analyzer=unexpected)
        self.assertEqual(again['M1']['as_of'],NOW)
    def test_native_reads_only_on_each_timeframe_closed_boundary(self):
        api=API();p=proof();retry={};analyzer=lambda *a:{}
        cache=calculate(api,CONFIG,p,{},mono=10,analyzer=analyzer,retry_state=retry)
        self.assertEqual(len(api.calls),4)
        cache=calculate(api,CONFIG,p,cache,mono=11,analyzer=analyzer,retry_state=retry)
        self.assertEqual(len(api.calls),4)
        api.now=NOW+60;p['utc']=api.now;p['monotonic']=12
        cache=calculate(api,CONFIG,p,cache,mono=12,analyzer=analyzer,retry_state=retry)
        self.assertEqual([call[1] for call in api.calls],[1,15,30,60,1])
        api.now=NOW+900;p['utc']=api.now;p['monotonic']=13
        cache=calculate(api,CONFIG,p,cache,mono=13,analyzer=analyzer,retry_state=retry)
        self.assertEqual([call[1] for call in api.calls][-2:],[1,15])
        self.assertEqual(len(api.calls),7)
        p['instance']='new'
        calculate(api,CONFIG,p,cache,mono=13,analyzer=analyzer,retry_state=retry)
        self.assertEqual(len(api.calls),11)

    def test_unavailable_history_retries_no_faster_than_five_seconds(self):
        api=API();api.rows=[];p=proof();retry={}
        cache=calculate(api,CONFIG,p,{},mono=10,retry_state=retry)
        self.assertEqual(len(api.calls),4)
        calculate(api,CONFIG,p,cache,mono=11,retry_state=retry)
        self.assertEqual(len(api.calls),4)
        p['monotonic']=15;p['utc']=NOW+5
        calculate(api,CONFIG,p,cache,mono=15,retry_state=retry)
        self.assertEqual(len(api.calls),8)

    def test_real_adapter_contract_accepts_fractional_observation_time(self):
        p=proof();p['utc']=NOW+.125
        result=calculate(API(),CONFIG,p,{},mono=10)
        self.assertEqual(result['M1']['status'],'READY')
        self.assertNotIn('INVALID_INPUT',json.dumps(result))
        self.assertEqual(result['M1']['as_of'],NOW+.125)

    def test_wrong_terminal_directory_fails_closed(self):
        api=API();api.terminal_info=lambda:SimpleNamespace(connected=True,path='C:/other')
        result=calculate(api,CONFIG,proof(),{},mono=10)
        self.assertEqual(result['M1']['status'],'DISCONNECTED')
        self.assertEqual(api.calls,[])

    def test_missing_invalid_future_and_partial(self):
        api=API(); good=api.copy_rates_from_pos('XAUUSD.s',1,1,500)
        for rows in ([], [dict(good[0],high=8)], [dict(good[0],close=float('nan'))],
                     [good[0],good[0]], list(reversed(good)), [dict(good[0],time=NOW+7260)],
                     [dict(good[0],time=NOW+7201)]):
            api.rows=rows
            with self.assertRaises(ValueError):read_bars(api,CONFIG,'M1',NOW)
        api.rows=good+[dict(good[-1],time=NOW+7200)]
        self.assertEqual(len(read_bars(api,CONFIG,'M1',NOW)),len(good))
    def test_identity_and_time_fail_closed_without_history_read(self):
        api=API();api.login=999
        result=calculate(api,CONFIG,proof(),{},mono=10,analyzer=lambda *a:{})
        self.assertEqual(result['M1']['status'],'DISCONNECTED');self.assertEqual(api.calls,[])
        for change,status in (({'connected':False},'DISCONNECTED'),({'timestamp_semantics':'UNVERIFIED'},'TIME_UNVERIFIED'),({'status':'STALE'},'STALE')):
            p=proof();p.update(change)
            self.assertEqual(calculate(api,CONFIG,p,{},mono=10)['H1']['status'],status)
        self.assertEqual(proof_status(proof(),16),'STALE')
        self.assertEqual(proof_status(proof(),9),'TIME_UNVERIFIED')
    def test_gap_is_stale_and_never_fabricated(self):
        api=API();api.rows=api.copy_rates_from_pos('XAUUSD.s',60,1,500)[:-1]
        bars=read_bars(api,CONFIG,'H1',NOW)
        self.assertEqual(len(bars),4)
        self.assertEqual(calculate(api,CONFIG,proof(),{},mono=10,analyzer=lambda *a:{})['H1']['status'],'STALE')
    def test_slow_analyzer_invalidates_proof_before_ready(self):
        p=proof();p['max_age']=.001
        def slow(*a): time.sleep(.005);return {}
        result=calculate(API(),CONFIG,p,{},mono=10,analyzer=slow)
        self.assertEqual(result['M1']['status'],'STALE')

    def test_exception_sanitized(self):
        api=API()
        def bad(*a):raise RuntimeError('secret login 123')
        result=calculate(api,CONFIG,proof(),{},mono=10,analyzer=bad)
        self.assertNotIn('secret',json.dumps(result));self.assertEqual(result['M1']['status'],'UNAVAILABLE')

class ServiceTests(unittest.TestCase):
    def broker(self):return SimpleNamespace(path='fake',symbol='XAUUSD.s',login=123,account='123@test',server='test',market_time_offset_seconds=7200,max_age=5,api=SimpleNamespace(is_test_binding=True))
    def test_lazy_no_process_or_owner_io(self):
        with patch('multiprocessing.get_context') as context:
            service=AnalysisService(self.broker())
            self.assertIsNone(service.thread)
            service.observe(dict(connected=True,status='FRESH',timestamp_semantics='VERIFIED',timestamp=NOW,age_seconds=0,instance='one'))
            self.assertEqual(service.view('M15')['status'],'UNAVAILABLE')
            context.return_value.Process.assert_not_called()
            service.close()
    def test_timeframe_rollover_marks_old_cache_stale(self):
        service=AnalysisService(self.broker());service.enabled=True;service.thread=object()
        p=proof();p['utc']=NOW+61;p['monotonic']=time.monotonic();service.proof=p
        item=empty_snapshot('XAUUSD.s','M1','READY')
        item.update(bars=[{'raw_time':NOW+7200-60,'utc_time':NOW-60}],closed_through=NOW)
        service.cache={'M1':item}
        self.assertEqual(service.view('M1')['status'],'STALE')

    def test_config_allowlist_and_no_credentials(self):
        b=self.broker();b.config={'password':'secret','store':'private'}
        service=AnalysisService(b)
        self.assertEqual(set(service.config),{'terminal_path','symbol','expected_login','expected_server','time_mode','offset'})
        self.assertNotIn('secret',str(service.config))
    def test_cached_result_expires_without_owner_tick(self):
        service=AnalysisService(self.broker());service.enabled=True
        service.thread=object() # no actual supervisor
        service.proof=proof();service.proof['monotonic']=time.monotonic()-6
        service.cache={'M1':empty_snapshot('XAUUSD.s','M1','READY')}
        self.assertEqual(service.view('M1')['status'],'STALE')


    def test_real_large_complete_snapshot_crosses_pipe(self):
        service=AnalysisService(self.broker(),timeout=3)
        service.enabled=True;service.worker_target=complete_large_worker
        service.observe(dict(connected=True,status='FRESH',timestamp_semantics='VERIFIED',timestamp=NOW,age_seconds=0,instance='one'))
        service.view('M1');thread=service.thread;deadline=time.monotonic()+4
        while not service.cache and time.monotonic()<deadline:time.sleep(.02)
        try:
            self.assertEqual(set(service.cache),set(TIMEFRAMES))
            self.assertTrue(all(len(value['bars'])==500 for value in service.cache.values()))
            self.assertEqual(service.view('M1')['status'],'READY')
        finally:service.close()
        self.assertFalse(thread.is_alive())

    @unittest.skipIf(os.name == 'nt', 'POSIX partial-frame injection; normal IPC is cross-platform')
    def test_real_partial_result_hang_and_crash_release_reader(self):
        for target in (partial_hang_worker, partial_crash_worker):
            service=AnalysisService(self.broker(),timeout=.5)
            service.enabled=True;service.worker_target=target
            service.observe(dict(connected=True,status='FRESH',timestamp_semantics='VERIFIED',timestamp=NOW,age_seconds=0,instance='one'))
            baseline={t.ident for t in threading.enumerate() if t.name=='analysis-result-reader'}
            service.view('M1')
            thread=service.thread
            deadline=time.monotonic()+4
            while service.failure is None and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(service.failure,'UNAVAILABLE')
            started=time.monotonic();service.close()
            self.assertLess(time.monotonic()-started,2)
            self.assertFalse(thread.is_alive())
            self.assertEqual({t.ident for t in threading.enumerate() if t.name=='analysis-result-reader'},baseline)

    @unittest.skipIf(os.name == 'nt', 'POSIX partial-frame injection')
    def test_real_crashes_have_three_attempt_limit_and_no_orphans(self):
        class Context:
            def __init__(self):self.base=multiprocessing.get_context('spawn');self.processes=[]
            def Queue(self,**kw):return self.base.Queue(**kw)
            def Pipe(self,**kw):return self.base.Pipe(**kw)
            def Process(self,**kw):
                process=self.base.Process(**kw);self.processes.append(process);return process
        class FastBackoff:
            def __init__(self):self.event=threading.Event()
            def is_set(self):return self.event.is_set()
            def set(self):self.event.set()
            def wait(self,delay):return self.event.wait(.01 if delay==5 else delay)
        context=Context();service=AnalysisService(self.broker(),context=context,timeout=1)
        service.enabled=True;service.worker_target=partial_crash_worker;service.stop=FastBackoff()
        service.observe(dict(connected=True,status='FRESH',timestamp_semantics='VERIFIED',timestamp=NOW,age_seconds=0,instance='one'))
        service.view('M1');thread=service.thread;thread.join(timeout=5)
        service.close()
        self.assertFalse(thread.is_alive());self.assertEqual(len(context.processes),3)
        self.assertTrue(all(not process.is_alive() for process in context.processes))
        self.assertGreater(service.retry_after,time.monotonic())

    def test_optional_startup_failure_does_not_break_panel(self):
        from tradfi.web_control import NativePanel
        broker=self.broker();broker.config={}
        controller=SimpleNamespace(broker=broker,state={'web_store_id':'existing'},action=lambda *a:None)
        with patch('tradfi.analysis_service.AnalysisService',side_effect=RuntimeError('optional')):
            panel=NativePanel(controller)
        self.assertEqual(panel.analysis.view('H1')['status'],'UNAVAILABLE')
        panel.close()

    def test_worker_only_initializes_own_binding_and_closes(self):
        from tradfi.analysis_service import _worker
        api=API();calls=[]
        api.initialize=lambda *args,**kw:calls.append(('initialize',args,kw)) or True
        api.shutdown=lambda:calls.append(('shutdown',))
        class Output:
            def __init__(self):self.messages=[]
            def send_bytes(self,value):self.messages.append(value)
            def close(self):pass
        incoming=queue.Queue();outgoing=Output();incoming.put(proof());incoming.put(None)
        with patch.dict(os.environ,{}), patch.dict(sys.modules,{'MetaTrader5':api}), patch('tradfi.analysis_service.calculate',return_value={}):
            _worker(CONFIG,incoming,outgoing)
            self.assertEqual(os.environ['OMP_NUM_THREADS'],'1')
            self.assertEqual(os.environ['NUMBA_NUM_THREADS'],'1')
        self.assertEqual(calls,[('initialize',('C:/mt5/terminal64.exe',),{'timeout':10000}),('shutdown',)])
        self.assertEqual(json.loads(outgoing.messages[0]),{})

class RouteTests(unittest.TestCase):
    def test_analysis_auth_enum_and_read_only_route(self):
        class Cache:
            calls=[]
            def view(self,tf):self.calls.append(tf);return empty_snapshot('XAUUSD.s',tf)
        cache=Cache();box=OwnerMailbox();transport=WebTransport(box,password='offline-test-secret'*2,analysis=cache)
        headers={'Host':'127.0.0.1:8765','X-Local-Control':'1','Content-Type':'application/json'}
        self.assertEqual(transport.request('GET','/api/analysis?timeframe=M1',headers)[0],401)
        _,_,extra=transport.request('POST','/api/login',headers,json.dumps({'password':'offline-test-secret'*2}).encode())
        headers['Cookie']=extra['Set-Cookie'].split(';')[0]
        for tf in TIMEFRAMES:
            code,value,_=transport.request('GET','/api/analysis?timeframe='+tf,headers)
            self.assertEqual(code,200);self.assertEqual(value['timeframe'],tf)
        for query in ('','timeframe=M5','timeframe=M1&timeframe=H1','timeframe=M1&symbol=EVIL','timeframe=','timeframe=M1&x=1&y=2'):
            self.assertEqual(transport.request('GET','/api/analysis?'+query,headers)[0],400)
        self.assertEqual(transport.request('POST','/api/analysis?timeframe=M1',headers,b'{}')[0],404)
        self.assertEqual(transport.request('GET','/api/analysis?timeframe=M1',dict(headers,Origin='https://evil.invalid'))[0],403)
        self.assertTrue(box.requests.empty())
