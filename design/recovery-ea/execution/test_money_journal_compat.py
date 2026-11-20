"""Run the close-to-money journal against controlled terminal history and file operations."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "verifier"))
from test_closed_money_compat import SHIM  # existing exact-deal MT5 simulation


class MoneyJournal(unittest.TestCase):
    def test_verified_exit_retry_and_durable_idempotence(self):
        money = (HERE.parent / "verifier" / "RecoveryClosedMoney.mqh").read_text()
        for typename, name in (("RecoveryCloseMoneyExit", "exits"), ("RecoveryCloseMoneyRow", "rows")):
            money = money.replace(f"const {typename} &{name}[]", f"const std::vector<{typename}> &{name}")
        money = money.replace("RecoveryCloseMoneyRow first[];", "std::vector<RecoveryCloseMoneyRow> first;")
        journal = (HERE / "RecoveryMoneyJournal.mqh").read_text()
        journal = "\n".join(line for line in journal.splitlines() if not line.startswith("#include"))
        journal = re.sub(r"const uchar &bytes\[\]", "const std::vector<uchar> &bytes", journal)
        journal = re.sub(r"\b(RecoveryMoneyJob|RecoveryCloseMoneyExit|uchar|string) (\w+)\[\];",
                         r"std::vector<\1> \2;", journal)
        journal = re.sub(r"\(string\)(ChartID\(\)|job\.scope\.login|b\.key\.(?:position_id|anchor_order)|sum)",
                         r"std::to_string(\1)", journal)
        journal = journal.replace("(ulong)count", "static_cast<ulong>(count)")
        fixture = (HERE / "test_money_journal.cpp").read_text().replace(
            '#include "MoneyJournalUnderTest"', journal)
        with tempfile.TemporaryDirectory() as directory:
            cpp = Path(directory) / "money.cpp"
            exe = Path(directory) / "money"
            cpp.write_text(SHIM + money + fixture)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=undefined", str(cpp), "-o", str(exe)], check=True)
            subprocess.run([str(exe)], check=True)

    def test_runtime_hooks_follow_independent_close_and_trading_timer(self):
        poll = (HERE / "RecoveryRangeClosePoll.mqh").read_text()
        success = poll.split("status=RCCPObserveFromReports(", 1)[1].split("return RCP_PROOF", 1)[0]
        self.assertLess(success.index("RecoveryMoneyEnqueue(task.binding,task.history,observed)"),
                        success.index("!RCCPDerive(observed"))
        self.assertEqual(poll.count("RecoveryMoneyEnqueue("), 1)
        timer = (HERE / "RecoveryTrader.mq5").read_text().split("void OnTimer()", 1)[1]
        self.assertLess(timer.index("RecoveryChartOnTimer("), timer.index("RecoveryMoneyJournalTimer("))
        self.assertNotIn("RecoveryMoneyJobs", (HERE / "RecoveryChartRuntime.mqh").read_text())


if __name__ == "__main__":
    unittest.main()
