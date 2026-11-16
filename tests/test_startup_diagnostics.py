import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import startup_diagnostics as d
import install_tradfi as installer
from tradfi.startup_errors import reason,write_failure

class API:
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING=2
    TIMEFRAME_M1=1
    def __init__(self):
        self.calls=[];self.initialized=True
        self.terminal=NS(connected=True,path=r'C:\MT5')
        self.account=NS(login=987654321,server='Bybit-Live-6',currency='UST',margin_mode=2)
        self.symbol=NS(visible=True,currency_profit='USD',trade_calc_mode=0,trade_contract_size=100.,trade_tick_size=.01,trade_tick_value_profit=1.,trade_tick_value_loss=1.,volume_min=.01,volume_step=.01,volume_max=100.,point=.01,trade_stops_level=0,trade_freeze_level=0)
        self.tick=NS(bid=4000.,ask=4000.1,time=1800000000,time_msc=1800000000100)
        self.bars=[{'time':1799999940},{'time':1800000000}]
    def initialize(self,*args,**kwargs):self.calls.append('initialize');assert kwargs=={'timeout':10000};return self.initialized
    def shutdown(self):self.calls.append('shutdown')
    def terminal_info(self):return self.terminal
    def account_info(self):return self.account
    def symbol_info(self,s):assert s=='XAUUSD.s';return self.symbol
    def symbol_info_tick(self,s):assert s=='XAUUSD.s';return self.tick
    def copy_rates_from_pos(self,*args):return self.bars
    def __getattr__(self,name):
        raise AssertionError('Unexpected API capability: '+name)

