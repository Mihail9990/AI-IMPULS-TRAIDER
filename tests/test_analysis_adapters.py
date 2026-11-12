"""Optional engines replaced by offline stubs: no broker or package install."""
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from tradfi import analysis_adapters as a


def bars(n=80):
    return [dict(utc_time=1700000000 + i*60, raw_time=1700000000 + i*60,
                 open='100', high='102', low='98', close='101', tick_volume='10') for i in range(n)]




class Smc:
    def __init__(self):
        self.frames = []
    def swing_highs_lows(self, frame, swing_length):
        import pandas as pd
        self.frames.append(frame.copy())
        self.swing_length = swing_length
        result = pd.DataFrame({'HighLow': np.full(len(frame), np.nan), 'Level': np.full(len(frame), np.nan)})
        # Edge extrema are artificial, and a near-edge point is provisional.
        for i in (0, 10, len(frame)-2, len(frame)-1):
            result.loc[i] = [1, frame.iloc[i]['high']]
        return result
    def fvg(self, frame, join_consecutive):
        import pandas as pd
        self.join_consecutive = join_consecutive
        result = pd.DataFrame({'FVG': np.full(len(frame), np.nan), 'Top': np.full(len(frame), np.nan), 'Bottom': np.full(len(frame), np.nan)})
        result.loc[20] = [1, 104, 102]
        result.loc[len(frame)-1] = [1, 110, 105]
        return result




