"""Drive the pending chart timer with immutable verifier replies and an inert broker."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("RECOVERY_SOURCE_ROOT", HERE.parent)) / "execution"


class PendingChartRetry(unittest.TestCase):
    def test_frozen_ticket_survives_snapshot_retries_and_cancel(self):
        header = (SOURCE / "RecoveryManualPendingChart.mqh").read_text(encoding="utf-8")
        types = header[header.index("enum RecoveryPendingChartState"):header.index("bool RecoveryPendingChartManualOrder")]
        timer = header[header.index("void RecoveryManualPendingChartReset"):header.index("// Fast reverse execution")]
        for variable in ("hint", "serial"):
            timer = timer.replace("(string)" + variable, "std::to_string(" + variable + ")")
        fixture = (HERE / "test_pending_chart_retry.cpp").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pending.cpp"
            executable = source.with_suffix("")
            source.write_text(fixture.replace("// CHART TYPES", types).replace("// CHART TIMER", timer),
                              encoding="utf-8")
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-pedantic",
                            "-fsanitize=undefined", "-fno-sanitize-recover=all", str(source),
                            "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
