import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from trade_wakeup import PULSE_NAME, TradeWakeup


class WakeupTest(unittest.TestCase):
    def test_real_shared_directory_notifications_and_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pulse = root / PULSE_NAME
            pulse.write_bytes(b"initial")
            with TradeWakeup(root) as wake:
                self.assertTrue(wake.handle)
                (root / "unrelated.req").write_bytes(b"unrelated")
                self.assertFalse(wake.wait(.04))
                # A genuine overwrite while already waiting must release the wait.
                writer = threading.Timer(.04, pulse.write_bytes, (b"new-event",))
                writer.start()
                try:
                    self.assertTrue(wake.wait(2))
                finally:
                    writer.join()
                for i in range(4):
                    temp = root / "pending.tmp"
                    temp.write_bytes(bytes(i + 1))
                    os.replace(temp, pulse)
                self.assertTrue(wake.wait(1))
                self.assertFalse(wake.wait(.04))
            self.assertIsNone(wake.handle)

    def test_missing_directory_falls_back_and_later_attaches(self):
        with tempfile.TemporaryDirectory() as directory:
            scope = Path(directory) / "later"
            with TradeWakeup(scope) as wake:
                start = time.monotonic()
                self.assertFalse(wake.wait(.03))
                self.assertGreaterEqual(time.monotonic() - start, .02)
                scope.mkdir()
                wake.retry_at = 0
                self.assertFalse(wake.wait(.02))
                (scope / PULSE_NAME).write_bytes(b"event")
                self.assertTrue(wake.wait(1))


if __name__ == "__main__":
    unittest.main()
