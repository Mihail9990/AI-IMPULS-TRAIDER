"""Run the actual diagnostic EA handlers in a fake terminal (never MetaTrader)."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
ROOT=Path(os.environ.get("RECOVERY_SOURCE_ROOT",HERE.parent))

SHIM=r"""
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <map>
#include <set>
#include <string>
#include <vector>
using string=std::string;
using uchar=unsigned char;
using uint=unsigned;
using ulong=unsigned long;
const int ACCOUNT_LOGIN=1,ACCOUNT_SERVER=2,FILE_BIN=4,FILE_WRITE=8,FILE_REWRITE=16;
const int INIT_FAILED=1,INIT_SUCCEEDED=0,INVALID_HANDLE=-1,CP_UTF8=0;
string _Symbol="XAUUSD.s",file;
long login=5;
bool timer=false,permit_timer=true,permit_move=true,pending=false;
int poll_count=0;
std::set<string> folders;
std::map<string,string> files;
long ChartID(){return 17;}
long AccountInfoInteger(int){return login;}
string AccountInfoString(int){return "server";}
long TimeLocal(){return 1001;}
ulong GetTickCount64(){return 123;}
ulong GetMicrosecondCount(){return 456;}
bool EventSetTimer(int){return timer=permit_timer;}
void EventKillTimer(){timer=false;}
template<class T> void ZeroMemory(T& t){t=T{};}
template<class... T> void PrintFormat(T&&...){}
int StringLen(const string& s){return s.size();}
string StringSubstr(const string& s,int at,int n){return s.substr(at,n);}
int StringToCharArray(const string& s,std::vector<uchar>& b,int,int,int){
 b.assign(s.begin(),s.end());return b.size();
}
bool FolderCreate(const string& s){return folders.insert(s).second;}
int FileOpen(const string& f,int mode){assert(mode==(FILE_BIN|FILE_WRITE));file=f;files[f]="";return 2;}
uint FileWriteArray(int,std::vector<uchar>& b,int,int){files[file]=string(b.begin(),b.end());return b.size();}
void FileFlush(int){}
void FileClose(int){}
bool FileDelete(const string& f){return files.erase(f);}
bool FileMove(const string& src,int,const string& dst,int flags){
 assert(flags==FILE_REWRITE);if(!permit_move)return false;
 files[dst]=files.at(src);files.erase(src);return true;
}
string RecoverySessionSha256(const string& s){
 const auto h=std::hash<string>{}(s);
 char buf[65];
 std::snprintf(buf,sizeof(buf),"%016lx%016lx%016lx%016lx",h,h,h,h);
 return buf;
}
struct RecoveryPanelMailbox { int cursor; };
struct RecoveryPanelCommandScope{string scope_id,boot_id,context_id,generation_id;};
struct RecoveryPanelApplySession{int i;};
struct RecoveryPanelApplyResult{int code;string command_id;};
struct RRSettings{ulong scope,generation;long initial_sl_ticks,support_first_ticks,support_second_ticks,
 support_offset_ticks;long lot_steps[24];};
enum {REC_CONTROL_IDLE,REC_CONTROL_MANUAL_WAIT};
struct RecoveryPanelControl{int mode;};
bool RecoveryPanelCommandUUID(const string& s){return s.size()==36&&s[8]=='-';}
string RecoveryPanelBootEpoch(string s){for(int i=0;i<4;i++)s.erase(s.find('-'),1);return s;}
void RecoveryClearPanelApply(RecoveryPanelApplySession& s){s.i=0;}
void RecoveryPanelMailboxClose(RecoveryPanelMailbox& m){m.cursor=0;}
string RecoveryPanelMailboxBootPath(const string& boot,bool diag){assert(diag);return "RECOVERY_PANEL_PROBE\\sessions\\"+boot;}
"""

HARNESS=r"""
bool RecoveryPanelMailboxPoll(RecoveryPanelMailbox&,const RecoveryPanelCommandScope& mail,
 const RecoverySessionLifecycle& life,const RecoverySessionContext& live,const RRSettings& cfg,double tick,
 RecoveryPanelApplySession& ram,RecoveryPanelControl& c,RecoveryPanelApplyResult& r,bool diagnostic){
 assert(RecoverySessionCurrent(life,live)&&diagnostic&&cfg.initial_sl_ticks==80&&tick==.01);
 assert(folders.count("RECOVERY_PANEL_PROBE\\sessions\\"+mail.boot_id+"\\commands")==1);
 ++poll_count;
 if(!pending)return false;
 pending=false;++ram.i;c.mode=REC_CONTROL_MANUAL_WAIT;r.code=1;r.command_id="example";return true;
}
#include "RecoveryPanelQueueProbe.mqh"
int main(){
 const auto state="RECOVERY_PANEL_PROBE\\state.json";
 assert(OnInit()==INIT_FAILED&&!timer&&files.empty());
 ScopeId="11111111-1111-4111-8111-111111111111";
 assert(OnInit()==INIT_SUCCEEDED&&timer&&RecoveryPanelCommandUUID(QueueProbeScope.boot_id));
 const auto initial=QueueProbeScope.boot_id;
 assert(files.at(state).find(initial)!=string::npos&&files.at(state).find("ready")!=string::npos);
 pending=true;permit_move=false;
 OnTimer();assert(poll_count==1&&QueueProbeSeen.i==1&&QueueProbeStateDirty);
 assert(files.at(state).find("manual_wait")==string::npos);
 permit_move=true;OnTimer();
 assert(!QueueProbeStateDirty&&files.at(state).find("manual_wait")!=string::npos);
 OnDeinit(0);assert(!timer&&QueueProbeSeen.i==0);
 assert(OnInit()==INIT_SUCCEEDED&&timer&&QueueProbeScope.boot_id!=initial);
 assert(files.at(state).find(initial)==string::npos&&files.at(state).find("ready")!=string::npos);
 int last=poll_count;login++;
 OnTimer();assert(!timer&&poll_count==last);
 permit_timer=false;assert(OnInit()==INIT_FAILED&&!timer);
 for(const auto& f:files)assert(f.first.rfind("RECOVERY_PANEL_PROBE\\",0)==0);
}
"""

class PanelQueueProbeCompat(unittest.TestCase):
    def test_diagnostic_queue_uses_new_session_only(self):
        with tempfile.TemporaryDirectory() as folder:
            d=Path(folder)
            shutil.copy2(ROOT/"execution/RecoverySessionLifecycle.mqh", d/"RecoverySessionLifecycle.mqh")
            body=(HERE/"RecoveryPanelQueueProbe.mq5").read_text()
            body="\n".join(line.removeprefix("input ") for line in body.splitlines()
                           if not line.startswith(("#property","#include")))
            body=body.replace("uchar bytes[];", "std::vector<uchar> bytes;")
            body=re.sub(r"\(string\)(QueueProbeSerial|\w+\(\))",r"std::to_string(\1)",body)
            (d/"RecoveryPanelQueueProbe.mqh").write_text(body)
            src=d/"test.cpp"
            src.write_text(SHIM+'\n#include "RecoverySessionLifecycle.mqh"\n'+HARNESS)
            exe=d/"test"
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic",
                            "-fsanitize=undefined",str(src),"-o",str(exe)],check=True)
            subprocess.run([str(exe)],check=True)

if __name__=="__main__":
    unittest.main()
