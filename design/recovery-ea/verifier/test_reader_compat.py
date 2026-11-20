"""Exercise the MQL text parser via a limited C++ shim, not MetaEditor."""
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import unittest

from test_record import P, O, SCOPE, wire


SHIM = r"""
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
using string=std::string;
using ushort=unsigned short;
int StringLen(const string &s){return static_cast<int>(s.size());}
ushort StringGetCharacter(const string &s,int i){return static_cast<ushort>(static_cast<unsigned char>(s.at(i)));}
int StringFind(const string &s,const string &q,int start=0){
  auto pos=s.find(q,start);
  return pos==string::npos ? -1 : static_cast<int>(pos);
}
string StringSubstr(const string &s,int start,int n=-1){return s.substr(start,n);}
int StringCompare(const string &a,const string &b){return a.compare(b);}
long StringToInteger(const string &s){return std::stol(s);}
int StringSplit(const string &s,int sep,std::vector<string>& out) {
  out.clear();
  if(s.empty()) return 0;
  size_t at=0;
  while(true){
    auto pos=s.find(static_cast<char>(sep),at);
    if(pos==string::npos){out.push_back(s.substr(at));break;}
    out.push_back(s.substr(at,pos-at));at=pos+1;
  }
  return static_cast<int>(out.size());
}
int ArraySize(const std::vector<string>& a){return static_cast<int>(a.size());}
int ArrayResize(std::vector<string>& a,int n){a.resize(n);return n;}
void ArrayFree(std::vector<string>& a){a.clear();}
int ArrayCopy(std::vector<string>& d,const std::vector<string>& s){d=s;return ArraySize(d);}
"""

def as_cpp(s):
    """Translate only MQL string casts and known local/parameter arrays."""
    s = s.split("bool RecoveryInvRead(", 1)[0] + "\n#endif\n"
    s = re.sub(r"\(string\)(expected\.\w+)", r"std::to_string(\1)", s)
    s = s.replace("const string &fields[]", "const std::vector<string> &fields")
    s = s.replace("string fields[];", "std::vector<string> fields;")
    s = s.replace("string rows[];", "std::vector<string> rows;")
    s = s.replace("order_tickets[128], rows[];", "order_tickets[128]; std::vector<string> rows;")
    return s


class ReaderCompat(unittest.TestCase):
    @unittest.skipUnless(shutil.which("c++"), "C++ compiler unavailable")
    def test_synthetic_records(self):
        # Use ASCII to avoid claiming the shim simulates MQL's UTF-16 storage.
        import dataclasses
        scope=dataclasses.replace(SCOPE, server="Test-MT5")
        valid=wire([P], [O], scope=scope)
        unavailable=wire(coverage="unavailable", reason="changed", scope=scope)
        invalid_cases = [
            valid[:-1], valid+b"END\n",
            valid.replace(b"\t1\t1\n", b"\t2\t1\n"),
            valid.replace(b"\t314159\t", b"\t314158\t"),
            valid.replace(b"\t0.0200000000000000\t", b"\t0.0020000000000000\t"),
            wire([P, P], scope=scope),
            wire(orders=[O, O], scope=scope),
            wire([P.replace("9007199254740993", "18446744073709551616")], scope=scope),
            wire([P], coverage="unavailable", reason="read", scope=scope),
        ]
        source=Path(__file__).with_name("RecoveryInventoryReader.mqh").read_text()
        cases="\n".join(f"assert(!RecoveryInvParse({json.dumps(x.decode())}, scope, got));\n"
                        "assert(!got.usable && !got.complete && got.rows.empty() && got.sequence==0);"
                        for x in invalid_cases)
        code=SHIM+as_cpp(source)+f"""
        int main() {{
          RecoveryInventoryScope scope{{
           "{scope.nonce}","{scope.executor_epoch}","{scope.verifier_epoch}",
           {scope.generation},{scope.login},"{scope.server}","{scope.symbol}",{scope.magic},{scope.verifier_chart}
          }};
          RecoveryInventorySnapshot got;
          assert(RecoveryInvParse({json.dumps(valid.decode())}, scope, got));
          assert(got.usable && got.complete && got.position_count==1 && got.order_count==1);
          assert(got.rows.at(0).find("9007199254740993")!=string::npos);
          assert(RecoveryInvParse({json.dumps(unavailable.decode())}, scope, got));
          assert(got.usable && !got.complete && got.rows.empty() && got.reason=="changed");
          {cases}
        }}
        """
        with tempfile.TemporaryDirectory() as tmp:
            test=Path(tmp)/"verify.cpp"
            binary=Path(tmp)/"verify"
            test.write_text(code)
            subprocess.run(["c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            str(test), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
