from pathlib import Path
import json
import subprocess
import sys
import textwrap
import unittest


class SchedulerReuseTest(unittest.TestCase):
    def run_probe(self, source):
        # Real scheduler workers live until process exit. Keep them isolated from the suite.
        setup = """\
import json
import threading
from trader.scheduler import scheduler_for

def worker_count():
    return sum(thread.name.startswith("capital-api-scheduler-")
               for thread in threading.enumerate())
"""
        result = subprocess.run(
            [sys.executable, "-c", setup + textwrap.dedent(source)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_repeated_lookup_reuses_workers_and_executes_task_once(self):
        result = self.run_probe("""
            schedulers = [scheduler_for("https://demo.example.test") for _ in range(3)]
            calls = []
            def task():
                calls.append("executed")
                return "done"
            value = schedulers[-1].execute("probe", 2, task, coalesce=False)
            print(json.dumps({
                "shared": all(item is schedulers[0] for item in schedulers),
                "workers": worker_count(), "calls": calls, "value": value,
            }))
        """)
        self.assertTrue(result["shared"])
        self.assertEqual(result["workers"], 4)
        self.assertEqual(result["calls"], ["executed"])
        self.assertEqual(result["value"], "done")

    def test_concurrent_lookup_creates_only_one_worker_group(self):
        result = self.run_probe("""
            from concurrent.futures import ThreadPoolExecutor

            barrier = threading.Barrier(8, timeout=5)
            def lookup():
                barrier.wait()
                return scheduler_for("https://demo.example.test")
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(lookup) for _ in range(8)]
                schedulers = [future.result(timeout=5) for future in futures]
            print(json.dumps({
                "shared": all(item is schedulers[0] for item in schedulers),
                "workers": worker_count(),
            }))
        """)
        self.assertTrue(result["shared"])
        self.assertEqual(result["workers"], 4)

    def test_different_hosts_keep_separate_worker_groups(self):
        result = self.run_probe("""
            demo = scheduler_for("https://demo.example.test")
            real = scheduler_for("https://real.example.test")
            demo_again = scheduler_for("https://demo.example.test")
            real_again = scheduler_for("https://real.example.test")
            print(json.dumps({
                "distinct": demo is not real,
                "demo_reused": demo_again is demo,
                "real_reused": real_again is real,
                "workers": worker_count(),
            }))
        """)
        self.assertTrue(result["distinct"])
        self.assertTrue(result["demo_reused"])
        self.assertTrue(result["real_reused"])
        self.assertEqual(result["workers"], 8)


if __name__ == "__main__":
    unittest.main()
