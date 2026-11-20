"""Exercise MQL journal in a C++ filesystem double, not a native terminal."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MOCK = r"""
#include <cassert>
#include <cstdint>
#include <map>
#include <string>
#include <vector>
using string=std::string;
using ulong=std::uint64_t;
using uint=unsigned int;
using uchar=unsigned char;
const int INVALID_HANDLE=-1,FILE_READ=1,FILE_WRITE=2,FILE_BIN=4,CP_UTF8=8,WHOLE_ARRAY=-1;
const int TERMINAL_CONNECTED=0,ACCOUNT_LOGIN=1,ACCOUNT_SERVER=2;
std::map<string,string> files; string path;
string _Symbol="XAUUSD.s",server="Test-Server";
long login=12;
bool connected=true,move_ok=true;
int StringLen(const string &s){return static_cast<int>(s.size());}
string StringSubstr(const string &s,int at,int n=-1){return at>StringLen(s)?"":s.substr(at,n);}
bool RecoveryInvHex(const string&s){return s.size()==32 && s.find_first_not_of("0123456789abcdef")==string::npos;}
bool RecoveryInvContext(const string&s){return !s.empty()&&s.find_first_of("\t\r\n")==string::npos;}
bool RecoveryInvNatural(const string&s,const string&max,bool pos){
    return !s.empty() && s.find_first_not_of("0123456789")==string::npos &&
           !(s.size()>1&&s[0]=='0') && (!pos||s!="0") && (s.size()<max.size() || (s.size()==max.size()&&s<=max));
}
bool TerminalInfoInteger(int){return connected;}
long AccountInfoInteger(int){return login;}
string AccountInfoString(int){return server;}
long ChartID(){return 443;}
bool FileIsExist(const string&p){return files.count(p);}
bool FolderCreate(const string&){return true;}
int FileOpen(const string&p,int mode){path=p;if(mode&FILE_WRITE)files[p]="";return FileIsExist(p)?1:INVALID_HANDLE;}
ulong FileSize(int){return files.at(path).size();}
void FileClose(int){}
void FileFlush(int){}
uint FileReadArray(int,std::vector<uchar>&bytes,int,int n){
    const auto &s=files.at(path);bytes.assign(s.begin(),s.end());return n;
}
uint FileWriteArray(int,const std::vector<uchar>&bytes,int,int n){
    files[path]=string(bytes.begin(),bytes.begin()+n);return n;
}
bool FileDelete(const string&p){return files.erase(p);}
bool FileMove(const string&src,int,const string&dst,int){
    if(!move_ok || !FileIsExist(src) || FileIsExist(dst))return false;
    files[dst]=files[src];files.erase(src);return true;
}
int StringToCharArray(const string&s,std::vector<uchar>&bytes,int,int,int){
    bytes.assign(s.begin(),s.end());bytes.push_back(0);return bytes.size();
}
string CharArrayToString(const std::vector<uchar>&bytes,int,int n,int){
    return string(bytes.begin(),bytes.begin()+n);
}
"""
TESTS = r"""
int main(){
    RecoveryIntentKey key{string(32,'a'),12,"Test-Server","XAUUSD.s",18,0,string(64,'b')};
    string ticket;
    assert(RecoveryIntentLookup(key,ticket)==RI_NO_RECORD);
    assert(!RecoveryIntentRecordTicket(key,10));
    move_ok=false;
    assert(!RecoveryIntentReserve(key) && files.empty());
    move_ok=true;
    assert(RecoveryIntentReserve(key));
    assert(!RecoveryIntentReserve(key) && files.size()==1);
    assert(RecoveryIntentLookup(key,ticket)==RI_UNRESOLVED && ticket.empty());
    auto other=key; other.generation=1;
    assert(RecoveryIntentLookup(other,ticket)==RI_CONFLICT);
    other=key; other.fingerprint[0]='c';
    assert(RecoveryIntentLookup(other,ticket)==RI_CONFLICT);
    assert(!RecoveryIntentRecordTicket(key,0));
    assert(RecoveryIntentRecordTicket(key,UINT64_MAX));
    assert(RecoveryIntentLookup(key,ticket)==RI_TICKET && ticket=="18446744073709551615");
    assert(!RecoveryIntentRecordTicket(key,9));
    assert(!RecoveryIntentReserve(key));
    auto base=RecoveryIntentPath(key);
    auto original=files[base+".ticket"];
    files[base+".ticket"]=original.substr(0,original.size()-1);
    assert(RecoveryIntentLookup(key,ticket)==RI_CONFLICT && ticket.empty());
    files[base+".ticket"]=original;
    connected=false;
    assert(RecoveryIntentLookup(key,ticket)==RI_UNAVAILABLE);
    connected=true;
    files.erase(base+".intent");
    assert(RecoveryIntentLookup(key,ticket)==RI_CONFLICT && !RecoveryIntentReserve(key));
    other=key; other.id="../somewhere";
    assert(!RecoveryIntentReserve(other));
    other=key; other.server="x\ty";
    assert(!RecoveryIntentReserve(other));
}
"""

class TestIntentJournal(unittest.TestCase):
    def test_restart_identity_and_create_only(self):
        gxx = shutil.which("g++")
        self.assertIsNotNone(gxx)
        source = Path(__file__).with_name("RecoveryIntentJournal.mqh").read_text()
        self.assertNotRegex(source, r"OrderSend|TRADE_ACTION|CTrade")
        source = "\n".join(line for line in source.splitlines() if not line.startswith("#include"))
        for name in ("bytes", "normalized"):
            source = source.replace(f"uchar {name}[];", f"std::vector<uchar> {name};")
        for expr in ("key.login", "key.magic", "key.generation", "ChartID()", "ticket"):
            source = source.replace("(string)" + expr, f"std::to_string({expr})")
        with tempfile.TemporaryDirectory() as folder:
            cpp = Path(folder) / "journal.cpp"
            cpp.write_text(MOCK + source + "\n" + TESTS)
            binary = cpp.with_suffix("")
            subprocess.run([gxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsanitize=undefined",
                            str(cpp), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

if __name__ == "__main__":
    unittest.main()
