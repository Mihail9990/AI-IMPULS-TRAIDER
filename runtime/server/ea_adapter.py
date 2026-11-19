"""Local MT5 Common Files adapter. No retry after ambiguous execution."""
import csv
import os
import time
from pathlib import Path
from types import SimpleNamespace as N
from uuid import UUID

class EAAdapter:
    IS_EA_BRIDGE = True
    TRADE_ACTION_SLTP = 6
    def __init__(self, directory, account, server, timeout=7):
        self.root=Path(directory); self.account=account; self.server=server; self.timeout=timeout
    def initialize(self, **kwargs):
        return self.root.is_dir()
    def shutdown(self): pass
    def _snapshot(self):
        path=self.root/'snapshot.tsv'
        try:
            if time.time()-path.stat().st_mtime > 3: return None
            rows=list(csv.reader(path.read_text(encoding='utf-8-sig').splitlines(), delimiter='\t'))
            h=rows[0]
            if h[0]!='H' or int(h[1])!=self.account or h[2]!=self.server: return None
            positions=[]; symbols={}
            for r in rows[1:]:
                if r[0]=='P':
                    positions.append(N(ticket=int(r[1]),symbol=r[2],type=int(r[3]),volume=float(r[4]),price_open=float(r[5]),sl=float(r[6]),tp=float(r[7]),profit=float(r[8]),identifier=int(r[9])))
                elif r[0]=='S':
                    symbols[r[1]]=N(trade_tick_size=float(r[2]),point=float(r[3]),trade_stops_level=int(r[4]),trade_freeze_level=int(r[5]),bid=float(r[6]),ask=float(r[7]))
            return N(login=int(h[1]),server=h[2],connected=h[3]=='1',positions=positions,symbols=symbols)
        except (OSError,ValueError,IndexError): return None
    def account_info(self): return self._snapshot()
    def terminal_info(self): return self._snapshot()
    def positions_get(self, ticket=None):
        s=self._snapshot()
        return None if s is None else tuple(p for p in s.positions if ticket is None or p.ticket==ticket)
    def symbol_info(self,symbol):
        s=self._snapshot(); return None if s is None else s.symbols.get(symbol)
    symbol_info_tick=symbol_info
    def order_send(self, request):
        if request['action'] != 6: raise ValueError('Only SLTP supported')
        rid=str(UUID(request['request_id']))
        result=self.root/(rid+'.result'); target=self.root/(rid+'.request')
        # Unique request ID is durably recorded by HTTP service; do not overwrite consumed files.
        if result.exists() or target.exists() or (self.root/(rid+'.claimed')).exists(): return None
        fields=[rid,int(time.time())+5,self.account,self.server,request['position'],request['expected_symbol'],request['expected_entry'],request['expected_type'],request['expected_identifier'],request['expected_volume'],int(request['change_sl']),int(request['change_tp']),request['sl'],request['tp']]
        if any('\t' in str(f) or '\n' in str(f) or '\r' in str(f) for f in fields): raise ValueError('Invalid bridge field')
        tmp=self.root/(rid+'.tmp')
        with tmp.open('x',encoding='utf-8',newline='') as f:
            csv.writer(f,delimiter='\t',lineterminator='\n').writerow(fields); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,target)
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            try:
                rows=result.read_text(encoding='utf-8-sig').strip().split('\t')
                if rows[0]==rid: return N(retcode=int(rows[1]))
            except (OSError,ValueError,IndexError): pass
            time.sleep(.05)
        return None