class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'data').mkdir();self.output=self.root/'data'/'startup-diagnostic.txt'
        self.config=self.root/'data'/'service.json';self.report=d.Report(self.output);self.api=API()
        self.cfg=dict(backend='mt5_direct_v1',expected_login=987654321,expected_server='Bybit-Live-6',
            terminal_path=r'C:\MT5\terminal64.exe',time_profile='bybit_live6_time_v1',symbol='XAUUSD.s',
            enable_trading=False,web=dict(password='fixture-only-password-value-not-user',host='127.0.0.1',port=8765),
            state_file=str(self.root/'data'/'state.sqlite3'))
        self.config.write_text(json.dumps(self.cfg));self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.root)},clear=True)
        self.env.start();self.addCleanup(self.env.stop)
    def text(self):return self.output.read_text(encoding='utf-8-sig')
    def check(self):return d.check_config(self.cfg,self.report,exists=lambda p:True,environ={})
    def probe(self):return d.probe(self.cfg,self.report,api=self.api,busy=lambda *a:False,clock=lambda:1800000001)
    def runprobe(self):return d.run(self.root,self.output,api=self.api,busy=lambda *a:False,exists=lambda p:True,clock=lambda:1800000001)
    def test_success_zero_mutations_no_db(self):
        self.assertTrue(self.runprobe());self.assertEqual(self.api.calls,['initialize','shutdown'])
        self.assertFalse(Path(self.cfg['state_file']).exists());self.assertIn('INFO COMPLETE:',self.text())
        for value in (self.cfg['web']['password'],str(self.cfg['expected_login']),self.cfg['terminal_path']):self.assertNotIn(value,self.text())
    def test_json_syntax_redacted(self):
        self.config.write_text('{"password":"SECRET-IN-BAD-JSON"')
        self.assertFalse(self.runprobe());self.assertIn('CONFIG_JSON',self.text());self.assertNotIn('SECRET-IN-BAD-JSON',self.text());self.assertFalse(self.api.calls)
    def test_invalid_terminal_file(self):
        self.assertFalse(d.check_config(self.cfg,self.report,exists=lambda p:False,environ={}))
        self.assertIn('TERMINAL_FILE',self.text())
    def test_terminal_not_executable(self):
        self.cfg['terminal_path']=r'C:\MT5\other.exe';self.assertFalse(self.check())
    def test_login_string(self):
        self.cfg['expected_login']='987654321';self.assertFalse(self.check());self.assertIn('CONFIG_LOGIN',self.text())
    def test_invalid_password(self):
        self.cfg['web']['password']='tiny';self.assertFalse(self.check());self.assertNotIn('tiny',self.text())
    def test_public_short_password_rejected(self):
        self.cfg['web'].update(password='short',public_origin='https://1.1.1.1');self.assertFalse(self.check())
    def test_environment_empty_no_fallback(self):
        self.assertFalse(d.check_config(self.cfg,self.report,exists=lambda p:True,environ={'TRADFI_WEB_PASSWORD':''}))
    def test_bad_profile(self):
        self.cfg['time_profile']='guess';self.assertFalse(self.check());self.assertIn('TIME_PROFILE',self.text())
    def test_port_busy_no_native_connection(self):
        self.assertFalse(d.probe(self.cfg,self.report,api=self.api,busy=lambda *a:True));self.assertFalse(self.api.calls)
    def test_initialize_failure(self):
        self.api.initialized=False;self.assertFalse(self.probe());self.assertIn('MT5_INITIALIZE',self.text());self.assertEqual(self.api.calls[-1],'shutdown')
    def test_disconnected(self):
        self.api.terminal.connected=False;self.assertFalse(self.probe());self.assertIn('MT5_DISCONNECTED',self.text())
    def test_wrong_mt5_path(self):
        self.api.terminal.path=r'C:\Other';self.assertFalse(self.probe());self.assertIn('MT5_PATH_MISMATCH',self.text())
    def test_account_mismatch_no_actual_account_logged(self):
        self.api.account.login=4444433333;self.assertFalse(self.probe());self.assertIn('MT5_ACCOUNT_MISMATCH',self.text());self.assertNotIn('4444433333',self.text())
    def test_server_mismatch(self):
        self.api.account.server='private-server';self.assertFalse(self.probe());self.assertNotIn('private-server',self.text())
    def test_currency_mismatch(self):
        self.api.account.currency='USD';self.assertFalse(self.probe());self.assertIn('MT5_CURRENCY_MODE',self.text())
    def test_nonhedging_readonly_warning(self):
        self.api.account.margin_mode=0;self.assertTrue(self.probe());self.assertIn('WARN NON_HEDGING',self.text())
    def test_hidden_symbol(self):
        self.api.symbol.visible=False;self.assertFalse(self.probe());self.assertIn('MT5_SYMBOL',self.text())
    def test_stale_tick_warn_not_guessed_exit_cause(self):
        self.cfg['market_time_mode']='fixed_utc_v1'
        self.api.tick.time-=100;self.api.tick.time_msc-=100000;self.assertTrue(self.probe());self.assertIn('WARN TICK_STALE',self.text())
    def test_future_tick_no_offset_correction(self):
        self.cfg['market_time_mode']='fixed_utc_v1'
        self.api.tick.time+=7200;self.api.tick.time_msc+=7200000;self.assertTrue(self.probe());self.assertIn('WARN TICK_FUTURE',self.text())
    def test_omitted_mode_no_windows_age_warning_or_db_creation(self):
        self.api.tick.time+=7200;self.api.tick.time_msc+=7200000
        self.assertTrue(self.probe())
        self.assertIn('INFO TICK_TIME_UNRESOLVED',self.text())
        self.assertNotIn('WARN TICK_FUTURE',self.text())
        self.assertFalse(Path(self.cfg['state_file']).exists())
    def test_omitted_mode_existing_db_not_opened_or_mutated(self):
        # Even an inaccessible/unknown binding is not grounds to assume UTC.
        path=Path(self.cfg['state_file']);path.write_bytes(b'opaque database fixture')
        self.api.tick.time-=7200;self.api.tick.time_msc-=7200000
        self.assertTrue(self.probe())
        self.assertIn('INFO TICK_TIME_UNRESOLVED',self.text())
        self.assertNotIn('WARN TICK_STALE',self.text())
        self.assertEqual(path.read_bytes(),b'opaque database fixture')
    def test_explicit_raw_no_windows_age_warning(self):
        self.cfg['market_time_mode']='mt5_raw_v1'
        self.api.tick.time+=7200;self.api.tick.time_msc+=7200000
        self.assertTrue(self.probe())
        self.assertIn('OK TICK_OBSERVED',self.text())
        self.assertNotIn('WARN TICK_FUTURE',self.text())
    def test_tick_invalid(self):
        self.api.tick.time_msc=float('nan');self.assertTrue(self.probe());self.assertIn('TICK_INVALID',self.text())
    def test_no_bars(self):
        self.api.bars=None;self.assertTrue(self.probe());self.assertIn('BARS_WAIT',self.text())
    def test_raw_exception_secret_never_written(self):
        def fail(*a,**kw):raise RuntimeError('password=TOPSECRET account=987654321 token=KEY')
        self.api.initialize=fail;self.assertFalse(self.probe())
        for text in ('TOPSECRET','987654321','KEY'):self.assertNotIn(text,self.text())
    def test_held_service_lock_blocks_probe(self):
        with installer.lock(self.root/'operation.lock'):self.assertFalse(self.runprobe())
        self.assertIn('LOCK_BUSY',self.text());self.assertFalse(self.api.calls)
    def test_existing_db_bytes_untouched(self):
        path=Path(self.cfg['state_file']);path.write_bytes(b'not opened by probe')
        self.assertTrue(self.runprobe());self.assertEqual(path.read_bytes(),b'not opened by probe')
    def test_runtime_safe_reason(self):
        text=write_failure(self.config,ValueError('Unexpected MT5 account/server'))
        self.assertIn('MT5_ACCOUNT_MISMATCH',text)
        self.assertTrue((self.root/'data'/'startup-error.txt').exists())
        self.assertNotIn('SECRET',write_failure(self.config,OSError('SECRET')))
    def test_runtime_entry_saves_early_error(self):
        from tradfi.runtime import main
        self.config.write_text('{invalid SECRET')
        with contextlib.redirect_stderr(io.StringIO()) as captured,self.assertRaises(SystemExit) as exc:
            main(['--mode','mt5','--service','--config',str(self.config)])
        self.assertEqual(exc.exception.code,2);self.assertIn('CONFIG_JSON',captured.getvalue());self.assertNotIn('SECRET',captured.getvalue())
    def test_report_resume_drops_injected_data(self):
        self.output.write_text('arbitrary password SECRET\nheader\nOK MT5_CONNECTED: password SECRET\nERROR unknown: SECRET\n')
        d.Report.resume(self.output).add('ERROR','TIMEOUT');self.assertNotIn('SECRET',self.text());self.assertIn('MT5_CONNECTED',self.text())
    def test_enabled_trading_still_read_only(self):
        self.cfg['enable_trading']=True;self.config.write_text(json.dumps(self.cfg))
        self.assertTrue(self.runprobe());self.assertEqual(self.api.calls,['initialize','shutdown'])
    def test_account_lock_prevents_parallel_connection(self):
        with installer.account_lock(self.root,self.cfg):self.assertFalse(self.runprobe())
        self.assertFalse(self.api.calls);self.assertIn('LOCK_BUSY',self.text())
    def test_previous_failure_allowlisted_only(self):
        (self.root/'data'/'startup-error.txt').write_text('SECRET\nSTARTUP MT5_ACCOUNT_MISMATCH\nSECRET')
        self.assertTrue(self.runprobe());self.assertIn('PREVIOUS_FAILURE',self.text());self.assertNotIn('SECRET',self.text())
    def test_unknown_previous_failure_not_echoed(self):
        (self.root/'data'/'startup-error.txt').write_text('STARTUP SECRET-IN-CODE')
        self.assertTrue(self.runprobe());self.assertNotIn('SECRET',self.text())
    def test_full_symbol_spec_validated_without_secret_values(self):
        for field in ('trade_contract_size','trade_tick_size','trade_tick_value_profit','trade_tick_value_loss','volume_min','volume_step','volume_max','point'):
            with self.subTest(field=field):
                api=API();setattr(api.symbol,field,0)
                report=d.Report(self.output)
                self.assertFalse(d.probe(self.cfg,report,api=api,busy=lambda *a:False))
                self.assertIn('MT5_SPEC_INCOMPLETE',self.text())
                self.assertEqual(api.calls,['initialize','shutdown'])
    def test_missing_spec_metadata_detected(self):
        del self.api.symbol.trade_tick_value_loss
        self.assertFalse(self.probe());self.assertIn('MT5_SPEC_INCOMPLETE',self.text())
    def test_nonfinite_and_invalid_spec_metadata_redacted(self):
        for value in (float('nan'),float('inf'),'SECRET-METADATA'):
            self.api.symbol.trade_contract_size=value
            self.assertFalse(self.probe());self.assertIn('MT5_SPEC_NUMERIC',self.text())
            self.assertNotIn('SECRET-METADATA',self.text())
    def test_invalid_spec_limits(self):
        self.api.symbol.trade_freeze_level=-1
        self.assertFalse(self.probe());self.assertIn('MT5_SPEC_LIMITS',self.text())
    def test_raw_tick_not_compared_to_windows(self):
        self.cfg['market_time_mode']='mt5_raw_v1'
        self.api.tick.time+=7200;self.api.tick.time_msc+=7200000
        self.assertTrue(self.probe());self.assertIn('TICK_OBSERVED',self.text())
        self.assertNotIn('TICK_FUTURE',self.text());self.assertNotIn('TICK_STALE',self.text())
    def test_invalid_raw_tick_identity_detected_and_redacted(self):
        self.cfg['market_time_mode']='mt5_raw_v1';self.api.tick.flags='SECRET-TICK'
        self.assertFalse(self.probe());self.assertIn('MT5_RAW_TICK_IDENTITY',self.text())
        self.assertIn('STAGE_RAW_TICK',self.text());self.assertNotIn('SECRET-TICK',self.text())
        self.assertEqual(self.api.calls,['initialize','shutdown'])
    def test_symbol_failure_runtime_stage_is_fixed_and_secret_free(self):
        from tradfi.broker import Spec
        try:Spec.load(dict(symbol='SECRET',currency='UST'))
        except Exception as exc:
            # KeyError is intentionally not diagnosed as a known failure;
            # the stage is still identified without echoing exception content.
            text=write_failure(self.config,exc)
        self.assertIn('STAGE SYMBOL_SPEC',text);self.assertNotIn('SECRET',text)
    def test_previous_failure_stage_allowlisted_only(self):
        path=self.root/'data'/'startup-error.txt'
        path.write_text('STARTUP MT5_SPEC_INCOMPLETE\nSTAGE SYMBOL_SPEC\nSECRET')
        self.assertTrue(self.runprobe());self.assertIn('STAGE_SYMBOL_SPEC',self.text())
        self.assertNotIn('SECRET',self.text())
        path.write_text('STARTUP STARTUP_UNKNOWN\nSTAGE SECRET-PATH')
        self.assertTrue(self.runprobe());self.assertNotIn('SECRET-PATH',self.text())
    def test_new_failure_reasons_are_exact_only(self):
        for message,code in [('Incomplete/nonpositive live symbol specification','MT5_SPEC_INCOMPLETE'),('Invalid stop/freeze levels','MT5_SPEC_LIMITS'),('Invalid native tick identity','MT5_RAW_TICK_IDENTITY'),('INVALID_MONOTONIC_CLOCK','MONOTONIC_CLOCK')]:
            self.assertEqual(reason(ValueError(message)),code)
            self.assertEqual(reason(ValueError(message+' SECRET')),'STARTUP_UNKNOWN')
    def manager(self,runner):
        m=installer.Manager(self.root,runner=runner,report=lambda x:None);m.current_release=lambda:self.root/'release';return m
    def test_manager_timeout_no_service_launch(self):
        seen=[]
        def timeout(args,**kw):seen.append((args,kw));raise subprocess.TimeoutExpired(args,40)
        self.manager(timeout).diagnose();self.assertIn('TIMEOUT',self.text())
        args,kw=seen[0];self.assertEqual(kw['timeout'],40)
        self.assertNotIn('--execute',args);self.assertNotIn('--service',args);self.assertNotIn(self.cfg['web']['password'],str(args));self.assertIn('startup_diagnostics.py',args[1])
    def test_manager_bad_exit_report(self):
        self.manager(lambda *a,**k:NS(returncode=1)).diagnose();self.assertIn('PROBE_FAILED',self.text())

if __name__=='__main__':unittest.main()
