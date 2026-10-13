"""File credentials and service integration, entirely offline."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from tradfi.credentials import CredentialsError, KEYS, load_telegram_credentials
from tradfi.runtime import _run_mt5, main
from tradfi.log_export import build_document
from test_tradfi_native_service import NativeAPI
from tradfi.time_profile import BYBIT_PROFILE

class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{},clear=True); self.env.start(); self.addCleanup(self.env.stop)
        self.values=dict(zip(KEYS,['SECRET_SENTINEL_TOKEN','778899','998877']))
        self.path=self.root/'telegram.json'; self.path.write_text(json.dumps(self.values))
        self.config=dict(backend='mt5_direct_v1',time_profile=BYBIT_PROFILE,
            expected_server='Bybit-Live-6',expected_login=111111111,symbol='XAUUSD.s',magic=25101102,
            terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',enable_trading=False,
            state_file=str(self.root/'state.db'),log_file=str(self.root/'diagnostic.log'),
            telegram_credentials_file='telegram.json')
        self.config_path=self.root/'service.json'
        self.config_path.write_text(json.dumps(self.config))
    def load(self): return load_telegram_credentials('telegram.json',config_path=self.config_path)
    def test_relative_path_and_no_environment_mutation(self):
        self.assertEqual(self.load(),tuple(self.values.values()))
        self.assertFalse(any(k in os.environ for k in KEYS))
    def test_windows_environment_expansion(self):
        os.environ['LOCALAPPDATA']=str(self.root)
        self.assertEqual(load_telegram_credentials('%LOCALAPPDATA%/telegram.json'),tuple(self.values.values()))
    def test_env_only_compatibility_and_user_default(self):
        os.environ.update({KEYS[0]:'fake',KEYS[1]:'123'})
        self.assertEqual(load_telegram_credentials(),('fake','123','123'))
    def test_per_field_environment_precedence(self):
        os.environ[KEYS[0]]='OVERRIDE'
        self.assertEqual(self.load(),('OVERRIDE','778899','998877'))
    def test_blank_environment_does_not_fall_back(self):
        os.environ[KEYS[0]]=''
        with self.assertRaises(CredentialsError): self.load()
    def test_group_requires_positive_user(self):
        self.path.write_text(json.dumps({KEYS[0]:'fake',KEYS[1]:'-123'}))
        with self.assertRaises(CredentialsError): self.load()
        os.environ[KEYS[2]]='456'
        self.assertEqual(self.load(),('fake','-123','456'))
    def test_invalid_content_and_types(self):
        samples=['{SECRET_SENTINEL_TOKEN','[]','null','{"unknown":"SECRET_SENTINEL_TOKEN"}',
                 '{"TRADFI_TELEGRAM_TOKEN":"a","TRADFI_TELEGRAM_TOKEN":"b"}','x'*16385]
        for key in KEYS:
            for value in (None,True,123,[],{},''):
                samples.append(json.dumps(dict(self.values,**{key:value})))
        for raw in samples:
            with self.subTest(raw=raw[:40]):
                self.path.write_text(raw)
                with self.assertRaises(CredentialsError) as caught: self.load()
                self.assertNotIn('SECRET_SENTINEL',str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
    def test_missing_file_env_and_path_type(self):
        for value in ('missing.json','%MISSING%/telegram.json','',123,True,[]):
            with self.subTest(value=value),self.assertRaises(CredentialsError):
                load_telegram_credentials(value,config_path=self.config_path)
    def test_invalid_credentials_before_locks_store_and_broker(self):
        self.path.write_text('{}')
        with patch('tradfi.runtime.owner_lock') as lock,patch('tradfi.runtime.Store') as store,patch('tradfi.evidence_broker.MT5EvidenceBroker') as broker:
            with self.assertRaises(CredentialsError):
                _run_mt5(self.config,False,service=True,config_path=self.config_path)
            lock.assert_not_called(); store.assert_not_called(); broker.assert_not_called()
        self.assertFalse((self.root/'state.db').exists())
    def test_cli_errors_are_generic(self):
        self.path.write_text('{SECRET_SENTINEL_TOKEN')
        output=io.StringIO()
        with contextlib.redirect_stderr(output),self.assertRaises(SystemExit):
            main(['--mode','mt5','--service','--config',str(self.config_path)])
        self.assertNotIn('SECRET_SENTINEL',output.getvalue())
        self.assertNotIn(str(self.root),output.getvalue())
    def test_inspect_does_not_load_file(self):
        with patch('tradfi.credentials.load_telegram_credentials') as loader,patch('tradfi.mt5_adapter.MT5Broker') as broker,contextlib.redirect_stdout(io.StringIO()):
            broker.return_value.preflight.return_value={}
            _run_mt5(self.config,True,config_path=self.config_path)
            loader.assert_not_called()
            self.assertNotIn('telegram_credentials_file',broker.call_args.args[0])
    def test_service_readonly_secrets_not_persisted_or_exported(self):
        api=NativeAPI(); stopped=threading.Event(); seen=[]
        case=self
        class TelegramDouble:
            def __init__(self,token,chat,**kw):
                case.assertEqual((token,chat,kw['user_id']),tuple(case.values.values()))
                self.http=self
            def close(self): pass
            def updates(self,*args): return []
            def start(self,*args): pass
            def process(self,controller):
                seen.append(controller.status())
                case.assertNotIn('telegram_credentials_file',controller.broker.config)
                case.assertFalse(any(v in json.dumps(controller.broker.config) for v in case.values.values()))
                with case.assertRaisesRegex(ValueError,'READ_ONLY'): controller.command('/start')
                stopped.set()
        os.environ['LOCALAPPDATA']=str(self.root)
        out=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(out):
            _run_mt5(dict(self.config,enable_trading=True),False,service=True,config_path=self.config_path,
                api=api,telegram_factory=TelegramDouble,stop_event=stopped,clock=lambda:api.now,monotonic=lambda:api.now)
        self.assertIn('READ_ONLY',seen[0]); self.assertFalse(api.sends); self.assertFalse(api.checks)
        payload=build_document(self.root/'diagnostic.log')
        for value in self.values.values():
            self.assertNotIn(value,out.getvalue())
            self.assertNotIn(value.encode(),payload)
            for path in self.root.iterdir():
                if path.name not in ('telegram.json','service.json') and path.is_file():
                    self.assertNotIn(value.encode(),path.read_bytes())

if __name__=='__main__': unittest.main()
