"""Check actual translated MQL request/response text helpers off-terminal."""
import dataclasses
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from test_history_record import DEAL, ORDER, S, SECOND, wire
from test_reader_compat import SHIM, as_cpp

HOME = Path(__file__).parent

def history_as_cpp(text):
    text = text.split("// Publisher tag", 1)[0] + "\n#endif\n"
    text = text.replace('#include "RecoveryInventoryReader.mqh"', "")
    text = re.sub(r"\(string\)(scope\.\w+(?:\.\w+)?)", r"std::to_string(\1)", text)
    return (text.replace("const string &p[]", "const std::vector<string> &p")
            .replace("string fields[];", "std::vector<string> fields;")
            .replace("string rows[];", "std::vector<string> rows;")
            .replace("deal_tickets[128], rows[];", "deal_tickets[128]; std::vector<string> rows;"))


class HistoryReaderCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_history_wire_helpers(self):
        scope = dataclasses.replace(S, inventory_scope=dataclasses.replace(S.inventory_scope, server="Test-MT5"))
        valid = wire([ORDER], [DEAL, SECOND], scope=scope)
        deal_only = wire(deals=[DEAL], scope=scope)
        unavailable = wire(coverage="unavailable", reason="anchor_missing", scope=scope)
        bad = [
            valid[:-1], valid+b"END\n",
            valid.replace(b"\t1\t2\n", b"\t0\t2\n"),
            valid.replace(b"\t1\t2\n", b"\t1\t129\n"),
            valid.replace(b"\t314159\t", b"\t314158\t"),
            wire([ORDER, ORDER], scope=scope),
            wire(deals=[DEAL, DEAL], scope=scope),
            wire(deals=[DEAL.replace(f"\t{S.anchor_order}\t", "\t8\t")], scope=scope),
            wire([ORDER.replace("\t0.0000000000000000\t", "\t0.0300000000000000\t", 1)], scope=scope),
            wire(deals=[DEAL.replace("\t10001\t", "\t0\t")], scope=scope),
            wire(deals=[DEAL], coverage="unavailable", reason="anchor_missing", scope=scope),
            wire([ORDER], [DEAL], scope=dataclasses.replace(scope, position_identifier=5)),
            wire([ORDER], [DEAL], scope=dataclasses.replace(scope, anchor_order=5)),
        ]
        quoted = lambda payload: json.dumps(payload.decode())
        rb = scope.inventory_scope
        snippets = "\n".join(
            f"assert(RecoveryHistoryParse({quoted(valid)}, scope, got));"
            f"assert(!RecoveryHistoryParse({quoted(x)}, scope, got));"
            "assert(!got.usable && !got.observed && got.rows.empty() && got.sequence==0);"
            for x in bad
        )
        code = (SHIM + "\nusing ulong=std::uint64_t;\n"
                + as_cpp((HOME / "RecoveryInventoryReader.mqh").read_text())
                + history_as_cpp((HOME / "RecoveryHistoryReader.mqh").read_text()) + f"""
        int main() {{
          RecoveryHistoryScope scope{{{{
            "{rb.nonce}","{rb.executor_epoch}","{rb.verifier_epoch}",
            {rb.generation},{rb.login},"{rb.server}","{rb.symbol}",{rb.magic},{rb.verifier_chart}
          }},{scope.position_identifier}ULL,{scope.anchor_order}ULL}};
          assert(RecoveryHistoryRequestText(scope)=={quoted(scope.request_bytes())});
          auto wrong=scope;wrong.anchor_order=0;
          assert(RecoveryHistoryRequestText(wrong).empty());
          RecoveryHistorySnapshot got;
          assert(RecoveryHistoryParse({quoted(valid)}, scope, got));
          assert(got.usable && got.observed && got.order_count==1 && got.deal_count==2);
          assert(got.rows.at(1).find("3009.3000000000000000")!=string::npos);
          assert(RecoveryHistoryParse({quoted(deal_only)}, scope, got));
          assert(got.usable && got.observed && got.order_count==0 && got.deal_count==1);
          assert(RecoveryHistoryParse({quoted(unavailable)}, scope, got));
          assert(got.usable && !got.observed && got.rows.empty() && got.reason=="anchor_missing");
          {snippets}
        }}
        """)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "history.cpp"
            executable = Path(tmp) / "history"
            source.write_text(code)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            str(source), "-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)

    def test_transport_is_create_only_and_no_trade_api(self):
        source = (HOME / "RecoveryHistoryReader.mqh").read_text()
        self.assertIn("FileMove(temp,0,request_path,0)", source)
        self.assertIn('FileIsExist(prefix+".history")', source)
        self.assertIsNone(re.search(
            r"\b(?:FILE_REWRITE|OrderSend(?:Async)?|CTrade|TRADE_ACTION_\w+|PositionClose\w*)\b",
            source,
        ))


if __name__ == "__main__":
    unittest.main()
