// Offline adapter for the actual serialization functions extracted from the EA.
// Not an MQL compiler or terminal emulator. No broker I/O functions are provided.
#include <string>
#include <vector>
#include <map>
#include <sstream>
#include <iomanip>
#include <iostream>
#include <limits>
using string = std::string;
using ushort = unsigned short;
struct datetime {
    long value;
    datetime(long v=0):value(v) {}
    explicit operator long() const {return value;}
};
// Deliberately distinguish datetime from long: MQL calendar string vs epoch integer.
struct MqlString : string {
    MqlString(long n):string(std::to_string(n)) {}
    MqlString(unsigned long n):string(std::to_string(n)) {}
    MqlString(unsigned int n):string(std::to_string(n)) {}
    MqlString(datetime):string("2026.10.11 12:00:00") {}
};
string IntegerToString(long n) {return std::to_string(n);}
string DoubleToString(double n,int places) {
    std::ostringstream out; out << std::fixed << std::setprecision(places) << n; return out.str();
}
int StringLen(const string &s) {return int(s.size());}
ushort StringGetCharacter(const string &s,int i) {return static_cast<unsigned char>(s.at(i));}
string StringSubstr(const string &s,int i,int count) {return s.substr(i,count);}
std::map<string,string> files;
bool WriteAtomic(string name,string body) {files[name]=body; return true;}
string TradeSymbol="XAUUSD.s";
ulong Magic=25101101;
bool populated=false, connected=true;
string server="Bybit-Live-6",comment="fixture";
datetime clock_value(1791720000), candle_value(1791719940);
struct MqlTick {double bid=4000.1,ask=4000.2; datetime time=clock_value;};
struct MqlRates {datetime time=candle_value; double close=4000;};
datetime TimeGMT() {return clock_value;}
datetime TimeCurrent() {return clock_value;}
datetime TimeTradeServer() {return clock_value;}
bool SymbolInfoTick(string,MqlTick &tick) {tick=MqlTick(); return true;}
int CopyRates(string,int,int,int,MqlRates *bar) {bar[0]=MqlRates(); return 1;}
bool HistorySelect(datetime,datetime) {return true;}
int OrdersTotal() {return populated?2:0;}
int PositionsTotal() {return populated?2:0;}
int HistoryOrdersTotal() {return populated?2:0;}
int HistoryDealsTotal() {return populated?2:0;}
ulong OrderGetTicket(int i) {return 9007199254740993UL+i;}
ulong PositionGetTicket(int i) {return 9007199254740995UL+i;}
ulong HistoryOrderGetTicket(int i) {return 9007199254740997UL+i;}
ulong HistoryDealGetTicket(int i) {return 9007199254740999UL+i;}
long AccountInfoInteger(int key) {
    if(key==ACCOUNT_LOGIN) return 111111111; // nine synthetic digits: never a real account
    return ACCOUNT_MARGIN_MODE_RETAIL_HEDGING;
}
string AccountInfoString(int key) {return key==ACCOUNT_SERVER?server:"UST";}
long TerminalInfoInteger(int) {return connected;}
string SymbolInfoString(string,int) {return "USD";}
long SymbolInfoInteger(string,int) {return 1;}
double SymbolInfoDouble(string,int) {return .01;}
long OrderGetInteger(int key) {
    if(key==ORDER_MAGIC) return long(Magic);
    if(key==ORDER_TYPE) return ORDER_TYPE_BUY_STOP;
    return ORDER_STATE_FILLED;
}
string OrderGetString(int key) {return key==ORDER_SYMBOL?TradeSymbol:comment;}
long HistoryOrderGetInteger(ulong,int key) {return OrderGetInteger(key);}
string HistoryOrderGetString(ulong,int key) {return OrderGetString(key);}
long PositionGetInteger(int key) {
    if(key==POSITION_IDENTIFIER) return 9007199254740995L;
    if(key==POSITION_MAGIC) return long(Magic);
    return POSITION_TYPE_BUY;
}
string PositionGetString(int) {return TradeSymbol;}
double PositionGetDouble(int) {return 4000.125;}
string HistoryDealGetString(ulong,int) {return TradeSymbol;}
long HistoryDealGetInteger(ulong,int key) {
    if(key==DEAL_ENTRY) return DEAL_ENTRY_OUT;
    if(key==DEAL_REASON) return DEAL_REASON_SL;
    if(key==DEAL_TYPE) return DEAL_TYPE_BUY;
    if(key==DEAL_TIME_MSC) return 1791720000123L;
    return 9007199254740995L;
}
double HistoryDealGetDouble(ulong,int) {return -.125;}
