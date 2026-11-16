import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('mql_compile',ROOT/'tests/native/compile_mql.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

@unittest.skipUnless(shutil.which('g++'),'g++ required for deterministic native adapter tests')
class NativeExecutionTests(unittest.TestCase):
    def test_actual_native_engine_scenarios_with_mock_terminal(self):
        try:module.compile_source('ImpulsTrader.mq5',(ROOT/'tests/native/engine_scenarios.cpp').read_text())
        except subprocess.CalledProcessError as exc:self.fail(exc.stderr)
    def test_observer_compiles_against_mock_api(self):
        try:module.compile_source('ImpulsObserver.mq5',(ROOT/'tests/native/observer_scenarios.cpp').read_text())
        except subprocess.CalledProcessError as exc:self.fail(exc.stderr)
    def test_sl_monitor_compiles_against_mock_api(self):
        try:module.compile_source('ImpulsSLMonitor.mq5',(ROOT/'tests/native/sl_scenarios.cpp').read_text())
        except subprocess.CalledProcessError as exc:self.fail(exc.stderr)

    def test_actual_trader_snapshot_is_valid_native_panel_json(self):
        definitions=(ROOT/'tests/native/engine_scenarios.cpp').read_text().split('int main(){')[0]
        body=definitions+r'int main(){reset();NativeSnapshot();std::cout<<mock_files[MailboxFolder+"\\native-view.json"];return 0;}'
        try:result=json.loads(module.compile_source('ImpulsTrader.mq5',body))
        except subprocess.CalledProcessError as exc:self.fail(exc.stderr)
        self.assertEqual(result['protocol'],'native_state_v1')
        self.assertEqual(result['currency'],'USD')
        self.assertEqual(result['settings']['initial_tp'],'0.2500000000')
        self.assertEqual(result['settings']['recovery_offset'],'0.5000000000')
        self.assertEqual(result['settings']['profiles']['8'],'0.70,1.30,2.30,3.30,4.00,4.70,5.10,5.60')
        self.assertEqual(result['settings']['profiles']['2'],'0.30,0.70')
        self.assertFalse(result['split_two'])
        self.assertIsInstance(result['candles'],list)
