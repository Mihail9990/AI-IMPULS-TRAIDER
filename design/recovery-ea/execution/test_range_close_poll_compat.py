"""Simulated verifier transport around the actual close polling controller."""
from pathlib import Path
import re,subprocess,tempfile,unittest
HERE=Path(__file__).resolve().parent
CPP=r"""
#include <cassert>
#include <climits>
#include <string>
#include <vector>
using string=std::string;
using ulong=unsigned long;
struct RecoverySessionLifecycle{string epoch;};
struct RecoverySessionContext{long login,magic;string server,symbol;};
struct RecoveryEvidenceOwner{string executor_epoch,verifier_epoch;long verifier_chart;
                              RecoverySessionContext context;ulong serial;};
struct RecoveryInventoryScope{string nonce,executor_epoch,verifier_epoch,server,symbol;
                              long login,magic,generation,verifier_chart;};
struct RCRange{ulong generation,position_id,active_order;int range_id;};
struct RRRangeState{ulong scope,position_ticket;RCRange cycle;};
struct RRSettings{long initial_sl_ticks;};
struct TestHistoryScope{RecoveryInventoryScope binding;ulong position_identifier,anchor_order;};
struct RCCloseWireBinding{
 struct Key{ulong scope,observation,generation,position_id,anchor_order;int range_id;};
 Key key; TestHistoryScope history;RecoveryInventoryScope inventory;double tick,step;long history_after;
};
struct RCProtectionFact{};
struct RecoveryHistorySnapshot {bool usable,observed;long sequence;};
struct RecoveryInventorySnapshot{long sequence;};
struct RCCloseCheckpoint{RCCloseWireBinding::Key key;};
ulong tick_clock=500;
bool immutable_bad=false;
ulong GetTickCount64(){return tick_clock;}
bool FileIsExist(const string&){return immutable_bad;}
enum RCEvidenceResult{RCE_UNRESOLVED=0,RCE_FULLY_CLASSIFIED=2,RCE_CLOSED_UNCLASSIFIED=3};
struct RCClosePlan{};
int money_proofs=0;
bool strategy_plan=true,mixed_exit=false;
bool RecoveryMoneyEnqueue(const RCCloseWireBinding&,const RecoveryHistorySnapshot&,const RCCloseCheckpoint&){++money_proofs;return true;}
bool RCCPDerive(const RCCloseCheckpoint&,RCRange&,RCClosePlan&){return strategy_plan;}
void Print(const string&){}
long history_sequence=7,inventory_sequence=8;
bool current=true,position_present=true,history_ready=false,inventory_ready=false,typed_proof=false;
int history_requests=0,inventory_requests=0;
bool PositionSelectByTicket(ulong t){assert(t==21);return position_present;}
template<class T>void ZeroMemory(T& t){t=T{};}
bool RecoverySessionCurrent(const RecoverySessionLifecycle&,const RecoverySessionContext&){return current;}
bool RecoverySessionScopeEqual(const RecoverySessionContext& a,const RecoverySessionContext& b){return a.login==b.login;}
bool RecoveryInvScopeOK(const RecoveryInventoryScope&){return true;}
bool RecoveryInvHex(const string& s){return !s.empty();}
bool RRValid(const RRRangeState&,const RRSettings&){return true;}
bool RCCloseWireScope(const RCCloseWireBinding&){return true;}
bool RecoveryAlternatingPumpSession(const RecoveryInventoryScope&){return current;}
bool RCCPSameRange(const RCRange& a,const RCRange& b){
 return a.generation==b.generation&&a.position_id==b.position_id&&a.active_order==b.active_order&&a.range_id==b.range_id;
}
string RecoveryOwnerNonce(const string& name,const string& key){return name+key;}
bool RecoveryHistoryRequest(const TestHistoryScope& s,const string&){
 assert(s.position_identifier==22&&s.anchor_order==23);++history_requests;return true;
}
bool RecoveryHistoryRead(const TestHistoryScope&,RecoveryHistorySnapshot& s){
 if(!history_ready)return false;
 s={true,true,history_sequence};return true;
}
bool RecoveryRequestInventory(const string&,const string&,const string&,long,long,const string&,const string&,long){
 ++inventory_requests;return true;
}
bool RecoveryInvRead(const RecoveryInventoryScope&,RecoveryInventorySnapshot& v){
 if(!inventory_ready)return false;
 v.sequence=inventory_sequence;return true;
}
RCEvidenceResult RCCPObserveFromReports(const RCRange&,const RCCloseWireBinding& b,const RecoveryHistorySnapshot&,
 const RecoveryInventorySnapshot&,const std::vector<RCProtectionFact>&,long distance,RCCloseCheckpoint& out,bool include_unclassified){
 assert(distance==80&&include_unclassified);
 if(!typed_proof)return RCE_UNRESOLVED;
 out.key=b.key;return mixed_exit?RCE_CLOSED_UNCLASSIFIED:RCE_FULLY_CLASSIFIED;
}
#include "RecoveryRangeClosePoll.mqh"
int main(){
 RecoverySessionLifecycle life{"epoch"};
 RecoverySessionContext live{5,6,"srv","XAU"};
 RecoveryEvidenceOwner owner{"epoch","verifier",10,live,0};
 RecoveryInventoryScope v{"n","epoch","verifier","srv","XAU",5,6,3,10};
 RRRangeState range{1,21,{3,22,23,1}};
 const RRSettings cfg{80};const std::vector<RCProtectionFact> facts;
 RecoveryRangeClosePoll task{};RCCloseCheckpoint out{};
 assert(!RecoveryBeginRangeClosePoll(task,owner,life,live,v,range,cfg,.01,.01));
 assert(history_requests==0);
 position_present=false;
 assert(RecoveryBeginRangeClosePoll(task,owner,life,live,v,range,cfg,.01,.01));
 assert(!RecoveryBeginRangeClosePoll(task,owner,life,live,v,range,cfg,.01,.01));
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_PROGRESS);
 assert(history_requests==1&&out.key.scope==0);
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_WAIT);
 history_ready=true;
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_PROGRESS);
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_PROGRESS);
 assert(inventory_requests==1&&out.key.scope==0);
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_WAIT);
 inventory_ready=true;typed_proof=true;
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_PROOF);
 assert(money_proofs==1);
 // A confirmed TP (classified, no strategy plan) and two different exit
 // reasons are valid monetary observations. Neither can advance trading.
 auto replay=task;
 for(int scenario=0;scenario<3;scenario++){
  replay.state=RCP_INVENTORY_READ;strategy_plan=false;mixed_exit=scenario==1;typed_proof=scenario!=2;
  RCCloseCheckpoint trading{};
  assert(RecoveryRangeClosePollStep(replay,life,live,range,cfg,facts,trading)==RCP_NOT_CONFIRMED);
  assert(trading.key.scope==0&&money_proofs==(scenario==0?2:3));
 }
 strategy_plan=true;mixed_exit=false;typed_proof=true;
 assert(out.key.position_id==22&&out.key.scope==1&&out.key.observation==owner.serial);
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_NOT_CONFIRMED);
 assert(history_requests==1&&inventory_requests==1&&out.key.scope==0);
 task={};assert(RecoveryBeginRangeClosePoll(task,owner,life,live,v,range,cfg,.01,.01));
 current=false;
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_NOT_CONFIRMED);
 assert(history_requests==1);
 current=true;task={};history_ready=false;
 assert(RecoveryBeginRangeClosePoll(task,owner,life,live,v,range,cfg,.01,.01));
 const auto original=task.binding.history.binding.nonce;
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_PROGRESS);
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_WAIT);
 immutable_bad=true;
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_NOT_CONFIRMED);
 RecoveryRearmCloseRead(task);
 assert(task.state==RCP_UNRESOLVED);
 tick_clock+=999;RecoveryRearmCloseRead(task);assert(task.state==RCP_UNRESOLVED);
 ++tick_clock;RecoveryRearmCloseRead(task);assert(task.state==RCP_IDLE);
 assert(RecoveryBeginRangeClosePoll(task,owner,life,live,v,range,cfg,.01,.01));
 assert(task.binding.history.binding.nonce!=original);
 immutable_bad=false;history_ready=true;
 for(int i=0;i<3;i++)assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_PROGRESS);
 assert(RecoveryRangeClosePollStep(task,life,live,range,cfg,facts,out)==RCP_PROOF);
}
"""
class RangeClosePollCompat(unittest.TestCase):
    def test_two_anchored_reads_required(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)
            src=(HERE/"RecoveryRangeClosePoll.mqh").read_text()
            src="\n".join(x for x in src.splitlines() if not x.startswith("#include"))
            src=(HERE/"RecoveryProofRetry.mqh").read_text()+"\n"+src
            src=src.replace("const RCProtectionFact &facts[]","const std::vector<RCProtectionFact> &facts")
            src=re.sub(r'\(string\)(b\.key\.\w+|serial)',r'std::to_string(\1)',src)
            (p/"RecoveryRangeClosePoll.mqh").write_text(src)
            (p/"main.cpp").write_text(CPP)
            subprocess.run(["c++","-std=c++17","-Wall","-Wextra","-Werror","-pedantic","-fsanitize=undefined",
                            str(p/"main.cpp"),"-o",str(p/"check")],check=True)
            subprocess.run([str(p/"check")],check=True)
if __name__=="__main__":unittest.main()
