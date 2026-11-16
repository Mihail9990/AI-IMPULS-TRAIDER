"""Offline chart-only boundary tests: no execution or terminal connection."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from tradfi.chart_feed import ChartFeed


def bar(stamp, **changes):
    return dict(time=stamp, open=100, high=102, low=99, close=101, **changes)


class ChartRolloverTest(unittest.TestCase):
    def setUp(self):
        self.api=SimpleNamespace(TIMEFRAME_M1=1,
            copy_rates_from_pos=Mock(return_value=[bar(60),bar(120)]),
            order_send=Mock(side_effect=AssertionError('Chart must not trade')))
        self.broker=SimpleNamespace(api=self.api,symbol='XAUUSD.s',
            config={'market_time_mode':'mt5_raw_v1'},_context=Mock(),max_age=5)
        self.feed=ChartFeed(self.broker,clock=lambda:999999,monotonic=lambda:1)
        self.snapshot={'quote':{'broker_time':179.999,'bid':'100','ask':'101','utc_time':None},
                       'market_observation':{'sequence':10}}

    def assert_no_current(self):
        current=self.feed.disconnected()
        self.assertFalse(current['connected'])
        self.assertIsNone(current['quote'])
        self.assertEqual(current['candles'],[])
        self.api.order_send.assert_not_called()

    def test_boundary_old_tick_new_bar_hides_current_until_next_capture(self):
        self.feed.capture(self.snapshot)
        before=deepcopy(self.snapshot)
        self.api.copy_rates_from_pos.return_value=[bar(120),bar(180)]
        with self.assertRaises(ValueError):self.feed.capture(self.snapshot)
        self.assert_no_current()
        self.assertEqual(self.snapshot,before)
        # Next ordinary poll has a coherent new snapshot; no internal retry.
        self.snapshot['quote']['broker_time']=180
        result=self.feed.capture(self.snapshot)
        self.assertEqual(result['candles'][-1]['raw_time'],180)
        self.assertFalse(result['candles'][-1]['closed'])
        self.assertTrue(result['candles'][0]['closed'])
        self.assertEqual(self.api.copy_rates_from_pos.call_count,3)
        self.api.order_send.assert_not_called()

    def test_new_tick_old_bar_also_withholds_current(self):
        self.feed.capture(self.snapshot)
        self.snapshot['quote']['broker_time']=180
        with self.assertRaisesRegex(OSError,'observation disagree'):
            self.feed.capture(self.snapshot)
        self.assert_no_current()

    def test_cached_matching_pair_accepted_without_wall_clock_age_gate(self):
        result=self.feed.capture(self.snapshot)
        self.assertEqual(result['status'],'OBSERVED')
        self.assertEqual(result['broker_time'],179.999)
        self.assertIsNone(result['age_seconds'])
        self.api.order_send.assert_not_called()

    def test_invalid_candles_do_not_retain_previous_chart(self):
        for rows in (None, [], [bar(60),bar(60)], [bar(120),bar(60)],
                     [bar(61),bar(120)], [dict(bar(120),low=200)],
                     [dict(bar(120),time=float('nan'))]):
            with self.subTest(rows=rows):
                self.api.copy_rates_from_pos.return_value=[bar(60),bar(120)]
                self.feed.capture(self.snapshot)
                self.api.copy_rates_from_pos.return_value=rows
                with self.assertRaises((ValueError,OSError)):
                    self.feed.capture(self.snapshot)
                self.assert_no_current()

    def test_invalid_tick_withholds_current(self):
        for tick in (0,-1,float('nan'),float('inf')):
            with self.subTest(tick=tick):
                self.snapshot['quote']['broker_time']=tick
                with self.assertRaises(ValueError):self.feed.capture(self.snapshot)
                self.assert_no_current()


if __name__=='__main__':unittest.main()
