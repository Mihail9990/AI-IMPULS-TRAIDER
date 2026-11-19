import unittest
from itertools import repeat
from recovery.runtime import IsolatedRuntime


class Worker:
    simulation_only = True
    def __init__(self, rid, fail=None):
        self.range_id, self.fail, self.calls = rid, fail, []
    def action(self, stage):
        self.calls.append(stage)
        if self.fail == stage:
            raise ValueError('unresolved evidence')
    def reconcile(self): self.action('reconcile')
    def protect(self, quote): self.action('protect')
    def synchronize(self): self.action('synchronize')


class RuntimeTests(unittest.TestCase):
    def runtime(self, workers, **kw):
        return IsolatedRuntime(workers, preflight=kw.get('preflight', lambda: None),
                               record_failure=kw.get('record_failure', lambda f: None),
                               simulation_only=True)

    def test_isolation_each_stage_and_quarantine(self):
        for stage in ('reconcile', 'protect', 'synchronize'):
            a, b = Worker('a', stage), Worker('b')
            recorded = []
            r = self.runtime([a, b], record_failure=recorded.append)
            result = r.step((1, 2))
            self.assertEqual(result.completed, ('b',))
            self.assertEqual(recorded[0].stage, stage)
            before = list(a.calls)
            r.step((1, 2))
            self.assertEqual(a.calls, before)
            self.assertEqual(len(b.calls), 6)

    def test_preflight_failure_prevents_all_work(self):
        a = Worker('a')
        def fail(): raise ValueError('account changed')
        with self.assertRaises(ValueError): self.runtime([a], preflight=fail).step(None)
        self.assertEqual(a.calls, [])

    def test_record_failure_aborts_cycle(self):
        a, b = Worker('a', 'reconcile'), Worker('b')
        def fail(f): raise OSError('disk unavailable')
        r = self.runtime([a, b], record_failure=fail)
        with self.assertRaises(OSError): r.step(None)
        self.assertEqual(b.calls, [])
        with self.assertRaises(RuntimeError): r.step(None)

    def test_live_mode_and_duplicate_rejected(self):
        with self.assertRaises(RuntimeError):
            IsolatedRuntime([Worker('a')], preflight=lambda: None, record_failure=lambda f: None)
        with self.assertRaises(ValueError): self.runtime([Worker('a'), Worker('a')])
        a = Worker('a'); a.simulation_only = False
        with self.assertRaises(RuntimeError): self.runtime([a])

    def test_bounded_and_closed(self):
        r = self.runtime([Worker('a')])
        self.assertEqual(len(r.run_steps(repeat(None), max_steps=3)), 3)
        self.assertEqual(r.run_steps(repeat(None), max_steps=0), [])
        with self.assertRaises(ValueError): r.run_steps([], max_steps=True)
        r.close(); r.close()
        with self.assertRaises(RuntimeError): r.step(None)

    def test_recursive_cycle_forbidden(self):
        r = self.runtime([Worker('a')])
        r._preflight = lambda: r.step(None)
        with self.assertRaises(RuntimeError): r.step(None)

    def test_preflight_cannot_reassign_worker_or_enable_live(self):
        for field, value in (('range_id', 'other'), ('simulation_only', False)):
            a = Worker('a')
            r = self.runtime([a], preflight=lambda: setattr(a, field, value))
            with self.assertRaises(RuntimeError): r.step(None)
            self.assertEqual(a.calls, [])
            with self.assertRaises(RuntimeError): r.step(None)

    def test_each_stage_cannot_mutate_own_or_peer_authority(self):
        for stage in ('reconcile', 'protect', 'synchronize'):
            for field, value in (('range_id', 'other'), ('simulation_only', False)):
                for peer in (False, True):
                    a, b = Worker('a'), Worker('b')
                    target = b if peer else a
                    original = getattr(a, stage)
                    def mutate(*args):
                        original(*args)
                        setattr(target, field, value)
                    setattr(a, stage, mutate)
                    r = self.runtime([a, b])
                    with self.assertRaises(RuntimeError): r.step(None)
                    self.assertEqual(a.calls[-1], stage)
                    self.assertEqual(b.calls, [])
                    with self.assertRaises(RuntimeError): r.step(None)

    def test_failure_sink_cannot_reassign_peer(self):
        a, b = Worker('a', 'reconcile'), Worker('b')
        r = self.runtime([a, b], record_failure=lambda f: setattr(b, 'range_id', 'c'))
        with self.assertRaises(RuntimeError): r.step(None)
        self.assertEqual(b.calls, [])

    def test_mutation_then_exception_is_global_not_local(self):
        a, b = Worker('a'), Worker('b')
        recorded = []
        def mutate():
            b.range_id = 'other'
            raise ValueError('local error')
        a.reconcile = mutate
        r = self.runtime([a, b], record_failure=recorded.append)
        with self.assertRaises(RuntimeError): r.step(None)
        self.assertEqual(recorded, [])
        self.assertEqual(b.calls, [])


if __name__ == '__main__': unittest.main()
