"""Authenticated, socket-free panel alignment/migration on a READ_ONLY broker."""
import json
from pathlib import Path
import tempfile
import unittest

from tradfi.evidence_broker import MT5EvidenceBroker
from tradfi.recovery_v3 import ControlledRecoveryController
from tradfi.storage import Store
from tradfi.web_control import NativePanel
from test_raw_market_mode import config
from test_tradfi_native_utc import UTCAPI


class TimePanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'panel.db'); self.addCleanup(self.store.close)
        self.api = UTCAPI(); self.api.shift = 7200
        self.api.order_check = self.api.order_send = lambda *a, **k: self.fail('No broker mutation permitted')
        cfg = config(market_time_offset_seconds=7200); cfg.pop('market_time_mode')
        cfg['log_file'] = str(Path(self.tmp.name) / 'readonly.log')
        self.broker = MT5EvidenceBroker(cfg, self.store, api=self.api,
                                       clock=lambda: self.api.now, monotonic=lambda: self.api.now)
        self.broker.connect(); self.addCleanup(self.broker.close)
        self.bot = ControlledRecoveryController(self.store, self.broker,
                                                clock=lambda: self.api.now, monotonic=lambda: self.api.now)
        self.panel = NativePanel(self.bot, password='test-only-panel-password',
                                 clock=lambda: self.api.now, monotonic=lambda: self.api.now)
        self.addCleanup(self.panel.close)
        self.headers = {'Host': '127.0.0.1:8765', 'Origin': 'http://127.0.0.1:8765',
                        'X-Local-Control': '1', 'Content-Type': 'application/json'}
        code, _, extra = self.request('/api/login', {'password': 'test-only-panel-password'})
        self.assertEqual(code, 200)
        self.headers['Cookie'] = extra['Set-Cookie'].split(';')[0]
        self.panel.step()

    def request(self, path, payload=None):
        return self.panel.transport.request('GET' if payload is None else 'POST', path, self.headers,
                                             b'' if payload is None else json.dumps(payload).encode())

    def action(self, action_id, text):
        code, view, _ = self.request('/api/state'); self.assertEqual(code, 200)
        self.headers['X-Controller-Scope'] = view['scope']
        code, _, _ = self.request('/api/action', dict(id=action_id, revision=view['revision'],
                                                     action=dict(type='command', text=text)))
        self.assertEqual(code, 202)
        self.panel.step()
        code, view, _ = self.request('/api/state'); self.assertEqual(code, 200)
        self.assertEqual(view['action_results'][action_id]['status'], 'APPLIED', view['action_results'])
        return json.loads(json.dumps(view, allow_nan=False))

    def test_panel_sync_and_migration_replace_time_caches_without_trade(self):
        old_feed = self.panel.feed; old_analysis = self.panel.analysis
        view = self.action('clock_align_01', '/sync_mt5_time')
        self.assertEqual(view['time_status']['display_clock']['status'], 'ALIGNED')
        self.assertEqual(view['time_status']['market_time_mode'], 'fixed_utc_v1')
        self.assertFalse(view['time_status']['display_clock']['verified_utc'])
        self.assertIs(self.panel.feed, old_feed)
        view = self.action('clock_migrate_01', '/use_mt5_raw_time')
        self.assertEqual(view['time_status']['market_time_mode'], 'mt5_raw_v1')
        self.assertFalse(view['time_status']['can_switch_raw'])
        self.assertTrue(view['read_only']); self.assertTrue(view['stopped'])
        self.assertIsNot(self.panel.feed, old_feed)
        self.assertIsNot(self.panel.analysis, old_analysis)
        self.assertIs(self.panel.transport.analysis, self.panel.analysis)
        self.assertNotEqual(self.panel.feed.instance, old_feed.instance)
        self.assertIsNone(self.panel.snapshot)
        self.assertEqual(view['feed']['candles'], [])
        self.panel.step()
        view = self.panel.mailbox.view()
        self.assertEqual(view['feed']['timestamp_semantics'], 'BROKER_RAW')
        self.assertEqual(view['feed']['status'], 'OBSERVED')
        self.assertIsNone(view['feed']['timestamp'])
        self.assertTrue(all(b['utc_time'] is None for b in view['feed']['candles']))
        self.assertIn('mt5_raw_v1', self.bot.status())
        self.assertNotIn('OFFLINE_INJECTED_CLOCK', self.bot.status())
        self.assertEqual(self.store.load()['commands'], {})
        self.assertEqual(self.store.load()['completed'], [])


if __name__ == '__main__': unittest.main()
