"""Round-trip actual MQL wire helpers through the existing host compatibility shims."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from test_exact_order_compat import SHIM as BROKER
from test_reader_compat import SHIM as TEXT, as_cpp

HERE = Path(__file__).parent

def helper(name):
    source = (HERE / name).read_text()
    source = re.sub(r'^#include "[^"]+"\n', "", source, flags=re.M)
    source = re.sub(r"\(string\)((?:scope|value)\.\w+(?:\.\w+)?)", r"std::to_string(\1)", source)
    source = source.replace("string fields[];", "std::vector<string> fields;")
    return source

SOURCE = r"""
string DoubleToString(double x,int precision) {
   char buffer[128];
   std::snprintf(buffer,sizeof buffer,"%.*f",precision,x);
   return buffer;
}
"""
CASES = r"""
int main() {
   RecoveryExactReportScope request{{std::string(32,'1'),std::string(32,'2'),std::string(32,'3'),
      7,5,"Demo","XAUUSD.s",700,99},known};
   const std::string binding=request.binding.nonce+"\t"+request.binding.executor_epoch+"\t"+
      request.binding.verifier_epoch+"\t7\t5\tDemo\tXAUUSD.s\t700\t";
   const string prefix="RECOVERY_EXACT_ORDER_V1\t"+binding+"99\t2\t";
   assert(RecoveryExactReportRequestText(request)=="RECOVERY_EXACT_ORDER_REQUEST_V1\t"+
          binding+std::to_string(known)+"\tEND\n");
   RecoveryExactOrderScope scope{5,"Demo","XAUUSD.s",700,known};
   RecoveryExactOrderObservation fact;
   RecoveryExactReport got;
   assert(RecoveryObserveExactOrder(scope,fact)==RECOVERY_EXACT_ORDER_OBSERVED);
   string current=prefix+RecoveryExactOrderResponseBody(scope,fact);
   assert(RecoveryExactReportParse(current,request,got));
   assert(got.usable && got.observed && got.source=="current" && got.position_identifier=="0");
   mock={}; mock.current=false; mock.history=true; mock.pos=9007199254741100UL;
   assert(RecoveryObserveExactOrder(scope,fact)==RECOVERY_EXACT_ORDER_OBSERVED);
   string history=prefix+RecoveryExactOrderResponseBody(scope,fact);
   assert(RecoveryExactReportParse(history,request,got));
   assert(got.usable && got.observed && got.source=="history" && got.position_identifier=="9007199254741100");
   mock={}; mock.current=false;
   assert(RecoveryObserveExactOrder(scope,fact)==RECOVERY_EXACT_ORDER_UNKNOWN);
   string unknown=prefix+RecoveryExactOrderResponseBody(scope,fact);
   assert(RecoveryExactReportParse(unknown,request,got));
   assert(got.usable && !got.observed && got.reason=="unknown" && got.row.empty());

   auto bad_ticket=request; bad_ticket.order_ticket++;
   assert(!RecoveryExactReportParse(history,bad_ticket,got) && !got.usable && got.row.empty());
   auto bad_chart=request; bad_chart.binding.verifier_chart++;
   assert(!RecoveryExactReportParse(history,bad_chart,got) && !got.usable);
   auto bad_epoch=request; bad_epoch.binding.executor_epoch=std::string(32,'4');
   assert(!RecoveryExactReportParse(history,bad_epoch,got) && !got.usable);
   assert(!RecoveryExactReportParse(history.substr(0,history.size()-1),request,got) && !got.usable);
   assert(!RecoveryExactReportParse(history+"END\n",request,got) && !got.usable);
   string leading=history;
   leading.insert(leading.find("\t9007199254741100\t")+1,"0");
   assert(!RecoveryExactReportParse(leading,request,got) && !got.usable);
   string wrong_magic=history;
   wrong_magic.replace(wrong_magic.find("\t9007199254741100\t700")+18,3,"701");
   assert(!RecoveryExactReportParse(wrong_magic,request,got) && !got.usable);
   string excess=history;
   excess.replace(excess.find("0.0100000000000000"),18,"0.0300000000000000");
   assert(!RecoveryExactReportParse(excess,request,got) && !got.usable);
   string wrong_status=unknown;
   wrong_status.replace(wrong_status.find("\tunavailable\t"),13,"\tobserved\t");
   assert(!RecoveryExactReportParse(wrong_status,request,got) && !got.usable);
}
"""


class ExactWireCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_reader_writer_wire_roundtrip(self):
        code = (BROKER + TEXT + SOURCE + (HERE/"RecoveryExactOrder.mqh").read_text()
                + as_cpp((HERE/"RecoveryInventoryReader.mqh").read_text())
                + helper("RecoveryExactOrderWire.mqh")
                + helper("RecoveryExactOrderReader.mqh").split("// Publisher tag",1)[0]
                + "\n#endif\n" + CASES)
        with tempfile.TemporaryDirectory() as tmp:
            source, exe = Path(tmp)/"wire.cpp", Path(tmp)/"wire"
            source.write_text(code)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror",str(source),"-o",str(exe)],
                           check=True)
            subprocess.run([str(exe)],check=True)
    def test_bounded_separate_channel_and_create_only_paths(self):
        verifier = (HERE/"RecoveryVerifier.mq5").read_text()
        reader = (HERE/"RecoveryExactOrderReader.mqh").read_text()
        self.assertIn("ScanChannel(OrderSearchHandle,OrderSearchFilename,VERIFIER_EXACT_ORDER)",verifier)
        self.assertIn("PublishExact(nonce,generation,anchor)",verifier)
        self.assertIn('WriteReport(nonce,".order",',verifier)
        self.assertIn('const string request_path=prefix+".ord"',reader)
        self.assertIn("FileMove(temp,0,request_path,0)",reader)
        self.assertNotRegex(reader, r"\b(?:FILE_REWRITE|OrderSend(?:Async)?|CTrade)\b")


if __name__ == "__main__":
    unittest.main()
