"""Repository split contract: no runnable training product or imports remain."""
import ast
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tradfi.runtime import main

ROOT=Path(__file__).resolve().parents[1]

class LiveRepositoryBoundary(unittest.TestCase):
    def test_removed_entrypoints_and_runtime_modules_absent(self):
        for name in ('START_PAPER.bat','SETUP_PAPER_PHONE_HTTPS.bat','paper_https_setup.py',
                     'tradfi/paper_runtime.py','tradfi/paper.py','tradfi/paper_market.py',
                     'tradfi/simulator.py','tradfi/interactive_simulator.py','tradfi/local_panel.py'):
            self.assertFalse((ROOT/name).exists(),name)
    def test_application_never_imports_test_doubles_or_removed_modules(self):
        forbidden={'paper','paper_runtime','paper_market','simulator','interactive_simulator','local_panel','tests'}
        for path in (ROOT/'tradfi').rglob('*.py'):
            tree=ast.parse(path.read_text())
            for node in ast.walk(tree):
                modules=[node.module or ''] if isinstance(node,ast.ImportFrom) else [a.name for a in node.names] if isinstance(node,ast.Import) else []
                for module in modules:
                    self.assertFalse(set(module.split('.')) & forbidden,(path.name,module))
    def test_retired_cli_modes_fail_before_any_native_operation(self):
        for mode in ('paper','simulate'):
            with patch('tradfi.runtime._run_mt5') as run,redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:main(['--mode',mode])
                self.assertEqual(error.exception.code,2);run.assert_not_called()
    def test_default_mode_and_service_do_not_enable_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.json'
            config.write_text(json.dumps(dict(backend='mt5_direct_v1',enable_trading=True)))
            for extra in ([],['--service']):
                with patch('tradfi.runtime._run_mt5') as run:
                    main(['--config',str(config),*extra])
                self.assertFalse(run.call_args.kwargs['execute'])
                self.assertEqual(run.call_args.kwargs['service'],bool(extra))
    def test_execute_without_explicit_service_is_rejected(self):
        with patch('tradfi.runtime._run_mt5') as run,redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:main(['--execute'])
            self.assertEqual(error.exception.code,2);run.assert_not_called()
