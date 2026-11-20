"""Exercise the session journal with a C++ account double, not a native terminal."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

MOCK = r"""
#include <cassert>
#include <cstdint>
#include <string>
#include <vector>
using string=std::string;
using ulong=std::uint64_t;
using uint=unsigned int;
const int TERMINAL_CONNECTED=0,ACCOUNT_LOGIN=1,ACCOUNT_SERVER=2;
string _Symbol="XAUUSD.s",server="Test-Server";
long login=12;
bool connected=true, memory_ok=true;
int StringLen(const string &s){return static_cast<int>(s.size());}
string StringSubstr(const string &s,int at,int n=-1){return at>StringLen(s)?"":s.substr(at,n);}
bool RecoveryInvHex(const string&s){return s.size()==32 && s.find_first_not_of("0123456789abcdef")==string::npos;}
bool RecoveryInvContext(const string&s){return !s.empty()&&s.find_first_of("\t\r\n")==string::npos;}
bool TerminalInfoInteger(int){return connected;}
long AccountInfoInteger(int){return login;}
string AccountInfoString(int){return server;}
template<class T> int ArraySize(const std::vector<T>& x){return static_cast<int>(x.size());}
template<class T> int ArrayResize(std::vector<T>& x, int size,int) {
    if(!memory_ok)return -1;
    x.resize(size);return size;
}
"""
TESTS = r"""
int main(){
    RecoveryIntentKey key{string(32,'a'),12,"Test-Server","XAUUSD.s",18,0,string(64,'b')};
    string ticket;
    assert(RecoveryIntentLookup(key,ticket)==RI_NO_RECORD);
    assert(!RecoveryIntentRecordTicket(key,10));
    memory_ok=false;
    assert(!RecoveryIntentReserve(key) && RecoverySessionIntents.empty());
    memory_ok=true;
    assert(RecoveryIntentReserve(key));
    assert(!RecoveryIntentReserve(key) && RecoverySessionIntents.size()==1);
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
    connected=false;
    assert(RecoveryIntentLookup(key,ticket)==RI_UNAVAILABLE && ticket.empty());
    connected=true;
    login=13;
    assert(RecoveryIntentLookup(key,ticket)==RI_CONFLICT && !RecoveryIntentReserve(key));
    login=12;
    assert(RecoveryIntentLookup(key,ticket)==RI_TICKET);
    // New EA instance: no disk lookup and no inherited suppression.
    RecoverySessionIntents.clear();
    assert(RecoveryIntentLookup(key,ticket)==RI_NO_RECORD && ticket.empty());
    other=key; other.id="../somewhere";
    assert(!RecoveryIntentReserve(other));
    other=key; other.server="x\ty";
    assert(!RecoveryIntentReserve(other));
}
"""

class TestIntentJournal(unittest.TestCase):
    def test_in_session_identity_and_fresh_instance(self):
        gxx = shutil.which("g++")
        self.assertIsNotNone(gxx)
        source = Path(__file__).with_name("RecoveryIntentJournal.mqh").read_text()
        self.assertNotRegex(source, r"OrderSend|TRADE_ACTION|CTrade")
        source = "\n".join(line for line in source.splitlines() if not line.startswith("#include"))
        self.assertNotRegex(source, r"FileOpen|FileIsExist|FileMove|RECOVERY_INTENTS")
        source = source.replace("RecoverySessionIntent RecoverySessionIntents[];",
                                "std::vector<RecoverySessionIntent> RecoverySessionIntents;")
        source = source.replace("(string)RecoverySessionIntents[at].order",
                                "std::to_string(RecoverySessionIntents[at].order)")
        with tempfile.TemporaryDirectory() as folder:
            cpp = Path(folder) / "journal.cpp"
            cpp.write_text(MOCK + source + "\n" + TESTS)
            binary = cpp.with_suffix("")
            subprocess.run([gxx, "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsanitize=undefined",
                            str(cpp), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

if __name__ == "__main__":
    unittest.main()
