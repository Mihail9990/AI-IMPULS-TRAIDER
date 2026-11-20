"""Offline fault injection for the actual MQL intent journal and restart route."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from test_intent_journal_compat import MOCK

SCENARIOS = r"""
int main(){
    RecoveryIntentKey unknown{string(32,'a'),12,"Test-Server","XAUUSD.s",18,3,string(64,'b')};
    RecoveryIntentKey known{string(32,'c'),12,"Test-Server","XAUUSD.s",18,3,string(64,'d')};
    RecoveryRestartDecision out{};
    int accepted=0;
    assert(RecoveryIntentReserve(unknown));
    ++accepted; // Broker accepted. Process interrupted before persisting result.order.
    assert(RecoveryIntentReserve(known));
    ++accepted;
    assert(RecoveryIntentRecordTicket(known,9007199254740993ULL));
    // New process sees the same immutable files. No result/request object survives.
    auto file_snapshot=files;
    RecoveryIntentRestart(unknown,out);
    assert(out.route==RR_TICKET_UNKNOWN && out.order_ticket.empty() && out.position_id.empty());
    assert(RecoveryRestartLogReason(out.route)=="broker_ticket_unknown");
    assert(!RecoveryIntentReserve(unknown)); // Even a caller error cannot re-reserve.
    assert(accepted==2 && files==file_snapshot);
    RecoveryIntentRestart(known,out);
    assert(out.route==RR_READ_EXACT_ORDER && out.order_ticket=="9007199254740993" && out.position_id.empty());
    assert(!RecoveryIntentRecordPositionLink(known,"9007199254740992",111));
    assert(!RecoveryIntentRecordPositionLink(unknown,"9007199254740993",111));
    assert(RecoveryIntentRecordPositionLink(known,"9007199254740993",111));
    assert(!RecoveryIntentRecordPositionLink(known,"9007199254740993",112));
    file_snapshot=files;
    RecoveryIntentRestart(known,out);
    assert(out.route==RR_READ_EXACT_HISTORY && out.order_ticket=="9007199254740993" && out.position_id=="111");
    assert(RecoveryRestartLogReason(out.route)=="check_exact_history");
    const auto path=RecoveryIntentPath(known)+".position";
    files[path][0]='X';
    RecoveryIntentRestart(known,out);
    assert(out.route==RR_JOURNAL_CONFLICT && out.order_ticket.empty() && out.position_id.empty());
    files=file_snapshot;
    auto absent=known; absent.id=string(32,'f');
    RecoveryIntentRestart(absent,out);
    assert(out.route==RR_NOT_RECORDED && out.order_ticket.empty() && out.position_id.empty());
    auto wrong=known; wrong.fingerprint=string(64,'e');
    RecoveryIntentRestart(wrong,out);
    assert(out.route==RR_JOURNAL_CONFLICT && out.order_ticket.empty() && out.position_id.empty());
    connected=false;
    RecoveryIntentRestart(known,out);
    assert(out.route==RR_TERMINAL_UNAVAILABLE);
    assert(accepted==2 && files==file_snapshot);
}
"""

class TestRestart(unittest.TestCase):
    def test_accepted_then_crash_before_ticket_does_not_redispatch(self):
        gxx=shutil.which("g++")
        self.assertIsNotNone(gxx)
        paths=[Path(__file__).with_name(name) for name in
               ("RecoveryIntentJournal.mqh","RecoveryIntentRestart.mqh")]
        source="\n".join(line for p in paths for line in p.read_text().splitlines()
                         if not line.startswith("#include"))
        self.assertNotRegex(source,r"OrderSend|TRADE_ACTION|CTrade")
        for name in ("bytes","normalized"):
            source=source.replace(f"uchar {name}[];",f"std::vector<uchar> {name};")
        for expr in ("key.login","key.magic","key.generation","ChartID()","ticket","verified_position_id"):
            source=source.replace("(string)"+expr,f"std::to_string({expr})")
        with tempfile.TemporaryDirectory() as folder:
            cpp=Path(folder)/"restart.cpp"
            exe=cpp.with_suffix("")
            cpp.write_text(MOCK+source+"\n"+SCENARIOS)
            subprocess.run([gxx,"-std=c++17","-Wall","-Wextra","-Werror","-fsanitize=undefined",
                            str(cpp),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
