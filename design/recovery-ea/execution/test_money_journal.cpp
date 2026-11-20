#include <map>
#include <sstream>
#include <iomanip>
using uchar=unsigned char;
using uint=unsigned int;
enum {FILE_READ=1,FILE_BIN=2,FILE_SHARE_READ=4,FILE_WRITE=8,INVALID_HANDLE=-1,
      WHOLE_ARRAY=0,CP_UTF8=65001};
struct TestKey {ulong position_id,anchor_order;};
struct TestContext {long login;string server,symbol;};
struct RCCloseWireBinding {TestKey key;TestContext inventory;long history_after;double step;};
struct RecoveryHistorySnapshot {bool usable,observed;string reason;long sequence;
  int order_count,deal_count;std::vector<string> rows;};
struct RCCloseCheckpoint {TestKey key;
  struct {ulong position_id,active_order;int side;long filled_steps;} before;
  struct {bool independently_verified,history_complete;ulong position_id,order;
          long remaining_steps,exited_steps;} proof;};
bool RCCloseKeysEqual(const TestKey& a,const TestKey& b){return a.position_id==b.position_id&&a.anchor_order==b.anchor_order;}
bool RCCloseWireScope(const RCCloseWireBinding& b){return b.inventory.login>0&&b.step>0;}
bool RCValidSide(int side){return side==1||side==-1;}
bool RCBoundedPositive(long n){return n>0&&n<=1000000000000;}
bool RecoveryInvContext(const string& s){return !s.empty();}
bool RecoveryAdoptionUnsigned(const string& s,ulong& n){try {n=std::stoull(s);return std::to_string(n)==s;} catch(...){return false;}}
bool RecoveryAdoptionUnits(const string& s,double step,bool,long& n){n=std::lround(std::stod(s)/step);return n>0;}
template<class T>int ArrayResize(std::vector<T>& v,int n,int){return ArrayResize(v,n);}
int StringSplit(const string& s,int sep,std::vector<string>& v) {
  std::istringstream data(s);v.clear();
  for(string item;std::getline(data,item,(char)sep);)v.push_back(item);
  return (int)v.size();
}
int StringLen(const string& s){return (int)s.size();}
string DoubleToString(double x,int n){std::ostringstream o;o<<std::setprecision(std::abs(n))<<x;return o.str();}
string RecoverySessionSha256(const string& s) {
  assert(s=="RECOVERY_MONEY_V1\t99\tDemo");return string(64,'a');
}
std::map<string,std::vector<uchar>> disk;
std::map<int,string> handles;
int next_handle=1,final_commits=0;
bool fail_result_move=false,fail_marker_delete=false,short_write=false;
long ChartID(){return 140;}
int StringToCharArray(const string& s,std::vector<uchar>& v,int,int,int){v.assign(s.begin(),s.end());v.push_back(0);return v.size();}
bool FileIsExist(const string& path){return disk.count(path)>0;}
void FolderCreate(const string&){}
int FileOpen(const string& p,int flags) {
 if(flags&FILE_WRITE)disk[p].clear();
 if(!FileIsExist(p))return INVALID_HANDLE;
 handles[++next_handle]=p;return next_handle;
}
ulong FileSize(int h){return disk.at(handles.at(h)).size();}
uint FileReadArray(int h,std::vector<uchar>& a,int,int n){a=disk.at(handles.at(h));return std::min<uint>(n,a.size());}
uint FileWriteArray(int h,const std::vector<uchar>& a,int,int n){
 if(short_write)--n;
 disk.at(handles.at(h)).assign(a.begin(),a.begin()+n);return n;
}
void FileClose(int h){assert(handles.erase(h)==1);}
void FileFlush(int h){assert(handles.count(h)==1);}
bool FileMove(const string& a,int,const string& b,int flags){
 assert(flags==0);
 const bool result=b.find(".pending.")==string::npos;
 if(!FileIsExist(a)||FileIsExist(b)||(result&&fail_result_move))return false;
 disk[b]=disk[a];disk.erase(a);if(result)++final_commits;return true;
}
bool FileDelete(const string& p){
 if(fail_marker_delete&&p.find(".pending.tsv")!=string::npos)return false;
 return disk.erase(p)==1;
}
string read_file(const string& path){const auto& data=disk.at(path);return string(data.begin(),data.end());}
#include "MoneyJournalUnderTest"

