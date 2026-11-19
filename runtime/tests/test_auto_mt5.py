import hashlib
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import auto_mt5 as m

class AutoMt5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.signature_patch = patch.object(m, 'verify_metaquotes')
        self.signature_patch.start()
        self.addCleanup(self.signature_patch.stop)
        self.install = self.root / 'Program Files/MetaTrader 5'
        self.install.mkdir(parents=True)
        for name in ('terminal64.exe', 'metaeditor64.exe'):
            (self.install / name).write_bytes(b'MZfake')
        self.appdata = self.root / 'AppData'
        self.data = self.appdata / 'MetaQuotes/Terminal/ABC'
        self.make_data(self.data)
        self.server = self.root / 'Server'
        self.source = self.server / 'recovery/mql5' / m.EA_NAME
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b'// read only fixture')
    def make_data(self, directory):
        (directory / 'MQL5/Experts').mkdir(parents=True)
        (directory / 'origin.txt').write_text(str(self.install), encoding='utf-16')
    def detect(self):
        return m.detect_mt5(appdata=self.appdata)
    def runner(self, args, **kwargs):
        self.assertFalse(kwargs['shell'])
        self.assertEqual(kwargs['timeout'], 120)
        self.assertEqual(args[0], str(self.install / 'metaeditor64.exe'))
        self.assertEqual(args[-1], '/log')
        source = Path(args[1].removeprefix('/compile:'))
        source.with_suffix('.ex5').write_bytes(b'x' * 100)
        source.with_suffix('.log').write_text('Result: 0 errors, 0 warnings, 12 msec elapsed', encoding='utf-16')
        return SimpleNamespace(returncode=0)
    def compile(self, runner=None):
        with patch.object(m, 'EA_SHA256', hashlib.sha256(self.source.read_bytes()).hexdigest()):
            return m.prepare_compile_ea(self.server, self.detect(), log=lambda _: None,
                                        runner=runner or self.runner, platform='nt')
    def test_detect_origin_utf16(self):
        found = self.detect()
        self.assertEqual(found.data_dir, self.data)
        self.assertEqual(found.terminal.parent, self.install)
    def test_ambiguity_requires_selection(self):
        self.make_data(self.data.parent / 'DEF')
        with self.assertRaises(m.SetupError): self.detect()
        self.assertEqual(m.detect_mt5(data_dir=self.data).data_dir, self.data)
    def test_mismatched_terminal_rejected(self):
        with self.assertRaises(m.SetupError):
            m.detect_mt5(self.root / 'Other/terminal64.exe', self.data)
    def test_portable_explicit_only(self):
        (self.install / 'MQL5/Experts').mkdir(parents=True)
        self.assertEqual(m.detect_mt5(self.install / 'terminal64.exe', self.install).data_dir, self.install)
    def test_missing_editor_fails(self):
        (self.install / 'metaeditor64.exe').unlink()
        with self.assertRaises(m.SetupError): self.detect()
    def test_symlink_rejected(self):
        alias = self.root / 'alias'; alias.symlink_to(self.data, target_is_directory=True)
        with self.assertRaises(m.SetupError): m.detect_mt5(data_dir=alias)
    def test_compile_and_repeat_reuses(self):
        first, second = self.compile(), self.compile()
        self.assertEqual(first.source, second.source)
        self.assertTrue(second.reused)
        self.assertEqual(first.source.read_bytes(), self.source.read_bytes())
    def test_receipt_reuses_without_compiler(self):
        first = self.compile()
        marker = first.source.parent / m.RECEIPT_NAME
        self.assertTrue(marker.is_file())
        result = self.compile(lambda *a, **k: self.fail('must not compile again'))
        self.assertTrue(result.reused)
        self.assertEqual(result.executable, first.executable)
        self.assertTrue(result.log_path.is_file())
    def test_receipt_tamper_fails_no_recompile(self):
        first = self.compile()
        first.executable.write_bytes(b'tampered' * 20)
        with self.assertRaises(m.SetupError):
            self.compile(lambda *a, **k: self.fail('must not compile changed receipt'))
    def test_receipt_tool_update_requires_review(self):
        first = self.compile()
        (self.install / 'metaeditor64.exe').write_bytes(b'MZnewcompiler')
        with self.assertRaises(m.SetupError): self.compile()
    def test_receipt_log_change_fails(self):
        first = self.compile()
        first.log_path.write_text('Result: 1 errors, 0 warnings')
        with self.assertRaises(m.SetupError): self.compile()
    def test_receipt_revalidates_signature(self):
        self.compile()
        with patch.object(m, 'verify_metaquotes', side_effect=m.SetupError('invalid signature')):
            with self.assertRaises(m.SetupError): self.compile()
    def test_manual_root_never_gets_receipt(self):
        folder = self.data / 'MQL5/Experts'
        (folder / m.EA_NAME).write_bytes(self.source.read_bytes())
        (folder / 'RecoveryEvidenceBridge.ex5').write_bytes(b'x' * 100)
        self.compile()
        self.assertFalse((folder / m.RECEIPT_NAME).exists())
    def test_existing_root_reused_without_duplicate(self):
        folder = self.data / 'MQL5/Experts'
        (folder / m.EA_NAME).write_bytes(self.source.read_bytes())
        (folder / 'RecoveryEvidenceBridge.ex5').write_bytes(b'x' * 100)
        result = self.compile()
        self.assertTrue(result.reused)
        self.assertEqual(result.source.parent, folder)
        self.assertEqual(len(list(folder.iterdir())), 2)
    def test_existing_unverifiable_preserved(self):
        binary = self.data / 'MQL5/Experts/RecoveryEvidenceBridge.ex5'
        binary.write_bytes(b'old unverified binary')
        with self.assertRaises(m.SetupError): self.compile()
        self.assertEqual(binary.read_bytes(), b'old unverified binary')
    def test_existing_binary_mismatch_no_duplicate(self):
        folder = self.data / 'MQL5/Experts'
        (folder / m.EA_NAME).write_bytes(self.source.read_bytes())
        (folder / 'RecoveryEvidenceBridge.ex5').write_bytes(b'y' * 100)
        with self.assertRaises(m.SetupError): self.compile()
        self.assertEqual(len(list(folder.iterdir())), 2)
    def test_multiple_copies_rejected(self):
        first = self.compile()
        root = self.data / 'MQL5/Experts'
        (root / m.EA_NAME).write_bytes(self.source.read_bytes())
        with self.assertRaises(m.SetupError): self.compile()
    def test_relative_origin_rejected(self):
        (self.data / 'origin.txt').write_text('relative/path')
        with self.assertRaises(m.SetupError): self.detect()
    def test_signature_rejection_never_compiles(self):
        with patch.object(m, 'verify_metaquotes', side_effect=m.SetupError('unsigned')):
            with self.assertRaises(m.SetupError):
                self.compile(lambda *a, **k: self.fail('unsigned must not run'))
    def test_no_binary_fails_even_returncode_zero(self):
        with self.assertRaises(m.SetupError):
            self.compile(lambda *a, **k: SimpleNamespace(returncode=0))
    def test_error_log_fails(self):
        def bad(args, **kw):
            result = self.runner(args, **kw)
            Path(args[1][9:]).with_suffix('.log').write_text('Result: 1 errors, 0 warnings')
            return result
        with self.assertRaises(m.SetupError): self.compile(bad)
    def test_success_exit_one_with_warning(self):
        def run(args, **kw):
            self.runner(args, **kw)
            Path(args[1][9:]).with_suffix('.log').write_text('Result: 0 errors, 1 warning, 50 msec elapsed', encoding='utf-16')
            return SimpleNamespace(returncode=1)
        self.assertTrue(self.compile(run).executable.is_file())
    def test_last_summary_errors_rejected(self):
        def run(args, **kw):
            self.runner(args, **kw)
            Path(args[1][9:]).with_suffix('.log').write_text('Result: 0 errors, 0 warnings\nResult: 1 errors, 0 warnings')
            return SimpleNamespace(returncode=0)
        with self.assertRaises(m.SetupError): self.compile(run)
    def test_stale_binary_rejected(self):
        def run(args, **kw):
            result = self.runner(args, **kw)
            m.os.utime(Path(args[1][9:]).with_suffix('.ex5'), (1, 1))
            return result
        with self.assertRaises(m.SetupError): self.compile(run)
    def test_unsupported_exit_with_files_rejected(self):
        def run(args, **kw):
            self.runner(args, **kw)
            return SimpleNamespace(returncode=2)
        with self.assertRaises(m.SetupError): self.compile(run)
    def test_no_result_line_rejected(self):
        def run(args, **kw):
            result = self.runner(args, **kw)
            Path(args[1][9:]).with_suffix('.log').write_text('0 errors, 0 warnings')
            return result
        with self.assertRaises(m.SetupError): self.compile(run)
    def test_timeout_fails(self):
        def timeout(*a, **k): raise subprocess.TimeoutExpired(a, 120)
        with self.assertRaises(m.SetupError): self.compile(timeout)
    def test_unpinned_source_never_runs(self):
        with self.assertRaises(m.SetupError):
            m.prepare_compile_ea(self.server, self.detect(), log=lambda _: None,
                                runner=lambda *a, **k: self.fail('must not execute'), platform='nt')

class SignatureTests(unittest.TestCase):
    def test_default_windows_signature_valid_and_reject_others(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe = root / 'metaeditor64.exe'; exe.write_bytes(b'MZfixture')
            ps = root / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            ps.parent.mkdir(parents=True); ps.write_bytes(b'MZfixture')
            with patch.dict(m.os.environ, {'SystemRoot': str(root), 'ProgramFiles': str(root)}):
                for status, publisher, success in [('Valid', 'MetaQuotes Ltd.', True), ('NotTrusted', 'MetaQuotes Ltd.', False), ('Valid', 'Other Company', False), ('NotSigned', '', False)]:
                    def run(args, **kw):
                        self.assertEqual(args[0], str(ps))
                        self.assertNotIn('-ExecutionPolicy', args)
                        self.assertFalse(kw['shell'])
                        return SimpleNamespace(returncode=0, stdout=json.dumps(dict(status=status, publisher=publisher)).encode())
                    if success: m.verify_metaquotes(exe, runner=run)
                    else:
                        with self.assertRaises(m.SetupError): m.verify_metaquotes(exe, runner=run)

if __name__ == '__main__': unittest.main()
