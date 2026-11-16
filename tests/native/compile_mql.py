"""Compile actual EA sources after mechanical MQL-array/input syntax adaptation.
Not MetaEditor: terminal types/API are a deterministic C++ mock, explicitly.
"""
from pathlib import Path
import re
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[2]

def transform(text):
    text=re.sub(r'^#property[^\n]*','',text,flags=re.M)
    text=re.sub(r'^input ', '', text, flags=re.M)
    # Dynamic MQL arrays only. Fixed native policy arrays stay ordinary C++.
    text=re.sub(r'\b(\w+)\s+&(\w+)\[\]',r'Array<\1> &\2',text)
    pattern=r'\b(string|ulong|int|bool|double|uchar|MqlRates|NativeCommand)\s+((?:\w+\[\]\s*,\s*)*\w+\[\])\s*;'
    def arrays(m):
        return '\n'.join('Array<'+m[1]+'> '+name.strip()[:-2]+';' for name in m[2].split(','))
    text=re.sub(pattern,arrays,text)
    # Mixed declarations such as string body=...,lines[],header[].
    text=re.sub(r',\s*(\w+)\[\]',r'; Array<string> \1',text)
    return text

def compile_source(ea, test_body='int main(){return 0;}', run=True):
    with tempfile.TemporaryDirectory() as d:
        out=Path(d)
        for p in (ROOT/'mt5/native').glob('*'):
            if p.suffix in ('.mq5','.mqh'):(out/p.name).write_text(transform(p.read_text()))
        source=out/'main.cpp'
        source.write_text('#include "'+str(ROOT/'tests/native/mt5_mock.hpp')+'"\n#include "'+ea+'"\n'+test_body)
        subprocess.run(['g++','-std=c++17','-Wall','-Wextra','-Wno-unused-parameter','-Wno-sign-compare',str(source),'-o',str(out/'test')],check=True,capture_output=True,text=True)
        if run:return subprocess.run([str(out/'test')],check=True,capture_output=True,text=True).stdout

if __name__=='__main__':
    for name in ('ImpulsTrader.mq5','ImpulsObserver.mq5','ImpulsSLMonitor.mq5'):
        try:compile_source(name);print(name,'mock compile OK')
        except subprocess.CalledProcessError as e:print(e.stderr);raise