int main(){
 const RCCloseWireBinding b{{Pos,66},{99,"Demo","XAUUSD.s"},8,.01};
 const RCCloseCheckpoint closed{{Pos,66},{Pos,66,1,5},{true,true,Pos,66,0,5}};
 auto deal=[](ulong ticket,const string& vol,const string& price) {
    return "D\t"+std::to_string(ticket)+"\t77\t0\tSELL\t"+std::to_string(DEAL_ENTRY_OUT)+
           "\t4\t111\t"+vol+"\t"+price+"\t3999.30\t0.0";
 };
 RecoveryHistorySnapshot hist{true,true,"ok",9,1,3,{"H",
    "D\t1\t66\t0\tBUY\t"+std::to_string(DEAL_ENTRY_IN)+"\t2\t110\t0.05\t4000\t0\t0",
    deal(Second,"0.03","3999.22"),deal(First,"0.02","3998.44")}};
 auto missing=hist;missing.rows.pop_back();missing.deal_count=2;
 assert(!RecoveryMoneyEnqueue(b,missing,closed));
 auto duplicate=hist;duplicate.rows[3]=duplicate.rows[2];assert(!RecoveryMoneyEnqueue(b,duplicate,closed));
 auto wrong=closed;wrong.key.position_id++;assert(!RecoveryMoneyEnqueue(b,hist,wrong));
 auto incomplete=closed;incomplete.proof.independently_verified=false;assert(!RecoveryMoneyEnqueue(b,hist,incomplete));
 auto stale=hist;stale.sequence=8;assert(!RecoveryMoneyEnqueue(b,stale,closed));
 short_write=true;
 assert(RecoveryMoneyEnqueue(b,hist,closed)&&disk.empty());
 std::swap(hist.rows[2],hist.rows[3]);assert(RecoveryMoneyEnqueue(b,hist,closed)&&RecoveryMoneyJobs.size()==1);
 auto job=RecoveryMoneyJobs[0];string marker=job.base+".pending.tsv",result=job.base+".tsv";
 auto tick=[] {RecoveryMoneyJournalTimer(99,"Demo","XAUUSD.s");};
 tick();short_write=false;
 assert(disk.empty()&&!RecoveryMoneyJobs[0].pending_written);
 RecoveryMoneyFlushPending();assert(FileIsExist(marker)&&!FileIsExist(result));
 mock.reject_fee=true;tick();
 assert(disk.size()==1&&FileIsExist(marker)&&!FileIsExist(result)&&read_file(marker).find("pending")!=string::npos);
 assert(read_file(marker).find("net")==string::npos&&mock.count>0);
 mock.reject_fee=false;fail_result_move=true;tick();
 assert(!FileIsExist(result)&&final_commits==0&&RecoveryMoneyJobs.size()==1);
 fail_result_move=false;fail_marker_delete=true;tick();
 assert(final_commits==1&&FileIsExist(result)&&FileIsExist(marker)&&RecoveryMoneyJobs.size()==1);
 const auto report=read_file(result);
 assert(report.find("3998.44\t3999.30")!=string::npos&&report.find("3999.22\t3999.30")!=string::npos);
 auto p=report.find("observed\tUSD\t");assert(p!=string::npos);
 std::vector<string> columns;StringSplit(report.substr(p),9,columns);
 assert(columns.size()==8&&near(std::stod(columns[2]),-5.46)&&near(std::stod(columns[6]),-5.8)&&near(std::stod(columns[7]),5.8));
 fail_marker_delete=false;tick();
 assert(RecoveryMoneyJobs.empty()&&!FileIsExist(marker)&&read_file(result)==report);
 // Independent proof can be redelivered after manual reset; final file is immutable.
 assert(RecoveryMoneyEnqueue(b,hist,closed));tick();assert(RecoveryMoneyJobs.empty()&&final_commits==1);
 disk[result].push_back('!');assert(RecoveryMoneyEnqueue(b,hist,closed));tick();
 assert(RecoveryMoneyJobs.size()==1&&FileIsExist(marker)&&read_file(result)==report+"!"&&final_commits==1);
}
