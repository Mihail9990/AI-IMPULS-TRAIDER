"""Original heuristic fixtures only; no live market data or third-party engine."""
import json
import unittest
from tradfi.analysis_patterns import analyze_patterns, MAX_FIGURES, RADIUS


def fixture(values):
    # Strictly monotonic ramps put fully enclosed pivots six closed bars apart.
    anchors = [(0, (values[0]+values[1])/2)] + [(6+6*i, value) for i, value in enumerate(values)]
    anchors.append((anchors[-1][0]+4, (values[-1]+values[-2])/2))
    rows = []
    for (i, a), (j, b) in zip(anchors, anchors[1:]):
        for k in range(i, j):
            p = a+(b-a)*(k-i)/(j-i)
            rows.append(dict(utc_time=1700000000+k*60, open=p, close=p, high=p+.1, low=p-.1))
    p = anchors[-1][1]
    rows.append(dict(utc_time=1700000000+anchors[-1][0]*60, open=p, close=p, high=p+.1, low=p-.1))
    return rows


def run(rows, as_of=None):
    return analyze_patterns(rows, 'M1', as_of if as_of is not None else rows[-1]['utc_time']+60.25)


class GeometryTests(unittest.TestCase):
    def test_seven_basic_families_and_snapshot_semantics(self):
        for name, values in [('double_top',[110,100,110]), ('double_bottom',[100,110,100]),
                             ('head_shoulders',[110,100,116,100,110]), ('inverse_head_shoulders',[100,110,94,110,100]),
                             ('ascending_triangle',[110,100,110,102,110,104]),
                             ('descending_triangle',[110,100,108,100,106,100]),
                             ('symmetric_triangle',[110,100,108,102,106,104])]:
            with self.subTest(name=name):
                rows=fixture(values)
                result=run(rows)
                marks=[m for m in result['overlays'] if m['kind']=='figure_'+name]
                self.assertTrue(marks, result)
                for mark in marks:
                    self.assertEqual(mark['status'],'EXPERIMENTAL_CANDIDATE')
                    self.assertEqual(mark['direction'],'neutral')
                    self.assertEqual(mark['known_at'],rows[-1]['utc_time']+60.25)
                    self.assertLessEqual(mark['confirmed_through'],mark['known_at'])
                    self.assertGreater(mark['confirmed_through'],mark['formed_at'])
                    self.assertTrue(all(p['time'] in [r['utc_time'] for r in rows] for p in mark['points']))
                json.dumps(result, allow_nan=False)

    def test_prefix_requires_three_future_closed_bars(self):
        rows=fixture([110,100,110])
        final=18
        for length in range(1,final+RADIUS+1):
            self.assertFalse(run(rows[:length])['overlays'], length)
        self.assertTrue(run(rows[:final+RADIUS+1])['overlays'])
        # Three confirming bars present, but final bar still open: unavailable.
        self.assertFalse(run(rows, rows[final+RADIUS]['utc_time']+59.99)['overlays'])
        self.assertTrue(run(rows, rows[final+RADIUS]['utc_time']+60)['overlays'])

    def test_no_flat_or_near_match_or_wrong_shoulders(self):
        for values in ([100,100,100], [110,100,114]):
            result=run(fixture(values))
            self.assertFalse(result['overlays'], (values,result))

        self.assertFalse(any('triangle' in m['kind'] for m in run(fixture([110,100,112,98,114,96]))['overlays']))
        for values in ([110,100,108,100,110], [110,100,116,104,110], [110,100,116,100,115]):
            self.assertFalse(any(m['kind']=='figure_head_shoulders' for m in run(fixture(values))['overlays']))

    def test_invalidation_and_expiry(self):
        rows=fixture([110,100,110])
        rows.append(dict(utc_time=rows[-1]['utc_time']+60,open=112,close=112,high=113,low=111))
        self.assertFalse(run(rows)['overlays'])
        rows=fixture([110,100,110])
        for _ in range(105):
            rows.append(dict(utc_time=rows[-1]['utc_time']+60,open=105,close=105,high=106,low=104))
        self.assertFalse(run(rows)['overlays'])

    def test_no_breakout_claim_for_wick_or_open_bar(self):
        rows=fixture([110,100,110])
        rows.append(dict(utc_time=rows[-1]['utc_time']+60,open=105,close=105,high=120,low=90))
        before=run(rows,rows[-1]['utc_time']+59)['overlays']
        after=run(rows)['overlays']
        self.assertTrue(before)
        self.assertTrue(all(m['status']=='EXPERIMENTAL_CANDIDATE' for m in before+after))
        self.assertTrue(all(m['direction']=='neutral' for m in before+after))

    def test_future_changes_do_not_change_past_snapshot(self):
        rows=fixture([110,100,110])
        cutoff=rows[21]['utc_time']+60.5
        expected=run(rows,cutoff)
        rows[-1].update(open=1000,high=1001,low=999,close=1000)
        self.assertEqual(run(rows,cutoff),expected)

    def test_invalid_input_and_bounds(self):
        for patch in ({'open':0},{'high':float('nan')},{'utc_time':1.5},{'low':True}):
            rows=fixture([110,100,110]);rows[3].update(patch)
            self.assertEqual(run(rows)['status'],'ERROR')
        self.assertEqual(analyze_patterns([], 'H4',1700000000)['status'],'ERROR')
        result=run(fixture([110,100]*100))
        self.assertLessEqual(len(result['overlays']),MAX_FIGURES)
        self.assertTrue(all('экспериментально' in m['label'] for m in result['overlays']))


if __name__=='__main__':
    unittest.main()
