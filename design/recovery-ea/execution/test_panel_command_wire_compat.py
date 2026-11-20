"""Cross-language command fixture: Python canonical SHA-256 vs MQL decoder with C++ shim."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))
sys.path.insert(0,str(ROOT/"verifier"))
from test_reader_compat import SHIM, as_cpp

IDS=[f"{n}"*8+"-"+f"{n}"*4+"-4"+f"{n}"*3+"-8"+f"{n}"*3+"-"+f"{n}"*12 for n in range(1,6)]
SCOPE,BOOT,CONTEXT,GEN,CMD=IDS
TWO={"ranges":[{"price":"3999.850","range_id":2,"side":"SELL"},
              {"price":"4000.00","range_id":1,"side":"BUY"}]}
def encode(obj):
    return json.dumps(obj,ensure_ascii=True,allow_nan=False,sort_keys=True,separators=(",",":"))
def record(operation="configure_ranges",payload=TWO,**changes):
    v=dict(version=1,scope_id=SCOPE,boot_id=BOOT,context_id=CONTEXT,generation_id=GEN,
           command_id=CMD,created_at=42,operation=operation,payload=payload)
    v.update(changes)
    v["content_sha256"]=hashlib.sha256(encode(v).encode("ascii")).hexdigest()
    return encode(v)
def one(price,id=1,side="BUY"):
    return {"ranges":[{"price":price,"range_id":id,"side":side}]}

class PanelCommandWireCompat(unittest.TestCase):
    def test_exact_queue_grammar_scope_and_hash(self):
        reader=(ROOT/"verifier"/"RecoveryInventoryReader.mqh").read_text()
        header=(HERE/"RecoveryPanelCommandWire.mqh").read_text()
        header="\n".join(line for line in header.splitlines() if not line.startswith("#include"))
        header=header.replace("uchar bytes[],key[],hash[];","std::vector<uchar> bytes,key,hash;")
        valid=record()
        expected="0d5bf5440ba4c46b2004cfe6a9267ed4845a67892ab626a113d4479f8c459a81"
        self.assertEqual(json.loads(valid)["content_sha256"],expected)
        bad=[
              record(boot_id=SCOPE), record(scope_id=BOOT), record(context_id=BOOT),
              record(generation_id=BOOT), record(command_id=BOOT), record(version=2),
              record(version=True),record(created_at=0.1),record(created_at=True),
              record(created_at=2**63),record(created_at=-2**63-1),
              record("manual_wait",TWO),record("manual_cancel",{"x":1}),record("market",{}),
              record(payload={}),record(payload={"ranges":[]}), record(payload={"ranges":[{},{}]}),
              record(payload={"ranges":TWO["ranges"]+[TWO["ranges"][0]]}),
              record(payload={"ranges": [TWO["ranges"][0]]*2}),
              record(payload=one("4000.00",0)),record(payload=one("4000.00",True)),
              record(payload=one("4000.00",3)),record(payload=one("4000.00",1,"LONG")),
              valid.replace("3999.850","3999.950"),valid.replace(expected,expected.upper()),
              valid.replace(expected,"0"*64),valid+"\n",valid[:-1],
              valid.replace('"payload":{','"payload":{}, "payload":{'),
              valid.replace('"ranges":[','"ranges": ['),
              valid.replace('"version":1','"version":1,"debug":false'),
              record(payload={"ranges":[{"price":"4000","range_id":1,"side":"BUY","tp":"1"}]}),
             ]
        for price in ("0","0.00","-4000","4000.001","4000.0000000000000001",
                      "4000.10000000000000000","4000.","04000.00","4e3","4E3","4000,00",
                      " 4000","4000\n","",".01","4000/1","1000000000000000","10000000001"):
            bad.append(record(payload=one(price)))
        main=f'''
int main() {{
  RecoveryPanelCommandScope scope{{"{SCOPE}","{BOOT}","{CONTEXT}","{GEN}"}};
  RecoveryPanelCommand cmd{{}};
  const string id="{CMD}";
  auto use=[&](const string &wire,double tick=0.01) {{ return RecoveryDecodePanelCommand(wire,id,scope,tick,cmd); }};
  assert(use({json.dumps(valid)}));
  assert(cmd.operation==RPC_CONFIGURE_RANGES && cmd.command_id==id && cmd.content_sha256=="{expected}" &&
         cmd.range_count==2 && cmd.ranges[0].range_id==2 && cmd.ranges[0].side==-1 && cmd.ranges[0].price_ticks==399985 &&
         cmd.ranges[1].range_id==1 && cmd.ranges[1].side==1 && cmd.ranges[1].price_ticks==400000);
  assert(use({json.dumps(record("manual_wait",{}))},0) && cmd.operation==RPC_MANUAL_WAIT && cmd.range_count==0);
  assert(use({json.dumps(record("manual_cancel",{},created_at=-2**63))},0) && cmd.operation==RPC_MANUAL_CANCEL);
  assert(use({json.dumps(record(payload=one("4000"),created_at=2**63-1))}) && cmd.ranges[0].price_ticks==400000);
  assert(use({json.dumps(record(payload=one("4000.05")))},0.05) && cmd.ranges[0].price_ticks==80001);
  assert(!use({json.dumps(record(payload=one("4000.03")))},0.05));
  assert(use({json.dumps(record(payload=one("10000000000")))}) && cmd.ranges[0].price_ticks==1000000000000L);
  assert(!use({json.dumps(valid)},0));
  assert(!use({json.dumps(valid)},0.011));
'''
        for wire in bad:
            main+=f'  assert(!use({json.dumps(wire)}) && cmd.operation==RPC_NONE && cmd.command_id.empty());\n'
        main+="}\n"
        shim=r'''
#include <cmath>
#include <iomanip>
#include <openssl/sha.h>
#include <sstream>
using uchar=unsigned char;
std::string DoubleToString(double q,int places){
 std::ostringstream v;v<<std::fixed<<std::setprecision(places)<<q;return v.str();
}
double StringToDouble(const string &s){return std::stod(s);}
bool MathIsValidNumber(double q){return std::isfinite(q);}
enum {CP_UTF8,CRYPT_HASH_SHA256};
int StringToCharArray(const string &s,std::vector<uchar> &bytes,int start,int n,int code_page){
 assert(start==0 && n==int(s.size()) && code_page==CP_UTF8);
 bytes.assign(s.begin(),s.end()); return int(bytes.size());
}
int ArraySize(const std::vector<uchar> &bytes){return int(bytes.size());}
int CryptEncode(int method,const std::vector<uchar> &data,const std::vector<uchar> &key,std::vector<uchar> &hash){
 assert(method==CRYPT_HASH_SHA256 && key.empty());hash.resize(SHA256_DIGEST_LENGTH);
 return SHA256(data.data(),data.size(),hash.data())!=nullptr ? int(hash.size()) : 0;
}
'''
        with tempfile.TemporaryDirectory() as folder:
            src=Path(folder)/"test.cpp"; exe=src.with_suffix("")
            src.write_text(SHIM+as_cpp(reader)+shim+header+main)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(src),"-lcrypto","-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
