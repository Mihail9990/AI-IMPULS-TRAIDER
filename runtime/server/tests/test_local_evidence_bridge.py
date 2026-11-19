import tempfile
import unittest
from pathlib import Path
from decimal import Decimal
from dataclasses import replace
from recovery.ea_stop_evidence import ExitBinding, StopEvidenceUnavailable
from recovery.local_evidence_bridge import LocalEvidenceBridge

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bridge=LocalEvidenceBridge(Path(self.tmp.name))
        self.b=ExitBinding('a'*32,123,'Broker',10,11,12,'XAUUSD.s','SELL',1000,Decimal('.01'))
    def response(self,b=None):
        b=b or self.b
        return ('\t'.join(['RECOVERY_DEAL_SL_V1',b.request_id,str(b.account),b.server,str(b.deal),str(b.order),str(b.position),b.symbol,b.exit_side,str(b.time_msc),format(b.volume,'.16f'),'3999.3000000000000000','END'])+'\r\n').encode()
    def test_pending_and_exact_response(self):
        self.bridge.publish(self.b)
        with self.assertRaises(StopEvidenceUnavailable): self.bridge.collect(self.b)
        (Path(self.tmp.name)/(self.b.request_id+'.response')).write_bytes(self.response())
        self.assertEqual(self.bridge.collect(self.b),Decimal('3999.3'))
        self.assertEqual(self.bridge.collect(self.b),Decimal('3999.3'))
    def test_collision(self):
        p=self.bridge.publish(self.b)
        self.assertEqual(self.bridge.publish(self.b),p)
        with self.assertRaises(ValueError): self.bridge.publish(replace(self.b,deal=20))
    def test_foreign_response(self):
        self.bridge.publish(self.b)
        (Path(self.tmp.name)/(self.b.request_id+'.response')).write_bytes(self.response(replace(self.b,account=321)))
        with self.assertRaises(StopEvidenceUnavailable): self.bridge.collect(self.b)
    def test_oversize_and_symlink(self):
        self.bridge.publish(self.b)
        p=Path(self.tmp.name)/(self.b.request_id+'.response')
        p.write_bytes(b'x'*8193)
        with self.assertRaises(StopEvidenceUnavailable): self.bridge.collect(self.b)
        p.unlink(); p.symlink_to(Path(self.tmp.name)/(self.b.request_id+'.request'))
        with self.assertRaises(ValueError): self.bridge.collect(self.b)
    def test_invalid_nonce(self):
        with self.assertRaises(ValueError): self.bridge.publish(replace(self.b,request_id='../bad'))
    def test_request_crlf_wire_contract(self):
        data=self.bridge.publish(self.b).read_bytes()
        self.assertTrue(data.endswith(b'\r\n'))
        self.assertEqual(data.count(b'\r\n'),1)
        self.assertNotIn(b'\n',data[:-2])
        self.assertNotIn(b'\r',data[:-2])
        self.assertEqual(len(data[:-2].decode('utf-8').split('\t')),12)

    def test_actual_ea_cursor_functions_with_cpp_stubs(self):
        # Execute the exact cursor code, not a Python reimplementation. This cannot
        # establish native MQL5 compilation or Windows enumeration semantics.
        import shutil
        import subprocess
        compiler=shutil.which('g++')
        if compiler is None: self.skipTest('C++ stub harness requires g++')
        source=(Path(__file__).parents[1]/'recovery/mql5/RecoveryEvidenceBridge.mq5').read_text()
        cursor=source[source.index('long SearchHandle='):source.index('void ExportOne()')]
        harness=r'''
#include <string>
#include <cassert>
using string=std::string;
const long INVALID_HANDLE=-1;
bool connected=true;
int index_=0, starts=0, closes=0, visits=0, exports=0, total=385;
string RequestId;
bool SessionOK(){return connected;}
long FileFindFirst(string, string &name){starts++;index_=0;name="0";return 42;}
bool FileFindNext(long h,string &name){assert(h==42);index_++;name=std::to_string(index_);return index_<total;}
void FileFindClose(long h){assert(h==42);closes++;}
bool LoadRequest(string name){visits++; RequestId=name;return index_!=130;}
bool FileIsExist(string){return index_==131;}
void ExportOne(){if(index_==384) exports++;} // first 384 unresolved/invalid/completed
void EventKillTimer(){}
'''+cursor+r'''
int main(){
OnTimer(); assert(visits==128 && starts==1 && closes==0);
OnTimer(); assert(visits==256 && starts==1 && closes==0);
OnTimer(); assert(visits==384 && starts==1 && closes==0);
OnTimer(); assert(visits==385 && exports==1 && closes==1);
OnTimer(); assert(visits==513 && starts==2);
connected=false; OnTimer(); assert(visits==513 && closes==2);
connected=true; OnTimer(); assert(starts==3 && visits==641);
OnDeinit(0); assert(closes==3 && SearchHandle==INVALID_HANDLE);
}
'''
        p=Path(self.tmp.name)/'cursor.cpp'; p.write_text(harness)
        exe=Path(self.tmp.name)/'cursor-test'
        subprocess.run([compiler,'-std=c++11',str(p),'-o',str(exe)],check=True,capture_output=True)
        subprocess.run([str(exe)],check=True,capture_output=True)
