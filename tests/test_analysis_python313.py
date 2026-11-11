"""Python 3.13 migration contracts, without installation, MT5 or LIVE state."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tradfi import analysis_runtime as runtime
from tradfi import analysis_service as service_module
from tradfi.analysis_process import validate_python
from tools import check_analysis_dependencies as check
from tools import setup_analysis as setup
from tools import analysis_worker as worker


class RuntimeTests(unittest.TestCase):
    def test_only_standard_cpython313_x64_is_supported(self):
        cases = [((3, 13, 5), 'cpython', 8, 0, True),
                 ((3, 12, 14), 'cpython', 8, 0, False),
                 ((3, 14, 0), 'cpython', 8, 0, False),
                 ((3, 13, 5), 'cpython', 4, 0, False),
                 ((3, 13, 5), 'cpython', 8, 1, False),
                 ((3, 13, 5), 'pypy', 8, 0, False)]
        for version, implementation, size, free_threaded, expected in cases:
            with self.subTest(version=version, implementation=implementation,
                              size=size, free_threaded=free_threaded):
                fake_sys = SimpleNamespace(version_info=version,
                                           implementation=SimpleNamespace(name=implementation))
                with patch.object(runtime, 'sys', fake_sys), \
                        patch.object(runtime.struct, 'calcsize', return_value=size), \
                        patch.object(runtime.sysconfig, 'get_config_var', return_value=free_threaded):
                    self.assertEqual(runtime.supported_runtime(), expected)

    def test_lock_checker_requires_supported_runtime_even_with_matching_packages(self):
        for supported in (False, True):
            with patch.object(check, 'inspect_packages', return_value={k: {'status': 'PRESENT'} for k in check.PACKAGES}), \
                    patch.object(check, 'inspect_lock', return_value={'matches': True, 'mismatches': []}), \
                    patch.object(check, 'supported_runtime', return_value=supported), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                code = check.main(['--require-lock'])
            report = json.loads(output.getvalue())
            self.assertEqual(code, 0 if supported else 2)
            self.assertEqual(report['runtime_supported'], supported)
            self.assertFalse(report['windows_vendor_smoke_verified'])
            self.assertFalse(report['windows_release_verified'])

    def test_worker_checks_runtime_before_native_worker(self):
        class Output(io.StringIO):
            def fileno(self): return 1
        config = dict(terminal_path='offline', symbol='offline', expected_login=1,
                      expected_server='offline', time_mode='fixed', offset=0)
        for supported in (False, True):
            incoming = SimpleNamespace(buffer=io.BytesIO((json.dumps(config) + '\n').encode()))
            with patch.object(runtime, 'supported_runtime', return_value=supported), \
                    patch('tradfi.analysis_service._worker') as native_worker, \
                    patch.object(worker.os, 'dup', return_value=42), \
                    patch.object(worker.os, 'dup2'), \
                    patch.object(worker.os, 'fdopen', return_value=io.BytesIO()), \
                    patch('builtins.open', return_value=Output()), \
                    patch.object(worker.sys, 'stdout', Output()), \
                    patch.object(worker.sys, 'stdin', incoming):
                if supported:
                    worker.main()
                    self.assertEqual(native_worker.call_args.args[0], config)
                else:
                    with self.assertRaisesRegex(ValueError, 'PYTHON_313_X64_GIL_REQUIRED'):
                        worker.main()
                    native_worker.assert_not_called()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'requirements-analysis.txt').write_text('numpy==2.5.4\n')

    def tearDown(self):
        self.temp.cleanup()

    def make_venv(self, name, version='3.13.5'):
        root = self.root / name
        executable = root / 'Scripts' / 'python.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'offline fixture, never executable')
        (root / 'pyvenv.cfg').write_text(f'version = {version}\ninclude-system-site-packages = false\n')
        return executable

    def run_setup(self, run):
        with patch.object(setup.sys, 'platform', 'win32'), \
                patch.object(setup, 'supported_runtime', return_value=True), \
                patch.object(setup, 'resolve_target', return_value=(self.root, None)), \
                patch.object(setup, 'run', side_effect=run), \
                contextlib.redirect_stdout(io.StringIO()):
            return setup.main([])

    def test_install_creates_new313_environment_preserving_old_and_live(self):
        old = self.make_venv('.venv-analysis', '3.12.14')
        live = self.make_venv('live-venv')
        preserved = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        calls = []
        def run(args, env):
            calls.append(args)
            self.assertEqual(env['PIP_CONFIG_FILE'], setup.os.devnull)
            if args[1:4] == ['-I', '-m', 'venv']:
                self.assertEqual(Path(args[4]), self.root / runtime.VENV_DIRECTORY)
                self.make_venv(runtime.VENV_DIRECTORY)
        self.assertEqual(self.run_setup(run), 0)
        for path, contents in preserved.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertTrue(old.exists() and live.exists())
        self.assertEqual(len(calls), 4)
        target = self.root / runtime.VENV_DIRECTORY
        self.assertTrue(all(args[0] == str(target / 'Scripts' / 'python.exe') for args in calls[1:]))
        self.assertIn('--only-binary=:all:', calls[1])
        self.assertEqual(calls[2][1:], ['-I', '-m', 'pip', 'check'])
        self.assertEqual(calls[3][-2:], ['--smoke', '--require-lock'])
        marker = json.loads((target / 'analysis-smoke.json').read_text())
        self.assertEqual(marker['python'], '3.13')
        self.assertFalse(marker['terminal_connected'])
        self.assertNotIn('service.json', [p.name for p in self.root.rglob('*')])

    def test_existing313_target_is_validated_and_reused_without_venv_recreation(self):
        self.make_venv(runtime.VENV_DIRECTORY)
        calls = []
        self.assertEqual(self.run_setup(lambda args, env: calls.append(args)), 0)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any('venv' in args for args in calls))

    def test_wrong_version_in_new_target_is_not_replaced(self):
        executable = self.make_venv(runtime.VENV_DIRECTORY, '3.12.14')
        cfg = executable.parent.parent / 'pyvenv.cfg'
        before = cfg.read_bytes()
        calls = []
        with self.assertRaisesRegex(ValueError, 'ANALYSIS_PYTHON_313_REQUIRED'):
            self.run_setup(lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        self.assertEqual(cfg.read_bytes(), before)
        self.assertFalse((cfg.parent / 'analysis-smoke.json').exists())

    def test_live_owner_venv_is_rejected_even_when_python_version_matches(self):
        executable = self.make_venv('live-venv')
        with patch.object(sys, 'prefix', str(executable.parent.parent)):
            with self.assertRaisesRegex(ValueError, 'ANALYSIS_TARGET_MUST_BE_SEPARATE'):
                validate_python(str(executable))

    def broker(self, config=None):
        return SimpleNamespace(path='C:/mt5/terminal.exe', symbol='OFFLINE_ONLY',
                               login=1, account='1@offline', server='offline',
                               market_time_offset_seconds=0, max_age=5,
                               api=SimpleNamespace(), config=config or {})

    def test_windows_service_selects_new_default_without_starting_worker(self):
        with patch.object(service_module, 'os', SimpleNamespace(name='nt')), \
                patch('tradfi.analysis_process.validate_python', side_effect=lambda value: value) as validate:
            service = service_module.AnalysisService(self.broker())
        expected = str(Path(service_module.__file__).resolve().parents[1]
                       / runtime.VENV_DIRECTORY / 'Scripts' / 'python.exe')
        validate.assert_called_once_with(expected)
        self.assertEqual(service.external_python, expected)
        self.assertTrue(service.enabled)
        self.assertIsNone(service.thread)

    def test_explicit_override_has_priority_and_old312_never_falls_back(self):
        for version in ('3.13.5', '3.12.14'):
            executable = self.make_venv('override-' + version, version)
            with patch.object(service_module, 'os', SimpleNamespace(name='nt')), \
                    patch('tradfi.analysis_process.validate_python', wraps=validate_python) as validate:
                service = service_module.AnalysisService(self.broker({'analysis_python': str(executable)}))
            validate.assert_called_once_with(str(executable))
            self.assertIsNone(service.thread)
            if version.startswith('3.13.'):
                self.assertEqual(service.external_python, str(executable))
                self.assertTrue(service.enabled)
            else:
                self.assertIsNone(service.external_python)
                self.assertFalse(service.enabled)
                self.assertEqual(service.view('M1')['reason'], 'ANALYSIS_ENVIRONMENT_UNAVAILABLE')

    def test_setup_rejects_unsupported_runtime_before_target_or_commands(self):
        with patch.object(setup.sys, 'platform', 'win32'), \
                patch.object(setup, 'supported_runtime', return_value=False), \
                patch.object(setup, 'resolve_target') as resolve, \
                patch.object(setup, 'run') as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.main([]), 2)
        resolve.assert_not_called()
        run.assert_not_called()


if __name__ == '__main__': unittest.main()
