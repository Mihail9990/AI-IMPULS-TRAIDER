"""Separate interpreter transport tests. No terminal binding or connection."""
import io
import json
import os
from pathlib import Path
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
from types import SimpleNamespace
from tradfi.analysis_process import validate_python, clean_environment, ExternalWorker, FrameReader, CommandWriter
from tradfi.analysis_service import _read_results


class ProcessContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.exe=self.root/'Scripts'/'python.exe';self.exe.parent.mkdir();self.exe.write_bytes(b'offline test only')
        (self.root/'pyvenv.cfg').write_text('version = 3.12.14\ninclude-system-site-packages = false\n')
    def tearDown(self):self.temp.cleanup()
    def test_exact_isolated_venv_validation(self):
        self.assertEqual(validate_python(str(self.exe)),str(self.exe))
        for value in ('python',str(self.root/'missing.exe'),str(self.root)):
            with self.assertRaises(ValueError):validate_python(value)
        (self.root/'pyvenv.cfg').write_text('version = 3.13.5\ninclude-system-site-packages = false\n')
        with self.assertRaises(ValueError):validate_python(str(self.exe))
        (self.root/'pyvenv.cfg').write_text('version = 3.12.14\ninclude-system-site-packages = true\n')
        with self.assertRaises(ValueError):validate_python(str(self.exe))
    def test_subprocess_argv_isolated_no_shell_no_environment_contamination(self):
        fake=MagicMock();fake.stdin=io.BytesIO();fake.stdout=io.BytesIO()
        with patch.dict(os.environ,{'PYTHONPATH':'evil','PYTHONHOME':'evil','PIP_INDEX_URL':'evil','OMP_NUM_THREADS':'99'}),patch('subprocess.Popen',return_value=fake) as popen:
            process=ExternalWorker(str(self.exe),{'symbol':'offline'});process.start()
            args,kw=popen.call_args
            self.assertEqual(args[0][:3],[str(self.exe),'-I','-u'])
            self.assertFalse(kw['shell']);self.assertEqual(kw['stderr'],subprocess.DEVNULL)
            self.assertNotIn('PYTHONPATH',kw['env']);self.assertNotIn('PYTHONHOME',kw['env']);self.assertNotIn('PIP_INDEX_URL',kw['env'])
            self.assertEqual(kw['env']['OMP_NUM_THREADS'],'1');self.assertEqual(os.environ['OMP_NUM_THREADS'],'99')
    def test_frame_bounds_and_command_limits(self):
        reader=FrameReader(io.BytesIO(struct.pack('!I',100)+b'x'))
        with self.assertRaises(EOFError):reader.recv_bytes(1000)
        with self.assertRaises(ValueError):FrameReader(io.BytesIO(struct.pack('!I',1001))).recv_bytes(1000)
        writer=CommandWriter(io.BytesIO())
        with self.assertRaises(ValueError):writer.put_nowait({'value':'x'*3000})
    def test_actual_subprocess_partial_stdout_releases_reader_after_kill(self):
        # This checks the very same FrameReader used by ExternalWorker, using
        # a synthetic process that never imports any trading/native binding.
        script=self.root/'fake.py'
        script.write_text("import sys,struct,time\nsys.stdout.buffer.write(struct.pack('!I',1000000)+b'x'*100)\nsys.stdout.buffer.flush()\ntime.sleep(30)\n")
        child=subprocess.Popen([sys.executable,'-I','-u',str(script)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        completed=queue.Queue(maxsize=1)
        reader=threading.Thread(target=_read_results,args=(FrameReader(child.stdout),completed),daemon=True)
        reader.start();time.sleep(.1)
        self.assertTrue(reader.is_alive())
        child.kill();child.wait(timeout=2);reader.join(timeout=2)
        self.assertFalse(reader.is_alive());self.assertFalse(completed.get_nowait()[0])
    def test_external_service_kills_closed_stdin_hung_child(self):
        from tradfi.analysis_service import AnalysisService
        script=self.root/'closed_stdin.py'
        script.write_text("import sys,os,struct,time\nsys.stdin.buffer.readline()\nos.close(0)\nsys.stdout.buffer.write(struct.pack('!I',1000000)+b'x'*100)\nsys.stdout.buffer.flush()\ntime.sleep(30)\n")
        original=subprocess.Popen;children=[]
        def launch(args,**kwargs):
            child=original([sys.executable,'-I','-u',str(script)],**kwargs);children.append(child);return child
        broker=SimpleNamespace(path='C:/mt5/terminal.exe',symbol='XAUUSD.s',login=123,account='123@test',server='test',market_time_offset_seconds=0,max_age=5,api=SimpleNamespace(),config={'analysis_python':str(self.exe)})
        service=AnalysisService(broker,timeout=.3)
        service.observe(dict(connected=True,status='FRESH',timestamp_semantics='VERIFIED',timestamp=1800000000,age_seconds=0,instance='one'))
        with patch('subprocess.Popen',side_effect=launch):
            service.view('M1');thread=service.thread;deadline=time.monotonic()+3
            while service.failure is None and time.monotonic()<deadline:time.sleep(.02)
            service.close()
        self.assertEqual(service.failure,'UNAVAILABLE');self.assertFalse(thread.is_alive())
        self.assertTrue(children);self.assertTrue(all(child.poll() is not None for child in children))
        self.assertFalse(any(t.name=='analysis-result-reader' and t.is_alive() for t in threading.enumerate()))
    def test_external_start_failure_cleans_up_created_process(self):
        original=subprocess.Popen;children=[]
        def launch(args,**kwargs):
            child=original([sys.executable,'-I','-c','import time;time.sleep(30)'],**kwargs);children.append(child);return child
        with patch('subprocess.Popen',side_effect=launch),patch.object(CommandWriter,'put_nowait',side_effect=BrokenPipeError):
            with self.assertRaises(BrokenPipeError):ExternalWorker(str(self.exe),{}).start()
        self.assertTrue(all(child.poll() is not None and child.stdin.closed and child.stdout.closed for child in children))
    def test_missing_environment_fails_analysis_only_with_safe_instruction(self):
        from tradfi.analysis_service import AnalysisService
        broker=SimpleNamespace(path='terminal',symbol='XAUUSD.s',login=123,account='123@test',server='test',market_time_offset_seconds=0,max_age=5,api=SimpleNamespace(),config={'analysis_python':'relative-or-invalid'})
        service=AnalysisService(broker);value=service.view('M1')
        self.assertFalse(service.enabled);self.assertEqual(value['reason'],'ANALYSIS_ENVIRONMENT_UNAVAILABLE')
        self.assertIn('SETUP_ANALYSIS.cmd',value['notes'][0]);self.assertIsNone(service.thread)

    def test_unc_rejected_before_any_filesystem_access(self):
        with patch.object(Path,'is_file',side_effect=AssertionError('filesystem must not be touched')):
            for value in ('//server/share/python.exe',r'\\server\share\python.exe'):
                with self.assertRaises(ValueError):validate_python(value)
    def test_managed_target_uses_only_valid_current_release(self):
        from tools.setup_analysis import resolve_target
        sha='a'*40;code=self.root/'releases'/sha/'code';code.mkdir(parents=True)
        pointer=self.root/'current.json';pointer.write_text(json.dumps({'current':sha,'previous':None}))
        (code/'independent-install-contract.json').write_text(json.dumps({'format':2,'roles':['live'],'layout':'independent_roots_v1'}))
        for name in ('requirements-analysis.txt','tools/check_analysis_dependencies.py','tradfi/analysis_process.py','tools/analysis_worker.py'):
            file=code/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text('offline fixture')
        target,binding=resolve_target(str(self.root),Path('/unused'))
        self.assertEqual(target,code);self.assertEqual(binding,(pointer,sha))
        self.assertEqual(json.loads(pointer.read_text())['current'],sha)
        pointer.write_text(json.dumps({'current':'../../outside'}))
        with self.assertRaises(ValueError):resolve_target(str(self.root),Path('/unused'))
    def test_windows_injected_binding_never_selects_external_python(self):
        from tradfi.analysis_service import AnalysisService
        broker=SimpleNamespace(path='terminal',symbol='XAUUSD.s',login=123,account='123@test',server='test',market_time_offset_seconds=0,max_age=5,api=SimpleNamespace(is_test_binding=True),config={})
        with patch('tradfi.analysis_service.os.name','nt'):
            service=AnalysisService(broker)
        self.assertIsNone(service.external_python);self.assertFalse(service.enabled)

    def test_setup_refuses_non_windows_without_touching_environment(self):
        from tools.setup_analysis import main
        with patch('sys.platform','linux'),patch('subprocess.run') as run:
            self.assertEqual(main(),2);run.assert_not_called()
