#pragma once
#include <algorithm>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <map>
#include <sstream>
#include <string>
#include <vector>
#include <iostream>
#include <iomanip>
using uchar=unsigned char;using ushort=unsigned short;using uint=unsigned int;using datetime=long;
struct string:std::string {
 using std::string::string;string()=default;string(const std::string&s):std::string(s){}
 template<class T, typename=std::enable_if_t<std::is_arithmetic_v<T> || std::is_enum_v<T>>> explicit string(T v):std::string(std::to_string(v)){}
};
template<class T>using Array=std::vector<T>;
template<class T>int ArraySize(const Array<T>&a){return (int)a.size();}
template<class T>int ArrayResize(Array<T>&a,int n){a.resize(n);return n;}
constexpr int INVALID_HANDLE=-1,WHOLE_ARRAY=-1,CP_UTF8=65001,PERIOD_M1=1,INIT_SUCCEEDED=0,INIT_FAILED=-1,INIT_PARAMETERS_INCORRECT=-2;
constexpr int FILE_READ=1,FILE_WRITE=2,FILE_BIN=4,FILE_TXT=8,FILE_ANSI=16,FILE_COMMON=32,FILE_REWRITE=64,FILE_SHARE_READ=128;
enum {ACCOUNT_LOGIN,ACCOUNT_SERVER,ACCOUNT_CURRENCY,ACCOUNT_MARGIN_MODE,ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,TERMINAL_CONNECTED,
 SYMBOL_POINT,SYMBOL_TRADE_TICK_SIZE,SYMBOL_TRADE_STOPS_LEVEL,SYMBOL_TRADE_FREEZE_LEVEL,SYMBOL_VOLUME_STEP,SYMBOL_VOLUME_MIN,SYMBOL_VOLUME_MAX,SYMBOL_FILLING_MODE,
 POSITION_IDENTIFIER,POSITION_SYMBOL,POSITION_MAGIC,POSITION_VOLUME,POSITION_SL,POSITION_TP,POSITION_TICKET,POSITION_PRICE_OPEN,POSITION_TYPE,POSITION_TIME_MSC,
 ORDER_SYMBOL,ORDER_MAGIC,ORDER_COMMENT,ORDER_VOLUME_CURRENT,ORDER_POSITION_ID,ORDER_STATE,
 ORDER_STATE_CANCELED,ORDER_STATE_EXPIRED,ORDER_STATE_REJECTED,ORDER_STATE_FILLED,
 DEAL_POSITION_ID,DEAL_SYMBOL,DEAL_TYPE,DEAL_ENTRY,DEAL_PROFIT,DEAL_COMMISSION,DEAL_SWAP,DEAL_FEE,DEAL_VOLUME,DEAL_ORDER,DEAL_MAGIC,DEAL_REASON,DEAL_SL,DEAL_PRICE,DEAL_TIME_MSC,
 DEAL_ENTRY_IN,DEAL_ENTRY_OUT,DEAL_ENTRY_INOUT,DEAL_REASON_SL,DEAL_REASON_TP};
