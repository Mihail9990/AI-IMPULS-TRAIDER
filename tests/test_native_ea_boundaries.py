"""Source boundaries supplement, but do not replace, MetaEditor/runtime tests."""
from pathlib import Path
import ast
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]

class NativeEABoundaries(unittest.TestCase):
    def test_candidates_cannot_send_trade_requests(self):
        for path in (ROOT/'mt5/native').glob('*'):
            if path.name in {"NativeEngine.mqh", "NativeControl.mqh", "ImpulsTrader.mq5"}:continue
            if path.suffix not in {'.mq5','.mqh'}:continue
            source=re.sub(r'//[^\n]*|/\*.*?\*/','',path.read_text(),flags=re.S)
            self.assertNotRegex(source,r'\b(?:OrderSend|OrderSendAsync|CTrade)\s*\(')

    def test_transport_does_not_import_execution_modules(self):
        tree=ast.parse((ROOT/'tradfi/native_transport.py').read_text())
        modules=[]
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):modules.extend(n.name for n in node.names)
            if isinstance(node,ast.ImportFrom):modules.append(node.module)
        self.assertFalse(set(modules)&{'MetaTrader5','mt5_adapter','evidence_broker','engine','recovery_v3'})

    def test_runtime_has_not_been_silently_switched_to_candidate(self):
        source=(ROOT/'tradfi/runtime.py').read_text()
        self.assertIn("config.get('backend') == 'ea_native_v1'",source)
        self.assertIn('ControlledRecoveryController',source)

    def test_tick_consumers_require_the_symbol_chart(self):
        for name in ('ImpulsTrader.mq5','ImpulsSLMonitor.mq5'):
            self.assertIn('_Symbol!=TradeSymbol',(ROOT/'mt5/native'/name).read_text())

    def test_only_engine_is_allowed_to_send(self):
        senders=[]
        for path in (ROOT/'mt5/native').glob('*'):
            source=re.sub(r'//[^\n]*|/\*.*?\*/','',path.read_text(),flags=re.S)
            if re.search(r'\b(?:OrderSend|OrderSendAsync)\s*\(',source):senders.append(path.name)
        self.assertEqual(senders,['NativeEngine.mqh'])
