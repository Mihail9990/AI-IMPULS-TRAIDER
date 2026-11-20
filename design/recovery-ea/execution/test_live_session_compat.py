"""The real start/stop helper with a fake MT5 file runtime; no terminal or trades."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))
MOCK=r"""
#include <cassert>
#include <cstdio>
#include <map>
#include <set>
#include <string>
#include <vector>
using string=std::string;
using uchar=unsigned char;
using uint=unsigned;
const int ACCOUNT_LOGIN=1,ACCOUNT_SERVER=2,TERMINAL_CONNECTED=3,FILE_READ=4,FILE_WRITE=8,
 FILE_BIN=16,FILE_REWRITE=32,CP_UTF8=33,INVALID_HANDLE=-1,ERR_FILE_NOT_EXIST=5019;
bool connected=true,writer_open=false,fail_move=false;
long login=7;
string _Symbol="XAUUSD.s",server="server",current;
std::set<string> dirs;
std::map<string,string> files;
int StringLen(const string& s){return s.size();}
string StringSubstr(const string& s,int at,int n){return s.substr(at,n);}
int StringFind(const string& s,const string& part,int start=0){
 const auto at=s.find(part,start);return at==string::npos ? -1 : static_cast<int>(at);
}
bool TerminalInfoInteger(int){return connected;}
long AccountInfoInteger(int){return login;}
string AccountInfoString(int){return server;}
long ChartID(){return 99;}
long TimeLocal(){return 10000;}
template<class T> void ZeroMemory(T& v){v=T{};}
int last_error=0;
void ResetLastError(){last_error=0;}
int GetLastError(){return last_error;}
bool FileIsExist(const string& s){
 if(files.count(s))return true;
 last_error=dirs.count(s)?5018:ERR_FILE_NOT_EXIST;return false;
}
bool FolderCreate(const string& s){dirs.insert(s);return true;}
int FileOpen(const string& path,int flags){
 if(path=="RECOVERY_PANEL\\writer.lock"){
   assert(flags==(FILE_READ|FILE_WRITE|FILE_BIN));
   if(writer_open)return INVALID_HANDLE;
   writer_open=true;return 1;
 }
 assert(flags==(FILE_BIN|FILE_WRITE));current=path;files[path]="";return 2;
}
void FileClose(int h){if(h==1)writer_open=false;}
uint FileWriteArray(int,std::vector<uchar>& b,int,int){
 files[current]=string(b.begin(),b.end());return b.size();
}
void FileFlush(int){}
bool FileDelete(const string& f){
 if(f=="RECOVERY_PANEL\\state.json")assert(writer_open);
 return files.erase(f);
}
bool RecoveryPanelMailboxRead(const string& path,string& wire){
 if(!files.count(path))return false;
 wire=files.at(path);return true;
}
bool FileMove(const string& a,int,const string& b,int flags){
 assert(flags==FILE_REWRITE);if(fail_move)return false;
 files[b]=files.at(a);files.erase(a);return true;
}
int StringToCharArray(const string& s,std::vector<uchar>& b,int,int,int){
 b.assign(s.begin(),s.end());return b.size();
}
string RecoverySessionSha256(const string& s){
 const auto h=std::hash<string>{}(s);
 char b[65];std::snprintf(b,sizeof(b),"%016lx%016lx%016lx%016lx",h,h,h,h);return b;
}
struct RecoveryPanelCommandScope {string scope_id,boot_id,context_id,generation_id;};
struct RecoveryPanelMailbox{int dummy;};
struct RecoveryPanelApplySession{int seen;};
enum RecoveryControlMode {REC_CONTROL_IDLE,REC_CONTROL_MANUAL_WAIT};
void RecoveryPanelMailboxClose(RecoveryPanelMailbox& m){m.dummy=0;}
void RecoveryClearPanelApply(RecoveryPanelApplySession& r){r.seen=0;}
bool RecoveryPanelCommandUUID(const string& s){return s.size()==36&&s[8]=='-';}
string RecoveryPanelBootEpoch(string s){for(int i=0;i<4;i++)s.erase(s.find('-'),1);return s;}
string RecoveryPanelMailboxBootPath(const string& b){return RecoveryPanelCommandUUID(b)?"RECOVERY_PANEL\\sessions\\"+b:"";}
"""
TEST=r"""
#include "RecoverySessionLifecycle.mqh"
#include "RecoveryLiveSession.mqh"
int main(){
 RecoveryLiveSession a{},b{};
 const RecoverySessionContext live{99,7,90,"server","XAUUSD.s","fixed"};
 const string scope="11111111-1111-4111-8111-111111111111";
 const string state="RECOVERY_PANEL\\state.json";
 assert(RecoveryLiveSessionStart(a,live,scope,"seed1")&&writer_open);
 assert(!RecoveryLiveSessionStart(b,live,scope,"seed2"));
 assert(RecoveryLiveSessionCurrent(a,live));
 const auto first=a.mail.boot_id;
 assert(RecoveryLiveSessionPublish(a,live,REC_CONTROL_IDLE));
 const auto published=files[state];
 assert(published.find(first)!=string::npos&&published.find("ready")!=string::npos);
 fail_move=true;
 assert(!RecoveryLiveSessionPublish(a,live,REC_CONTROL_MANUAL_WAIT));
 assert(files[state]==published&&files.size()==1);
 fail_move=false;
 assert(RecoveryLiveSessionPublish(a,live,REC_CONTROL_MANUAL_WAIT));
 assert(files[state].find("manual_wait")!=string::npos);
 connected=false;
 assert(!RecoveryLiveSessionCurrent(a,live)&&!RecoveryLiveSessionPublish(a,live,REC_CONTROL_IDLE));
 RecoveryLiveSessionStop(a);assert(!writer_open&&a.mail.boot_id.empty()&&!files.count(state));
 connected=true;
 assert(!RecoveryLiveSessionStart(b,live,scope,"seed1")&&!writer_open); // no old mailbox reuse
 assert(RecoveryLiveSessionStart(b,live,scope,"seed2")&&b.mail.boot_id!=first);
 files[state]=published; // never withdraw a different boot
 login++;
 assert(!RecoveryLiveSessionPublish(b,live,REC_CONTROL_IDLE));
 RecoveryLiveSessionStop(b);assert(!writer_open&&files[state]==published);
 for(const auto& f:files)assert(f.first.rfind("RECOVERY_PANEL\\",0)==0);
}
"""

class LiveSessionCompat(unittest.TestCase):
    def test_fresh_owner_and_state(self):
        with tempfile.TemporaryDirectory() as folder:
            d=Path(folder)
            shutil.copy2(ROOT/"execution/RecoverySessionLifecycle.mqh", d/"RecoverySessionLifecycle.mqh")
            s=(HERE/"RecoveryLiveSession.mqh").read_text()
            s="\n".join(x for x in s.splitlines() if not x.startswith("#include"))
            s=s.replace("(string)TimeLocal()", "std::to_string(TimeLocal())")
            s=s.replace("uchar bytes[];", "std::vector<uchar> bytes;")
            (d/"RecoveryLiveSession.mqh").write_text(s)
            cpp=d/"t.cpp";cpp.write_text(MOCK+TEST)
            out=d/"t"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(cpp),"-o",str(out)],check=True)
            subprocess.run([str(out)],check=True)

if __name__=="__main__":unittest.main()
