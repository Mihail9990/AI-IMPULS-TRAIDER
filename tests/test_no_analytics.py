"""Removed analytics cannot restart through obsolete routes or preferences."""
import json
from pathlib import Path
import unittest
from tradfi.control import OwnerMailbox
from tradfi.web_control import WebTransport

ROOT = Path(__file__).resolve().parents[1]

class RemovedAnalyticsTest(unittest.TestCase):
    def test_old_analysis_endpoint_is_not_available(self):
        box = OwnerMailbox()
        transport = WebTransport(box, password='test-only-not-a-real-secret')
        headers = {'Host':'127.0.0.1:8765', 'Origin':'http://127.0.0.1:8765',
                   'X-Local-Control':'1', 'Content-Type':'application/json'}
        code, _, extra = transport.request('POST', '/api/login', headers,
            json.dumps({'password':'test-only-not-a-real-secret'}).encode())
        self.assertEqual(code, 200)
        headers['Cookie'] = extra['Set-Cookie'].split(';')[0]
        for path in ('/api/analysis?timeframe=M1', '/analysis.js'):
            code, value, _ = transport.request('GET', path, headers)
            self.assertEqual(code, 404)
            self.assertEqual(value['error'], 'NOT_FOUND')
        self.assertTrue(box.requests.empty())

    def test_removed_modules_and_installer_not_shipped(self):
        for name in ('SETUP_ANALYSIS.cmd', 'requirements-analysis.txt',
                     'tradfi/analysis_service.py', 'tradfi/analysis_process.py',
                     'tradfi/analysis_adapters.py', 'tradfi/analysis_runtime.py',
                     'tools/analysis_worker.py', 'web/basket-panel/analysis.js'):
            self.assertFalse((ROOT/name).exists(), name)

    def test_native_panel_has_no_analytics_dependency(self):
        source=(ROOT/'tradfi/web_control.py').read_text()
        self.assertNotIn('analysis', source.lower())
        html=(ROOT/'web/basket-panel/index.html').read_text()
        js=(ROOT/'web/basket-panel/panel.js').read_text()
        for removed in ('analysis.js', 'main-analysis', 'liquidity-visible', 'zone-opacity'):
            self.assertNotIn(removed, html)
        for removed in ('mainAnalysisUI', 'createMainAnalysisUI', 'saved.opacity', "'liquidity'"):
            self.assertNotIn(removed, js)

if __name__ == '__main__': unittest.main()
