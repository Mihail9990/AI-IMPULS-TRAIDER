#include <cassert>
#include <cstdint>
#include <map>
#include <string>
#include <vector>
using string=std::string;
using ulong=std::uint64_t;
using uint=unsigned int;
using uchar=unsigned char;

enum {INVALID_HANDLE=-1,FILE_READ=1,FILE_BIN=2,FILE_SHARE_READ=4,FILE_WRITE=8,CP_UTF8=65001};
int StringLen(const string &s){return int(s.size());}
string StringSubstr(const string &s,int start,int n=-1){return s.substr(start,n);}
int ArraySize(const std::vector<uchar> &b){return int(b.size());}
template<class T> void ZeroMemory(T &value){value=T{};}
int StringToCharArray(const string &s,std::vector<uchar> &out,int a,int n,int cp){
 assert(a==0 && n==StringLen(s) && cp==CP_UTF8);out.assign(s.begin(),s.end());return n;
}
string CharArrayToString(const std::vector<uchar> &out,int a,int n,int cp){
 assert(a==0 && n==ArraySize(out) && cp==CP_UTF8);return string(out.begin(),out.end());
}
bool RecoveryPanelCommandHex(const string &s,int length){
 return StringLen(s)==length && s.find_first_not_of("0123456789abcdef")==string::npos;
}
bool RecoveryPanelCommandUUID(const string &s){
 if(s.size()!=36)return false;
 for(int i=0;i<36;i++)
   if((i==8 || i==13 || i==18 || i==23) ? s[i]!='-' : !RecoveryPanelCommandHex(s.substr(i,1),1))return false;
 return true;
}
const string boot="11111111-1111-4111-8111-111111111111", old="22222222-2222-4222-8222-222222222222";
const string id1="33333333-3333-4333-8333-333333333333", id2="44444444-4444-4444-8444-444444444444";
const string id3="55555555-5555-4555-8555-555555555555", id4="66666666-6666-4666-8666-666666666666";
const string scope="77777777-7777-4777-8777-777777777777", gen="88888888-8888-4888-8888-888888888888";
const string ctx="99999999-9999-4999-8999-999999999999";
string RecoveryPanelBootEpoch(const string &s){
 string d;for(char c:s)if(c!='-')d+=c;return d;
}
struct RecoveryPanelCommandScope {string scope_id,boot_id,context_id,generation_id;};
struct RecoverySessionLifecycle {string epoch;bool active;};
struct RecoverySessionContext {bool valid;};
bool RecoverySessionCurrent(const RecoverySessionLifecycle &l,const RecoverySessionContext &c){return l.active&&c.valid;}
enum RecoveryPanelApplyCode {RECP_INVALID=0,RECP_REJECTED,RECP_STAGED,RECP_DUPLICATE,RECP_CONFLICT};
enum RecoveryPanelOperation {RPC_NONE=0,RPC_MANUAL_WAIT,RPC_MANUAL_CANCEL,RPC_CONFIGURE_RANGES};
struct RecoveryPanelApplyResult {RecoveryPanelApplyCode code,original;RecoveryPanelOperation operation;string command_id,digest;};
struct RecoveryPanelApplySession {std::map<string,RecoveryPanelApplyCode> seen;};
struct RecoveryPanelControl {int state_changes;};
struct RRSettings {};
RecoveryPanelApplyCode RecoveryDecodeAndStagePanel(const string &wire,const string &id,const RecoveryPanelCommandScope &,
 const RecoverySessionLifecycle &,const RecoverySessionContext &,const RRSettings &,double,RecoveryPanelApplySession &ram,
 RecoveryPanelControl &ctrl,RecoveryPanelApplyResult &out,bool may_cancel_wait){
 out=RecoveryPanelApplyResult{};out.command_id=id;out.digest=string(64,'a');
 if(wire=="ranges")out.operation=RPC_CONFIGURE_RANGES;
 else if(wire=="wait")out.operation=RPC_MANUAL_WAIT;
 else if(wire=="reject")out.operation=RPC_MANUAL_CANCEL;
 else return RECP_INVALID;
 if(ram.seen.count(id)){out.original=ram.seen.at(id);return out.code=RECP_DUPLICATE;}
 out.code=(wire=="reject"||(out.operation==RPC_MANUAL_CANCEL&&!may_cancel_wait))?RECP_REJECTED:RECP_STAGED;
 ram.seen[id]=out.code;
 if(out.code==RECP_STAGED)ctrl.state_changes++;
 return out.code;
}