enum ENUM_ORDER_TYPE {ORDER_TYPE_BUY=0,ORDER_TYPE_SELL=1,ORDER_TYPE_BUY_STOP=4,ORDER_TYPE_SELL_STOP=5};
constexpr int POSITION_TYPE_BUY=0,POSITION_TYPE_SELL=1,DEAL_TYPE_BUY=0,DEAL_TYPE_SELL=1;
constexpr int SYMBOL_FILLING_FOK=1,SYMBOL_FILLING_IOC=2,ORDER_FILLING_FOK=0,ORDER_FILLING_IOC=1,ORDER_FILLING_RETURN=2,ORDER_TIME_GTC=0;
enum {TRADE_ACTION_PENDING=1,TRADE_ACTION_REMOVE=2,TRADE_ACTION_SLTP=3,TRADE_ACTION_DEAL=4};
enum {TRADE_RETCODE_DONE=10009,TRADE_RETCODE_REJECT=10006,TRADE_RETCODE_INVALID=10013,TRADE_RETCODE_INVALID_VOLUME=10014,TRADE_RETCODE_INVALID_PRICE=10015,TRADE_RETCODE_INVALID_STOPS=10016,TRADE_RETCODE_NO_MONEY=10019,TRADE_RETCODE_TRADE_DISABLED=10017,TRADE_RETCODE_MARKET_CLOSED=10018,TRADE_RETCODE_TIMEOUT=10012};
struct MqlTick{long time=180,time_msc=180000;double bid=100,ask=100.02;};
struct MqlRates{long time=120;double open=100,close=100.10,high=100.2,low=99.8;};
struct MqlTradeRequest{string symbol,comment;ulong magic=0,position=0,order=0;uint deviation=0;int action=0,type=0,type_filling=0,type_time=0;double volume=0,price=0,sl=0,tp=0;};
struct MqlTradeResult{uint retcode=0;ulong order=0,deal=0;};struct MqlTradeCheckResult{uint retcode=TRADE_RETCODE_DONE;};struct MqlTradeTransaction{};
string _Symbol="XAUUSD.s";
int StringLen(string s){return (int)s.size();}int StringFind(string s,string q,int start=0){auto p=s.find(q,start);return p==std::string::npos?-1:(int)p;}
string StringSubstr(string s,int start,int count=-1){return s.substr(start,count<0?std::string::npos:(size_t)count);}
ushort StringGetCharacter(string s,int i){return (uchar)s[i];}
int StringSplit(string s,ushort delimiter,Array<string>&out){out.clear();size_t begin=0;for(size_t i=0;i<=s.size();i++)if(i==s.size()||s[i]==delimiter){out.push_back(s.substr(begin,i-begin));begin=i+1;}return out.size();}
long StringToInteger(string s){try{return std::stol(s);}catch(...){return 0;}}
double StringToDouble(string s){try{return std::stod(s);}catch(...){return 0;}}
string DoubleToString(double v,int precision=8){std::ostringstream s;s<<std::fixed<<std::setprecision(precision)<<v;return s.str();}
int StringToCharArray(string s,Array<uchar>&out,int=0,int=-1,int=65001){out.assign(s.begin(),s.end());out.push_back(0);return out.size();}
string CharArrayToString(Array<uchar>&a,int start=0,int count=-1,int=65001){return std::string(a.begin()+start,count<0?a.end():a.begin()+start+count);}
bool MathIsValidNumber(double v){return std::isfinite(v);}double MathAbs(double v){return std::abs(v);}double MathMax(double a,double b){return std::max(a,b);}double MathRound(double a){return std::round(a);}double MathCeil(double a){return std::ceil(a);}double MathFloor(double a){return std::floor(a);}
std::map<string,string> mock_files;struct Handle{string name;size_t cursor;};std::map<int,Handle> handles;int next_handle=1;bool disk_failure=false;
int FileOpen(string name,int flags,int=0,int=65001){if(disk_failure)return -1;if(!(flags&FILE_WRITE)&&!mock_files.count(name))return -1;if(flags&FILE_WRITE)mock_files[name]="";int h=next_handle++;handles[h]={name,0};return h;}
ulong FileSize(int h){return mock_files[handles[h].name].size();}void FileClose(int h){handles.erase(h);}void FileFlush(int){}
uint FileWriteArray(int h,Array<uchar>&a,int start,int count){mock_files[handles[h].name]=std::string(a.begin()+start,a.begin()+start+count);return count;}
uint FileReadArray(int h,Array<uchar>&a,int start,int count){auto &s=mock_files[handles[h].name];int n=std::min(count,(int)s.size());for(int i=0;i<n;i++)a[start+i]=s[i];handles[h].cursor=n;return n;}
string FileReadString(int h){auto &x=handles[h];auto &s=mock_files[x.name];auto pos=s.find('\n',x.cursor);string out=s.substr(x.cursor,pos==std::string::npos?std::string::npos:pos-x.cursor);x.cursor=pos==std::string::npos?s.size():pos+1;return out;}
bool FileIsEnding(int h){return handles[h].cursor>=mock_files[handles[h].name].size();}
bool FileIsExist(string n,int=0){return mock_files.count(n);}bool FileMove(string a,int,string b,int){if(disk_failure||!mock_files.count(a))return false;mock_files[b]=mock_files[a];mock_files.erase(a);return true;}
bool FileDelete(string a,int=0){return mock_files.erase(a)>0;}bool FolderCreate(string,int=0){return true;}
long FileFindFirst(string,string&,int=0){return -1;}bool FileFindNext(long,string&){return false;}void FileFindClose(long){}
long AccountInfoInteger(int key){return key==ACCOUNT_LOGIN?123:ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;}string AccountInfoString(int key){return key==ACCOUNT_CURRENCY?"USD":"Mock";}bool connected=true;long TerminalInfoInteger(int){return connected;}
MqlTick mock_quote;MqlRates mock_bar;
bool SymbolInfoTick(string,MqlTick &q){q=mock_quote;return connected;}int CopyRates(string,int,int,int,Array<MqlRates>&a){a={mock_bar};return connected?1:0;}
double SymbolInfoDouble(string,int key){switch(key){case SYMBOL_POINT:case SYMBOL_TRADE_TICK_SIZE:case SYMBOL_VOLUME_STEP:case SYMBOL_VOLUME_MIN:return .01;case SYMBOL_VOLUME_MAX:return 100;default:return 0;}}
long SymbolInfoInteger(string,int key){return key==SYMBOL_FILLING_MODE?SYMBOL_FILLING_FOK:0;}
long TimeCurrent(){return mock_quote.time;}long TimeLocal(){return 180;}ulong GetTickCount64(){return mock_quote.time_msc;}bool EventSetMillisecondTimer(int){return true;}void EventKillTimer(){}void Print(string s){std::cerr<<s<<"\n";}
struct Row{std::map<int,long> ints;std::map<int,double> doubles;std::map<int,string> strings;};
std::map<ulong,Row> mock_orders,mock_history_orders,mock_positions,mock_deals;
ulong selected_order=0,selected_position=0;std::vector<ulong> history_deals,history_orders;
int OrdersTotal(){return mock_orders.size();}ulong OrderGetTicket(int i){auto it=mock_orders.begin();std::advance(it,i);selected_order=it->first;return it->first;}
bool OrderSelect(ulong t){selected_order=t;return mock_orders.count(t);}string OrderGetString(int p){return mock_orders[selected_order].strings[p];}long OrderGetInteger(int p){return mock_orders[selected_order].ints[p];}double OrderGetDouble(int p){return mock_orders[selected_order].doubles[p];}
int PositionsTotal(){return mock_positions.size();}ulong PositionGetTicket(int i){auto it=mock_positions.begin();std::advance(it,i);selected_position=it->first;return it->first;}string PositionGetString(int p){return mock_positions[selected_position].strings[p];}long PositionGetInteger(int p){return mock_positions[selected_position].ints[p];}double PositionGetDouble(int p){return mock_positions[selected_position].doubles[p];}
bool history_available=true;
bool HistorySelect(long,long){if(!history_available)return false;history_orders.clear();for(auto &r:mock_history_orders)history_orders.push_back(r.first);return true;}
bool HistoryOrderSelect(ulong t){return history_available && mock_history_orders.count(t);}int HistoryOrdersTotal(){return history_orders.size();}ulong HistoryOrderGetTicket(int i){return history_orders[i];}
string HistoryOrderGetString(ulong t,int p){return mock_history_orders[t].strings[p];}long HistoryOrderGetInteger(ulong t,int p){return mock_history_orders[t].ints[p];}
bool HistorySelectByPosition(ulong pid){if(!history_available)return false;history_deals.clear();for(auto&r:mock_deals)if((ulong)r.second.ints[DEAL_POSITION_ID]==pid)history_deals.push_back(r.first);return true;}
int HistoryDealsTotal(){return history_deals.size();}ulong HistoryDealGetTicket(int i){return history_deals[i];}long HistoryDealGetInteger(ulong t,int p){return mock_deals[t].ints[p];}double HistoryDealGetDouble(ulong t,int p){return mock_deals[t].doubles[p];}string HistoryDealGetString(ulong t,int p){return mock_deals[t].strings[p];}
bool OrderCalcProfit(ENUM_ORDER_TYPE type,string,double volume,double entry,double close,double &profit){profit=(type==ORDER_TYPE_BUY?1:-1)*(close-entry)*volume*100;return true;}
int sends=0;bool lost_response=false,reject_send=false;ulong next_order=1000;string journal_name="AI_IMPULS_NATIVE_V1\\native-state.txt";
bool check_move_sl=false;ulong reject_check_position=0;
bool OrderCheck(MqlTradeRequest &r,MqlTradeCheckResult &c){c.retcode=0;if(r.position>0 && r.position==reject_check_position){c.retcode=TRADE_RETCODE_INVALID_STOPS;return false;}if(check_move_sl && r.position>0){mock_positions[r.position].doubles[POSITION_SL]=r.sl+.2;check_move_sl=false;}return true;}
void exit_position(ulong pid,int reason,double profit,double volume,double stop);
bool OrderSend(MqlTradeRequest &r,MqlTradeResult &out){
 assert(mock_files.count(journal_name));assert(mock_files[journal_name].find("UNKNOWN")!=std::string::npos);sends++;
 if(reject_send){out.retcode=TRADE_RETCODE_INVALID_STOPS;return false;}
 if(r.action==TRADE_ACTION_PENDING){ulong id=next_order++;Row o;o.strings={{ORDER_SYMBOL,r.symbol},{ORDER_COMMENT,r.comment}};o.ints={{ORDER_MAGIC,(long)r.magic}};o.doubles={{ORDER_VOLUME_CURRENT,r.volume},{POSITION_SL,r.sl},{POSITION_TP,r.tp},{POSITION_PRICE_OPEN,r.price}};mock_orders[id]=o;if(!lost_response)out.order=id;}
 else if(r.action==TRADE_ACTION_SLTP){assert(mock_positions.count(r.position));mock_positions[r.position].doubles[POSITION_SL]=r.sl;mock_positions[r.position].doubles[POSITION_TP]=r.tp;}
 else if(r.action==TRADE_ACTION_REMOVE){auto o=mock_orders.at(r.order);o.ints[ORDER_STATE]=ORDER_STATE_CANCELED;mock_history_orders[r.order]=o;mock_orders.erase(r.order);}
 else if(r.action==TRADE_ACTION_DEAL){exit_position(r.position,999,0,r.volume,-1);}
 out.retcode=lost_response?TRADE_RETCODE_TIMEOUT:TRADE_RETCODE_DONE;return !lost_response;
}
void fill_order(ulong id,ulong pid,int side,double volume){auto o=mock_orders.at(id);o.ints[ORDER_STATE]=ORDER_STATE_FILLED;o.ints[ORDER_POSITION_ID]=pid;mock_history_orders[id]=o;mock_orders.erase(id);Row p;p.ints={{POSITION_IDENTIFIER,(long)pid},{POSITION_TICKET,(long)pid},{POSITION_MAGIC,o.ints[ORDER_MAGIC]},{POSITION_TYPE,side==1?POSITION_TYPE_BUY:POSITION_TYPE_SELL}};p.strings[POSITION_SYMBOL]="XAUUSD.s";p.doubles={{POSITION_PRICE_OPEN,o.doubles[POSITION_PRICE_OPEN]},{POSITION_VOLUME,volume},{POSITION_SL,o.doubles[POSITION_SL]},{POSITION_TP,o.doubles[POSITION_TP]}};mock_positions[pid]=p;Row d;d.ints={{DEAL_POSITION_ID,(long)pid},{DEAL_ORDER,(long)id},{DEAL_MAGIC,o.ints[ORDER_MAGIC]},{DEAL_TYPE,side==1?DEAL_TYPE_BUY:DEAL_TYPE_SELL},{DEAL_ENTRY,DEAL_ENTRY_IN}};d.strings[DEAL_SYMBOL]="XAUUSD.s";d.doubles={{DEAL_VOLUME,volume},{DEAL_COMMISSION,-.02},{DEAL_SL,o.doubles[POSITION_SL]}};mock_deals[id*10]=d;}
void exit_position(ulong pid,int reason,double profit,double volume=-1,double stop=-1){
 auto p=mock_positions.at(pid);if(volume<0)volume=p.doubles[POSITION_VOLUME];
 ulong id=900000+mock_deals.size();Row d;d.ints={{DEAL_POSITION_ID,(long)pid},{DEAL_ORDER,(long)id},{DEAL_MAGIC,p.ints[POSITION_MAGIC]},{DEAL_TYPE,DEAL_TYPE_SELL},{DEAL_ENTRY,DEAL_ENTRY_OUT},{DEAL_REASON,reason}};
 d.strings[DEAL_SYMBOL]="XAUUSD.s";d.doubles={{DEAL_VOLUME,volume},{DEAL_PROFIT,profit},{DEAL_COMMISSION,-.01},{DEAL_SL,stop<0?p.doubles[POSITION_SL]:stop}};mock_deals[id]=d;
 if(volume>=p.doubles[POSITION_VOLUME]-1e-8)mock_positions.erase(pid);else mock_positions[pid].doubles[POSITION_VOLUME]-=volume;
}

constexpr int COPY_TICKS_INFO=1;
int mock_last_error=0;bool ticks_available=true;Array<MqlTick> mock_ticks;
void ResetLastError(){mock_last_error=0;}int GetLastError(){return mock_last_error;}
template<size_t N>int CopyTicksRange(string,MqlTick (&out)[N],int,ulong from,ulong to){
 if(!ticks_available){mock_last_error=4403;return -1;}int n=0;
 for(auto t:mock_ticks)if((ulong)t.time_msc>=from && (ulong)t.time_msc<=to){if(n==(int)N){mock_last_error=4407;return n;}out[n++]=t;}
 return n;
}

template<class T>void ArraySort(Array<T>&v){std::sort(v.begin(),v.end());}