class AdapterTests(unittest.TestCase):
    def run_engines(self, rows, engines, as_of=None, tf='M1'):
        real_import = importlib.import_module
        def load(name):
            if name in ('smartmoneyconcepts',):
                if name not in engines:
                    raise ImportError('private path/account secret must not appear')
                return engines[name]
            return real_import(name)
        if as_of is None:
            as_of = rows[-1]['utc_time']+60
        with patch.object(a, '_load', side_effect=load):
            result = a.analyze_bars(rows, tf, as_of)
        json.dumps(result, allow_nan=False)
        return result




    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_smc_edges_fvg_and_missing_volume(self):
        rows = bars()
        for row in rows:
            row.pop('tick_volume')
        rows[10]['high'] = 110
        rows[21].update(open=105, high=107, low=104, close=106)
        engine = Smc()
        result = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=engine)})
        self.assertEqual(result['modules']['smc']['status'], 'READY')
        self.assertIsNone(result['modules']['smc']['volume_source'])
        self.assertNotIn('volume', engine.frames[0].columns)
        self.assertFalse(engine.join_consecutive)
        marks = result['overlays']['structure']
        self.assertEqual([m['kind'] for m in marks], ['swing', 'fvg'])
        self.assertEqual(marks[0]['start_time'], rows[10]['utc_time'])
        self.assertEqual(marks[1]['end_time'], rows[21]['utc_time'])
        self.assertTrue(all(m['known_at'] == rows[-1]['utc_time']+60 for m in marks))

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_volume_explicit_precedence(self):
        for real, tick, expected in ((0, 10, 'tick_volume'), (20, 10, 'real_volume'), (None, None, None)):
            with self.subTest(real=real):
                rows = bars()
                for row in rows:
                    row.update(real_volume=real, tick_volume=tick)
                engine = Smc()
                result = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=engine)})
                self.assertEqual(result['modules']['smc']['volume_source'], expected)
                self.assertFalse(result['modules']['smc']['volume_dependent_enabled'])
                if expected:
                    self.assertEqual(engine.frames[0]['volume'].iloc[0], rows[0][expected])


    def test_only_smc_imported_and_emitted(self):
        calls = []
        def unavailable(name):
            calls.append(name)
            raise ImportError('private secret')
        with patch.object(a, '_load', side_effect=unavailable):
            result = a.analyze_bars(bars(), 'M1', bars()[-1]['utc_time']+60)
        self.assertEqual(calls, ['smartmoneyconcepts'])
        self.assertEqual(set(result['modules']), {'smc'})
        self.assertEqual(result['overlays'], {'structure': []})
        self.assertNotIn('secret', json.dumps(result))

    def test_history_and_marks_remain_bounded(self):
        rows, now = a._prepare(bars(500), 'M1', bars(500)[-1]['utc_time']+60)
        self.assertEqual(len(rows), a.MAX_BARS)
        self.assertEqual(rows[0]['utc_time'], bars(500)[-a.MAX_BARS]['utc_time'])

    def test_warming_up_distinct_from_missing(self):
        result = self.run_engines(bars(2), {'smartmoneyconcepts': SimpleNamespace(smc=Smc())})
        self.assertEqual(result['modules']['smc']['status'], 'WARMING_UP')
        self.assertEqual(self.run_engines(bars(2), {})['modules']['smc']['status'], 'UNAVAILABLE')

    def test_invalid_fails_closed_without_importing(self):
        variants = []
        for key, value in [('high', 'nan'), ('close', 200), ('utc_time', -1), ('tick_volume', -1), ('open', True)]:
            rows = bars()
            rows[-1][key] = value
            variants.append(rows)
        rows = bars(); rows[-1]['utc_time'] = rows[0]['utc_time']; variants.append(rows)
        # Even an invalid future row invalidates the request rather than being
        # silently ignored before validation.
        rows = bars(); rows[-1].update(utc_time=1900000000, high='inf'); variants.append(rows)
        with patch.object(a, '_load', side_effect=AssertionError('must not import')):
            for rows in variants:
                result = a.analyze_bars(rows, 'M1', 1800000000)
                self.assertEqual(result['modules']['smc']['error'], 'INVALID_INPUT')
                self.assertFalse(any(result['overlays'].values()))
                json.dumps(result, allow_nan=False)


    def test_vendor_exceptions_do_not_leak_paths(self):
        with patch.object(a, '_load', side_effect=RuntimeError('/private/account/12345 secret')):
            result = a.analyze_bars(bars(), 'M1', bars()[-1]['utc_time']+60)
        self.assertEqual(result['modules']['smc']['error'], 'ANALYSIS_FAILED')
        self.assertNotIn('12345', json.dumps(result))

    def test_fractional_service_snapshot_time(self):
        rows = bars()
        now = rows[-1]['utc_time'] + 60.123456
        prepared, stamp = a._prepare(rows, 'M1', now)
        self.assertEqual(stamp, now)
        self.assertEqual(len(prepared), len(rows))
        self.assertEqual(a._time('2024-01-01T00:00:00.125Z', integral=False), 1704067200.125)
        with self.assertRaises(ValueError):
            a._time(rows[0]['utc_time'] + .1)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_bos_choch_filtered_input_and_closed_break(self):
        class Breaks(Smc):
            def bos_choch(self, frame, swings, close_break):
                import pandas as pd
                self.filtered, self.close_break = swings.copy(), close_break
                out = pd.DataFrame({k: np.full(len(frame), np.nan) for k in ('BOS', 'CHOCH', 'Level', 'BrokenIndex')})
                out.loc[10] = [1, 1, 110, 25]
                # Fabricated edge origin must not be surfaced.
                out.loc[0] = [1, 1, 100, 25]
                return out
        rows = bars(); rows[10]['high'] = 110
        rows[25].update(close=111, high=112)
        engine = Breaks()
        result = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=engine)})
        self.assertTrue(engine.close_break)
        self.assertTrue(np.isnan(engine.filtered.iloc[0]['HighLow']))
        self.assertTrue(np.isnan(engine.filtered.iloc[-1]['HighLow']))
        marks = [m for m in result['overlays']['structure'] if m['kind'] in ('bos', 'choch')]
        self.assertEqual(len(marks), 2)
        for mark in marks:
            self.assertEqual(mark['start_time'], rows[10]['utc_time'])
            self.assertEqual(mark['end_time'], rows[25]['utc_time'])
            self.assertEqual(mark['known_at'], rows[-1]['utc_time']+60)
            self.assertEqual([p['price'] for p in mark['points']], [110, 110])

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_bos_invalid_breaks_and_component_failures(self):
        import pandas as pd
        for level, broken in ((100, 1000), (100, 10), (100, 12.5), (float('inf'), 25), (0, 25), (110, 25), (100, float('nan'))):
            class InvalidBreaks(Smc):
                def bos_choch(self, frame, swings, close_break):
                    out = pd.DataFrame({k: np.full(len(frame), np.nan) for k in ('BOS', 'CHOCH', 'Level', 'BrokenIndex')})
                    out.loc[10] = [1, np.nan, level, broken]
                    return out
            rows = bars(); rows[10]['high'] = 110
            result = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=InvalidBreaks())})
            self.assertFalse(any(m['kind'] == 'bos' for m in result['overlays']['structure']))
        class BrokenComponent(Smc):
            def bos_choch(self, *args, **kwargs):
                raise RuntimeError('secret account')
        rows = bars(); rows[10]['high'] = 110
        result = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=BrokenComponent())})
        self.assertEqual(result['modules']['smc']['status'], 'READY')
        self.assertEqual(result['modules']['smc']['components']['bos_choch']['status'], 'ERROR')
        self.assertTrue(any(m['kind'] == 'swing' for m in result['overlays']['structure']))
        self.assertNotIn('secret', json.dumps(result))

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_liquidity_zones_sweep_and_copy(self):
        import pandas as pd
        class Liquidity(Smc):
            def swing_highs_lows(self, frame, swing_length):
                out = pd.DataFrame({'HighLow': np.full(len(frame), np.nan), 'Level': np.full(len(frame), np.nan)})
                for i in (0, 10, 30, len(frame)-1):
                    out.loc[i] = [1, frame.iloc[i]['high']]
                return out
            def liquidity(self, frame, swings, range_percent):
                self.received = swings.copy(deep=True)
                self.range_percent = range_percent
                # Simulate older vendor's in-place view mutation.
                swings.loc[:, 'HighLow'] = 0
                out = pd.DataFrame({k: np.full(len(frame), np.nan) for k in ('Liquidity', 'Level', 'End', 'Swept')})
                out.loc[10] = [1, np.float32(110.025), 30, 50 if len(frame) > 50 else 0]
                return out
        rows = bars(); rows[10]['high'] = 110; rows[30]['high'] = 110.05; rows[50]['high'] = 111
        engine = Liquidity()
        result = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=engine)})
        marks = [m for m in result['overlays']['structure'] if m['kind'].startswith('liquidity')]
        self.assertEqual(len(marks), 2)
        zone, sweep = marks
        self.assertEqual(zone['zone_state'], 'SWEPT')
        self.assertEqual(zone['end_time'], rows[50]['utc_time'])
        self.assertEqual(sweep['formed_at'], rows[50]['utc_time'])
        self.assertEqual(zone['pivot_count'], 2)
        self.assertEqual(zone['estimate_basis'], 'OHLC')
        self.assertEqual(zone['known_at'], rows[-1]['utc_time']+60)
        self.assertEqual(engine.range_percent, .01)
        self.assertTrue(np.isnan(engine.received.iloc[0]['HighLow']))
        self.assertTrue(np.isnan(engine.received.iloc[-1]['HighLow']))
        self.assertEqual(engine.received.iloc[10]['HighLow'], 1)
        # Same request repeated stays stable despite mutation in vendor.
        repeated = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=engine)})
        self.assertEqual(result, repeated)
        # Future sweep candle is in supplied data but not yet closed.
        before = self.run_engines(rows, {'smartmoneyconcepts': SimpleNamespace(smc=engine)}, rows[50]['utc_time']+30)
        before_marks = [m for m in before['overlays']['structure'] if m['kind'].startswith('liquidity')]
        self.assertEqual(len(before_marks), 1)
        self.assertEqual(before_marks[0]['zone_state'], 'UNSWEPT')
        self.assertEqual(before_marks[0]['end_time'], rows[49]['utc_time'])

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_liquidity_strict_indices_thresholds_and_level(self):
        import pandas as pd
        rows, now = a._prepare(bars(), 'M1', bars()[-1]['utc_time']+60)
        rows[10]['high'], rows[30]['high'], rows[50]['high'] = 110, 110.05, 111
        swings = pd.DataFrame({'HighLow': np.full(len(rows), np.nan), 'Level': np.full(len(rows), np.nan)})
        for i in (10, 30):
            swings.loc[i] = [1, rows[i]['high']]
        frame = pd.DataFrame(rows)
        def run(level=110.025, end=30, swept=50, direction=1):
            class Vendor:
                def liquidity(self, *args, **kwargs):
                    out = pd.DataFrame({k: np.full(len(rows), np.nan) for k in ('Liquidity', 'Level', 'End', 'Swept')})
                    out.loc[10] = [direction, level, end, swept]
                    return out
            return a._smc_liquidity(Vendor(), frame, swings, rows, now, pd)
        self.assertEqual(len(run()), 2)
        for value in (0, -1, float('nan'), float('inf'), 30.5, 100, 10, 77):
            self.assertEqual(run(end=value), [], value)
        for value in (-1, float('nan'), float('inf'), 50.5, 100, 10, 30, 49, 0):
            self.assertEqual(run(swept=value), [], value)
        for value in (0, -1, float('nan'), float('inf'), 110.1):
            self.assertEqual(run(level=value), [], value)
        # Last cluster member outside snapshot threshold is not a match.
        rows[30]['high'] = 110.14
        self.assertEqual(run(level=110.07), [])
        rows[30]['high'] = 110.05
        # Non-confirmed / wrong-side last swing must be rejected.
        swings.loc[30, 'HighLow'] = -1
        self.assertEqual(run(), [])

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_liquidity_bearish_unswept_zero_sentinel(self):
        import pandas as pd
        rows, now = a._prepare(bars(), 'M1', bars()[-1]['utc_time']+60)
        rows[10]['low'], rows[30]['low'] = 90, 90.05
        frame = pd.DataFrame(rows)
        swings = pd.DataFrame({'HighLow': np.full(len(rows), np.nan), 'Level': np.full(len(rows), np.nan)})
        for i in (10, 30):
            swings.loc[i] = [-1, rows[i]['low']]
        class Vendor:
            def liquidity(self, *args, **kwargs):
                out = pd.DataFrame({k: np.full(len(rows), np.nan) for k in ('Liquidity', 'Level', 'End', 'Swept')})
                out.loc[10] = [-1, 90.025, 30, 0]
                return out
        marks = a._smc_liquidity(Vendor(), frame, swings, rows, now, pd)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]['side'], 'sell_side')
        self.assertEqual(marks[0]['zone_state'], 'UNSWEPT')
        self.assertEqual(marks[0]['end_time'], rows[-1]['utc_time'])

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_active_liquidity_zone_keeps_reserved_budget(self):
        import pandas as pd
        rows, now = a._prepare(bars(150), 'M1', bars(150)[-1]['utc_time']+60)
        zone = a._mark('liquidity_zone', 'old active zone', 'neutral', 5, rows, now)
        zone.update(end_time=rows[-1]['utc_time'], zone_state='UNSWEPT')
        clutter = [a._mark('fvg', str(i), 'neutral', i, rows, now) for i in range(10, 150)]
        with patch.object(a, '_smc_swings', return_value=([], pd.DataFrame())), \
             patch.object(a, '_smc_fvg', return_value=clutter), \
             patch.object(a, '_smc_breaks', return_value=[]), \
             patch.object(a, '_smc_liquidity', return_value=[zone]):
            marks, _, _ = a._smc(SimpleNamespace(smc=object()), rows, now)
        self.assertEqual(len(marks), a.MAX_MARKS)
        self.assertIn(zone, marks)
        self.assertEqual(zone['end_time'], rows[-1]['utc_time'])


    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ('smartmoneyconcepts',)), 'real optional libraries not installed')
    def test_real_smc_confirmed_liquidity(self):
        # Real engines only. The test is optional in the baseline environment,
        # required in the dedicated analysis-dependency integration environment.
        rows = bars()
        for i, high in ((10, 110), (30, 110.05), (50, 111), (70, 109)):
            rows[i]['high'] = high
        for i, low in ((20, 90), (40, 90.5), (60, 90.2)):
            rows[i]['low'] = low
        now = rows[-1]['utc_time']+60.25
        result = a.analyze_bars(rows, 'M1', now)
        self.assertEqual(result['modules']['smc']['components']['liquidity']['status'], 'READY')
        zones = [m for m in result['overlays']['structure'] if m['kind'] == 'liquidity_zone' and m['side'] == 'buy_side']
        self.assertTrue(zones, result['modules'])
        zone = next(m for m in zones if m['formed_at'] == rows[10]['utc_time'])
        self.assertEqual(zone['last_pivot_time'], rows[30]['utc_time'])
        self.assertEqual(zone['end_time'], rows[50]['utc_time'])
        self.assertEqual(zone['known_at'], now)
        self.assertEqual(zone['zone_state'], 'SWEPT')
        self.assertTrue(any(m['kind'] == 'liquidity_sweep' and m['formed_at'] == rows[50]['utc_time'] for m in result['overlays']['structure']))
        before = a.analyze_bars(rows, 'M1', rows[50]['utc_time']+30)
        self.assertFalse(any(m['kind'] == 'liquidity_sweep' and m['formed_at'] >= rows[50]['utc_time'] for m in before['overlays']['structure']))
        early_zone = next(m for m in before['overlays']['structure'] if m['kind'] == 'liquidity_zone' and m['formed_at'] == rows[10]['utc_time'])
        self.assertEqual(early_zone['zone_state'], 'UNSWEPT')
        json.dumps(result, allow_nan=False)
        json.dumps(before, allow_nan=False)

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_fvg_rejects_invented_vendor_levels(self):
        import pandas as pd
        rows, now = a._prepare(bars(), 'M1', bars()[-1]['utc_time']+60)
        rows[21].update(open=105, high=107, low=104.1234567, close=106)
        frame = pd.DataFrame(rows)
        for top, bottom, expected_count in ((105, 102, 0), (104.1234567, 101, 0),
                                            (np.float32(104.1234567), 102, 1)):
            class Vendor:
                def fvg(self, *args, **kwargs):
                    out = pd.DataFrame({k: np.full(len(rows), np.nan) for k in ('FVG', 'Top', 'Bottom')})
                    out.loc[20] = [1, top, bottom]
                    return out
            marks = a._smc_fvg(Vendor(), frame, rows, now, pd)
            self.assertEqual(len(marks), expected_count)
            if marks:
                self.assertEqual(marks[0]['top'], rows[21]['low'])
                self.assertEqual(marks[0]['bottom'], rows[19]['high'])

    @unittest.skipUnless(importlib.util.find_spec("pandas"), "optional pandas not installed")
    def test_bos_rejects_level_unrelated_to_origin_swing(self):
        import pandas as pd
        rows, now = a._prepare(bars(), 'M1', bars()[-1]['utc_time']+60)
        rows[10]['high'] = 110.1234567
        rows[25].update(close=111, high=112)
        swings = pd.DataFrame({'HighLow': np.full(len(rows), np.nan), 'Level': np.full(len(rows), np.nan)})
        swings.loc[10] = [1, rows[10]['high']]
        for level, expected_count in ((100, 0), (np.float32(rows[10]['high']), 1)):
            class Vendor:
                def bos_choch(self, *args, **kwargs):
                    out = pd.DataFrame({k: np.full(len(rows), np.nan) for k in ('BOS', 'CHOCH', 'Level', 'BrokenIndex')})
                    out.loc[10] = [1, np.nan, level, 25]
                    return out
            marks = a._smc_breaks(Vendor(), pd.DataFrame(rows), swings, rows, now, pd)
            self.assertEqual(len(marks), expected_count)
            if marks:
                self.assertEqual(marks[0]['price'], rows[10]['high'])

    def test_all_timeframes_closed_boundary_and_iso(self):
        for tf, seconds in a.TIMEFRAME_SECONDS.items():
            rows = bars(80)
            for i, row in enumerate(rows):
                row['utc_time'] = 1700000000 + i*seconds
            now = rows[-1]['utc_time']+seconds-1
            prepared, _ = a._prepare(rows, tf, now)
            self.assertEqual(len(prepared), 79)
        self.assertEqual(a._time('2024-01-01T00:00:00Z'), 1704067200)
        with self.assertRaises(ValueError):
            a._time('2024-01-01T00:00:00')


if __name__ == '__main__':
    unittest.main()
