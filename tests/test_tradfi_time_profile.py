"""Bybit time profile regressions; every API is an in-memory fake."""
from datetime import datetime, timezone
import json
import math
from unittest.mock import patch
import unittest

from test_tradfi_mt5 import FakeMT5, record
from tradfi.mt5_adapter import MT5Broker, QuoteFreshnessError
from tradfi.time_profile import (BYBIT_PROFILE, TEST_PROFILE, HistoryTimeUnverified,
                                TimeProfileError, offset_at, server_to_utc)


def epoch(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


class CalendarTest(unittest.TestCase):
    def test_exact_2026_utc_transitions(self):
        for instant, before, after in [('2026-03-08T02:00:00',7200,10800),
                                      ('2026-11-01T02:00:00',10800,7200)]:
            t = epoch(instant)
            self.assertEqual(offset_at(t-.001), before)
            self.assertEqual(offset_at(t), after)
            self.assertEqual(offset_at(t+.001), after)

    def test_calendar_is_not_europe_or_us_local_transition(self):
        self.assertEqual(offset_at(epoch('2026-03-15T12:00:00')),10800)
        self.assertEqual(offset_at(epoch('2026-10-28T12:00:00')),10800)
        self.assertEqual(offset_at(epoch('2027-03-14T02:00:00')),10800)
        self.assertEqual(offset_at(epoch('2027-11-07T02:00:00')),7200)

    def test_summer_winter_inverse_and_year_boundary(self):
        for value in ('2026-01-01T00:00:00','2026-07-01T00:00:00','2026-12-31T23:59:59'):
            t = epoch(value)
            self.assertEqual(server_to_utc(t+offset_at(t)),t)

    def test_gap_fold_and_invalid_are_rejected(self):
        for raw in (epoch('2026-03-08T04:30:00'), epoch('2026-11-01T04:30:00'),
                    0, -1, math.nan, math.inf, True, 'secret'):
            with self.subTest(raw=raw), self.assertRaises(TimeProfileError): server_to_utc(raw)


class BybitInspectTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeMT5()
        self.api.now = 1791809547.4977832
        self.mono = 1000.
        self.config = dict(backend='mt5_direct_v1', time_profile=BYBIT_PROFILE,
                           terminal_path='C:/Program Files/MetaTrader 5/terminal64.exe',
                           expected_server='Bybit-Live-6', expected_login=None, symbol='XAUUSD.s',
                           enable_trading=False)
        self.raw = 1791820347.418
        self.set_tick(self.raw)
        self.api.rates = [dict(time=t, close=4000.) for t in (1791820200,1791820260,1791820320)]
        self.broker = MT5Broker(self.config, api=self.api, clock=lambda:self.api.now,
                               monotonic=lambda:self.mono)
        self.broker.connect(); self.addCleanup(self.broker.close)

    def set_tick(self, raw):
        self.api.tick = record(bid=4000., ask=4000.1, time=int(raw), time_msc=round(raw*1000))

    def test_observed_vps_sample_inspects_without_history_or_mutation(self):
        with patch.object(self.api, 'history_orders_get', side_effect=AssertionError('history forbidden')), \
             patch.object(self.api, 'history_deals_get', side_effect=AssertionError('history forbidden')):
            result = self.broker.preflight()
        self.assertEqual(result['inspect_status'],'OK')
        self.assertEqual(result['quote_time']['offset_seconds'],10800)
        self.assertAlmostEqual(result['quote_time']['age_s'],.0797832,places=5)
        self.assertEqual(result['m1_utc_times'], [1791809400,1791809460,1791809520])
        self.assertEqual(result['m1_raw_times'],[1791820200,1791820260,1791820320])
        self.assertFalse(result['history_complete']); self.assertFalse(result['execution_ready'])
        self.assertEqual(result['history_time_status'],'UNVERIFIED')
        self.assertFalse(self.api.sends); self.assertFalse(self.api.checks)
        self.assertNotIn(str(self.api.account.login),json.dumps(result))

    def test_snapshot_and_store_binding_blocked_for_unverified_history(self):
        with self.assertRaises(HistoryTimeUnverified): self.broker.snapshot()
        with self.assertRaises(HistoryTimeUnverified): MT5Broker(self.config, store=object(), api=self.api)
        with self.assertRaises(HistoryTimeUnverified):
            MT5Broker(dict(self.config, enable_trading=True, history_verified=True), api=self.api)

    def test_runtime_blocks_before_store_lock_api_or_credentials(self):
        from tradfi.runtime import _run_mt5
        with patch('tradfi.runtime.Store') as store, patch('tradfi.runtime.owner_lock') as lock, \
             patch('tradfi.mt5_adapter.MT5Broker') as broker:
            with self.assertRaises(HistoryTimeUnverified):
                _run_mt5(dict(self.config, enable_trading=True), False)
            store.assert_not_called(); lock.assert_not_called(); broker.assert_not_called()

    def test_profile_missing_wrong_server_and_test_profile_cannot_enable_native_api(self):
        for changes in ({'time_profile':None}, {'expected_server':'Other'}, {'time_profile':TEST_PROFILE}):
            with self.assertRaises(TimeProfileError): MT5Broker(dict(self.config, **changes))

    def test_identity_bound_for_entire_readonly_session(self):
        self.api.account=self.api.account._replace(login=999)
        with self.assertRaises(ValueError): self.broker.preflight()

    def test_stale_and_future_ticks_rejected_with_five_second_limit(self):
        for age in (-6,6):
            self.set_tick(self.api.now+10800-age)
            with self.assertRaises(QuoteFreshnessError): self.broker.preflight()
        self.assertFalse(self.api.sends)

    def test_clock_jump_latches_even_when_tick_is_adjusted_and_clock_recovers(self):
        old=self.api.now
        self.api.now+=10; self.set_tick(self.api.now+10800)
        with self.assertRaises(TimeProfileError): self.broker.preflight()
        self.api.now=old; self.set_tick(self.raw)
        with self.assertRaises(TimeProfileError): self.broker.preflight()

    def test_nonfinite_clock_and_inconsistent_tick_fields_block(self):
        self.api.tick=self.api.tick._replace(time=self.api.tick.time-1)
        with self.assertRaises(TimeProfileError): self.broker.preflight()
        self.set_tick(self.raw); self.api.now=math.nan
        with self.assertRaises(TimeProfileError): self.broker.preflight()

    def test_wrong_m1_scale_fractional_and_unclosed_current_bar_block(self):
        for stamps in ((1791809400,1791809460,1791809520),
                       (1791820200,1791820260,1791820320.5),
                       (1791820260,1791820320,1791820380)):
            self.api.rates=[dict(time=t,close=4000.) for t in stamps]
            with self.assertRaises(TimeProfileError): self.broker.preflight()

    def test_slow_reads_preserve_original_tick_freshness_guard(self):
        def slow(*args):
            self.api.now+=6; self.mono+=6
            return self.api.rates
        self.api.copy_rates_from_pos=slow
        with self.assertRaises(QuoteFreshnessError): self.broker.preflight()
        self.assertFalse(self.api.calcs)


if __name__ == '__main__': unittest.main()
