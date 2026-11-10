"""Offline diagnostics privacy, rate limits and owner-thread isolation."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from tradfi.context_diagnostics import project,VERSION
from tradfi.web_logs import DiagnosticLogs,SafeRotatingFileHandler

class Store:
    db=None
    def __init__(self):self.db=self;self.calls=0
    def execute(self,*args):self.calls+=1;return self
    def fetchall(self):return []

class VerboseLogsTest(unittest.TestCase):
    def test_redaction_and_pseudonyms(self):
        raw=dict(id='123456789',password='SECRET',reason='SUPER_SECRET',account={'login':987654},position_history=[dict(position_id=12345,net='3.14',complete=True)],metrics={'history_complete':False})
        result=project(raw);text=json.dumps(result)
        for secret in ('123456789','12345','987654','SECRET'):self.assertNotIn(secret,text)
        self.assertEqual(project(result),result)
        self.assertEqual(result['position_history'][0]['net'],'3.14')
    def test_heartbeat_and_no_tick_flood(self):
        logs=DiagnosticLogs();store=Store();clock=[0];logs.monotonic=lambda:clock[0]
        def capture(i):
            logs.capture('live',store,{},1800000000+i,dict(timestamp=i,quote={'bid':i},positions=[{'ticket':1,'profit':i}],feed={'sequence':i,'age_seconds':i,'status':'FRESH'}))
        capture(0)
        for i in range(1,60):clock[0]=i;capture(i)
        self.assertEqual(len(logs.recent['live']),1);self.assertEqual(store.calls,1)
        clock[0]=60;capture(60)
        self.assertEqual(len(logs.recent['live']),2);self.assertEqual(logs.recent['live'][-1]['context']['record_kind'],'HEARTBEAT')
        logs.capture('live',store,{},1800000061,{'phase':'UNKNOWN'})
        self.assertEqual(len(logs.recent['live']),3)
    def test_write_failure_and_slow_writer_do_not_block(self):
        gate=threading.Event();started=threading.Event()
        def slow(*args):started.set();gate.wait(2);raise OSError('SECRET')
        with tempfile.TemporaryDirectory() as directory,patch.object(SafeRotatingFileHandler,'emit',slow):
            logs=DiagnosticLogs(Path(directory)/'test.log')
            try:
                before=time.monotonic();logs.capture('live',Store(),{},1800000000)
                self.assertLess(time.monotonic()-before,.5);self.assertTrue(started.wait(1))
                for i in range(110):logs.capture('live',Store(),{'cycle':{'id':str(i)}},1800000000+i)
                self.assertGreater(logs.dropped,0)
            finally:gate.set();logs.close()
    def test_symlink_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.log';target=Path(directory)/'target';target.write_text('KEEP')
            Path(str(path)+'.live.diagnostic.jsonl').symlink_to(target)
            logs=DiagnosticLogs(path);logs.capture('live',Store(),{},1800000000);logs.close()
            self.assertEqual(target.read_text(),'KEEP')
    def test_diagnostic_exception_isolated(self):
        logs=DiagnosticLogs();store=Store()
        with patch.object(store,'execute',side_effect=RuntimeError('SECRET')):logs.capture('live',store,{},1800000000)
        self.assertEqual(logs.failures,1)
        self.assertNotIn('SECRET',logs.document('live'))

    def test_recent_history_and_bounded_record(self):
        rows=[{'position_id':str(i),'net':str(i)} for i in range(200)]
        self.assertEqual(project({'position_history':rows})['position_history'][0]['net'],'0')
        logs=DiagnosticLogs()
        huge={'groups':[{'tickets':[{'entry':'1'*70,'sl':'2'*70,'tp':'3'*70} for _ in range(100)]} for _ in range(100)]}
        logs.capture('live',Store(),{'cycle':huge},1800000000)
        record=logs.recent['live'][-1]
        self.assertLessEqual(len(json.dumps(record,ensure_ascii=False).encode()),60000)
        self.assertTrue(record['context']['truncated'])
