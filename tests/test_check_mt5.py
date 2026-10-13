"""Offline Windows-launcher contract: no terminal, network, credentials or trade calls."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('check_mt5_tool',ROOT/'tools'/'check_mt5.py')
launcher=importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)


def response(data=None,code=0,stderr=''):
    return SimpleNamespace(returncode=code,stdout=json.dumps(data) if data is not None else '',stderr=stderr)


def good():
    return dict(inspect_status='OK',mode='READ_ONLY',adapter_trading_enabled=False,execution_ready=False,
        history_complete=False,connected=True,hedging=True,currency='UST',market_data_time_status='VERIFIED',
        orders=0,positions=0,quote_time=dict(age_s=.055,offset_seconds=10800),
        reconciliation=dict(execution_ready=False,history_complete=False,journal_source='none',
            discovered_orders=276,discovered_deals=274,unavailable_sources=0,foreign_order_count=0,foreign_position_count=0))


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='проверка spaces ! '); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=json.loads((ROOT/'tradfi_config.example.json').read_text())
        self.path=self.root/'inspect.json'; self.path.write_text(json.dumps(self.config))
    def test_fixed_child_arguments_even_if_config_requests_trading(self):
        self.config.update(enable_trading=True,execution_contract='cycle_scoped_identity_v1',token='PRIVATE')
        self.path.write_text(json.dumps(self.config)); before=self.path.read_bytes()
        calls=[]
        def run(args,**kwargs):
            calls.append((args,kwargs)); return response() if '-c' in args else response(good())
        result=launcher.check(self.root,run=run,platform='win32')
        self.assertEqual(result['status'],'WARN'); self.assertEqual(result['read_only_checks'],'PASS')
        args,kwargs=calls[1]
        self.assertEqual(args[2:6],['--mode','mt5','--inspect-evidence','--config'])
        self.assertNotIn('--execute',args); self.assertNotIn('--service',args); self.assertNotIn('--journal-snapshot',args)
        self.assertEqual(kwargs['cwd'],str(self.root)); self.assertTrue(kwargs['capture_output'])
        self.assertEqual(before,self.path.read_bytes()); self.assertEqual(list(self.root.iterdir()),[self.path])
    def test_raw_child_errors_never_escape(self):
        secret='account=777 token=PRIVATE path=C:/private'
        outputs=iter([response(),response(code=2,stderr=secret)])
        result=launcher.check(self.root,run=lambda *a,**kw:next(outputs),platform='win32')
        self.assertEqual(result['status'],'FAIL'); self.assertNotIn(secret,json.dumps(result))
    def test_dependency_failure_stops_before_mt5_inspection(self):
        calls=[]
        def run(*args,**kwargs): calls.append(args); return response(code=1,stderr='PRIVATE')
        result=launcher.check(self.root,run=run,platform='win32')
        self.assertEqual(result['code'],'MT5_DEPENDENCY_MISSING'); self.assertEqual(len(calls),1)
        self.assertNotIn('PRIVATE',json.dumps(result))
    def test_timeout_and_missing_or_invalid_config_fail_safely(self):
        with patch.object(launcher.subprocess,'run') as unused:
            result=launcher.check(self.root,run=lambda *a,**kw:(_ for _ in ()).throw(subprocess.TimeoutExpired('PRIVATE',1)),platform='win32')
        self.assertEqual(result['code'],'CHECK_TIMEOUT')
        self.path.write_text('PRIVATE invalid JSON'); self.assertEqual(launcher.check(self.root,platform='win32')['code'],'CONFIG_INVALID')
        self.path.unlink(); self.assertEqual(launcher.check(self.root,platform='win32')['code'],'CONFIG_MISSING')
    def test_allowlist_drops_private_extra_fields_and_rejects_false_readiness(self):
        data=good(); data.update(account='PRIVATE',token='PRIVATE',spec={'private':'PRIVATE'})
        self.assertNotIn('PRIVATE',json.dumps(launcher.summarize(data)))
        for key in ('execution_ready','adapter_trading_enabled','history_complete'):
            changed=dict(data,**{key:True}); self.assertEqual(launcher.summarize(changed)['status'],'FAIL')
        data['quote_time']['age_s']=float('nan'); self.assertEqual(launcher.summarize(data)['status'],'FAIL')
    def test_report_is_new_timestamped_utf8_crlf_and_never_overwrites(self):
        report=launcher.summarize(good())
        with patch.object(launcher.uuid,'uuid4',return_value=SimpleNamespace(hex='a'*32)), patch.object(launcher,'datetime') as clock:
            from datetime import datetime,timezone
            clock.now.return_value=datetime(2026,10,12,18,0,tzinfo=timezone.utc)
            path=launcher.write_report(self.root,report); before=path.read_bytes()
            with self.assertRaises(FileExistsError): launcher.write_report(self.root,report)
        self.assertEqual(before,path.read_bytes()); self.assertIn(b'\r\n',before)
        self.assertIn('execution_ready',before.decode('utf-8')); self.assertIn('20261012_180000',path.name)
    def test_bat_uses_own_directory_crlf_and_never_service_or_install(self):
        raw=(ROOT/'check_mt5.cmd').read_bytes(); self.assertNotIn(b'\n',raw.replace(b'\r\n',b''))
        text=raw.decode('ascii'); self.assertIn('cd /d "%~dp0"',text); self.assertIn('DisableDelayedExpansion',text)
        for forbidden in ('--execute','--service','pip install','order_send','TRADFI_TELEGRAM'):
            self.assertNotIn(forbidden,text)
        self.assertIn('pause',text); self.assertIn('CreateNew',text)
    def test_actual_inspect_entrypoint_is_stateless_and_never_sends(self):
        from test_tradfi_native_service import NativeAPI
        from tradfi.runtime import _run_mt5
        api=NativeAPI(); current=int(api.now//60)*60
        api.rates=[dict(time=t,close=4000.) for t in (current-120,current-60,current)]
        config=dict(self.config,enable_trading=True)
        output=io.StringIO()
        with patch('tradfi.runtime.Store',side_effect=AssertionError('SQLite forbidden')),redirect_stdout(output):
            _run_mt5(config,True,evidence=True,api=api,clock=lambda:api.now,monotonic=lambda:api.now)
        report=launcher.summarize(json.loads(output.getvalue()))
        self.assertEqual(report['status'],'WARN'); self.assertFalse(api.sends); self.assertFalse(api.checks)
        self.assertEqual(api.shutdowns,1)

if __name__=='__main__': unittest.main()