std::map<string,string> files;
std::map<string,std::vector<string>> dirs;
std::map<int,string> open_files;
std::vector<string> scanlog,readlog,names;
size_t offset=0;
int num=10,closes=0;
bool fail_move=false,short_read=false;
long FileFindFirst(const string &pattern,string &name){
 scanlog.push_back(pattern);names=dirs[pattern];offset=0;
 if(names.empty())return INVALID_HANDLE;
 name=names[0];return 3;
}
bool FileFindNext(long h,string &name){
 assert(h==3);if(++offset==names.size())return false;name=names[offset];return true;
}
void FileFindClose(long h){assert(h==3);closes++;}
bool FileIsExist(const string &path){return files.count(path)!=0;}
int FileOpen(const string &path,int flags){
 if(flags&FILE_READ){readlog.push_back(path);if(!FileIsExist(path))return INVALID_HANDLE;}
 else {assert(flags==(FILE_WRITE|FILE_BIN));files[path]="";}
 open_files[++num]=path;return num;
}
ulong FileSize(int h){return files.at(open_files.at(h)).size();}
uint FileReadArray(int h,std::vector<uchar> &data,int start,int count){
 const string s=files.at(open_files.at(h));assert(start==0 && count==StringLen(s));
 data.assign(s.begin(),s.end());return short_read?data.size()-1:data.size();
}
uint FileWriteArray(int h,const std::vector<uchar> &data,int start,int count){
 assert(start==0 && count==ArraySize(data));
 files[open_files.at(h)]=string(data.begin(),data.end());return count;
}
void FileFlush(int h){assert(open_files.count(h));}
void FileClose(int h){assert(open_files.erase(h)==1);}
bool FileMove(const string &src,int srcflags,const string &dst,int dstflags){
 assert(srcflags==0 && dstflags==0);
 if(fail_move || files.count(dst))return false;
 files[dst]=files.at(src);files.erase(src);return true;
}
bool FileDelete(const string &path){return files.erase(path)!=0;}

#include "MailboxUnderTest"

int main(){
 const string root=RecoveryPanelMailboxBootPath(boot,true);
 assert(root=="RECOVERY_PANEL_PROBE\\sessions\\"+boot);
 assert(RecoveryPanelMailboxBootPath(boot)=="RECOVERY_PANEL\\sessions\\"+boot);
 assert(RecoveryPanelMailboxBootPath("..\\evil",true).empty());
 const string commands=root+"\\commands\\",receipts=root+"\\receipts\\";
 RecoveryPanelCommandScope mail{scope,boot,ctx,gen};
 RecoverySessionLifecycle life{RecoveryPanelBootEpoch(boot),true};
 RecoverySessionContext live{true};RRSettings cfg;
 RecoveryPanelMailbox box{};
 RecoveryPanelApplySession ram{};
 RecoveryPanelControl state{};
 RecoveryPanelApplyResult result{};
 auto poll=[&](){return RecoveryPanelMailboxPoll(box,mail,life,live,cfg,.01,ram,state,result,true);};
 dirs[commands+"*.json"]=std::vector<string>(17,"..\\evil.json");
 dirs[commands+"*.json"].push_back(id1+".json");
 dirs[commands+"*.json"].push_back(id2+".json");
 dirs[commands+"*.json"].push_back(id3+".json");
 files[commands+id1+".json"]="ranges";
 files[commands+id2+".json"]="wait";
 files[commands+id3+".json"]="reject";
 const string old_commands=RecoveryPanelMailboxBootPath(old,true)+"\\commands\\";
 dirs[old_commands+"*.json"]={id4+".json"};
 files[old_commands+id4+".json"]="wait";
 assert(!poll() && state.state_changes==0 && readlog.empty() && scanlog.size()==1 && closes==0);
 assert(poll() && state.state_changes==1 && result.code==RECP_STAGED);
 assert(files.at(receipts+id1+".json").find("\"status\":\"accepted\"")!=string::npos);
 fail_move=true;
 assert(poll() && state.state_changes==2 && result.operation==RPC_MANUAL_WAIT);
 assert(!FileIsExist(receipts+id2+".json") && !FileIsExist(receipts+id2+".writing"));
 fail_move=false;
 assert(poll() && result.code==RECP_REJECTED && state.state_changes==2 && closes==1);
 assert(files.at(receipts+id3+".json").find("\"status\":\"rejected\"")!=string::npos);
 assert(!poll() && !poll() && state.state_changes==2);
 const string manual=files.at(receipts+id2+".json");
 assert(manual.find("\"status\":\"applied\"")!=string::npos &&
        manual.find("\"context_id\":\""+ctx+"\"")!=string::npos &&
        manual.find("\"generation_id\":\""+gen+"\"")!=string::npos &&
        manual.find("\"scope_id\":\""+scope+"\"")!=string::npos &&
        manual.find("\"content_sha256\":\""+string(64,'a')+"\"")!=string::npos);
 for(const string &name:scanlog)assert(name==commands+"*.json");
 assert(!RecoveryPanelMailboxReceipt(mail,life,live,result,true));
 assert(!poll()); // starts fresh pass; keep cursor open
 life.active=false;
 const size_t before=scanlog.size();
 assert(!poll() && box.handle==INVALID_HANDLE && !box.initialized && scanlog.size()==before);
 life.active=true;
 string wire;
 files[commands+id4+".json"]=string("wait\0",5);
 assert(!RecoveryPanelMailboxRead(commands+id4+".json",wire));
 files[commands+id4+".json"]=string(1025,'a');
 assert(!RecoveryPanelMailboxRead(commands+id4+".json",wire));
 files[commands+id4+".json"]="wait";short_read=true;
 assert(!RecoveryPanelMailboxRead(commands+id4+".json",wire));short_read=false;
 assert(RecoveryPanelMailboxRead(commands+id4+".json",wire) && wire=="wait");
 RecoveryPanelMailboxClose(box);
 assert(open_files.empty());
}
