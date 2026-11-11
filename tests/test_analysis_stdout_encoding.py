"""Vendor stdout stays discard-only on legacy Windows code pages; no MT5."""
import builtins
import io
import json
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tools import analysis_worker, check_analysis_dependencies


class VendorStdoutEncodingTests(unittest.TestCase):
    @staticmethod
    def legacy_open(encoding):
        def open_sink(path, mode, **kwargs):
            if path == os.devnull and mode == 'w':
                # Simulate Windows locale defaults without relying on the host
                # locale. Explicit production encoding must override this.
                kwargs.setdefault('encoding', encoding)
            return builtins.open(path, mode, **kwargs)
        return open_sink

    def test_smoke_sink_accepts_unicode_vendor_banner_on_legacy_windows(self):
        def analyze(*args):
            print('SmartMoneyConcepts \u2b50')
            print('Discard malformed vendor text: \ud800')
            return {'modules': {'smc': {'status': 'READY'}}, 'overlays': {}}
        for encoding in ('cp1251', 'cp1252'):
            with self.subTest(encoding=encoding), \
                    patch.object(check_analysis_dependencies, 'open', self.legacy_open(encoding), create=True), \
                    patch('tradfi.analysis_adapters.analyze_bars', side_effect=analyze), \
                    patch.object(check_analysis_dependencies.sys, 'platform', 'linux'):
                value = check_analysis_dependencies.smoke_check()
                self.assertEqual(value['modules']['smc']['status'], 'READY')
                self.assertFalse(value['native_binding_import']['initialized'])

    def test_worker_sink_does_not_corrupt_binary_frames_on_legacy_windows(self):
        config = dict(terminal_path='OFFLINE_ONLY', symbol='OFFLINE_ONLY',
                      expected_login=1, expected_server='OFFLINE_ONLY',
                      time_mode='fixed', offset=0)
        payload = b'{"offline":true}'
        def fake_worker(config, incoming, outgoing):
            # Exercise the real redirection and Output framing, never _worker's
            # native initialization, imports, account reads or history calls.
            print('SmartMoneyConcepts \u2b50')
            print('Discard malformed vendor text: \ud800')
            outgoing.send_bytes(payload)
        for encoding in ('cp1251', 'cp1252'):
            frame = io.BytesIO()
            stdout = MagicMock(); stdout.fileno.return_value = 1
            stdin = SimpleNamespace(buffer=io.BytesIO(json.dumps(config).encode() + b'\n'))
            with self.subTest(encoding=encoding), \
                    patch.object(analysis_worker, 'open', self.legacy_open(encoding), create=True), \
                    patch.object(analysis_worker.os, 'dup', return_value=999), \
                    patch.object(analysis_worker.os, 'fdopen', return_value=frame), \
                    patch.object(analysis_worker.os, 'dup2'), \
                    patch.object(analysis_worker.sys, 'stdout', stdout), \
                    patch.object(analysis_worker.sys, 'stdin', stdin), \
                    patch.object(analysis_worker.sys, 'version_info', (3, 12, 0)), \
                    patch('tradfi.analysis_service._worker', side_effect=fake_worker):
                try:
                    analysis_worker.main()
                    self.assertEqual(frame.getvalue(), struct.pack('!I', len(payload)) + payload)
                finally:
                    if analysis_worker.sys.stdout is not stdout:
                        analysis_worker.sys.stdout.close()
