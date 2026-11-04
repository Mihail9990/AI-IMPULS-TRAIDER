"""Authenticated bounded diagnostic projections, never arbitrary file access."""
from datetime import datetime,timezone,timedelta
import json
import re
import threading
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .context_diagnostics import project,VERSION,BASE
from .log_export import build_document,EVENTS,PHASES

LIMIT=1024*1024
PAGE=16000
STATUS={'UNKNOWN','APPLIED','REJECTED','CONFLICT','OBSERVED','ACKNOWLEDGED','NOT_SENT'}

class DiagnosticLogs:
    def __init__(self,runtime_path=None):
        self.runtime_path=runtime_path;self.lock=threading.Lock();self.documents={};self.signatures={};self.handlers={};self.recent={}
    def capture(self,scope,store,state,now,view=None):
        stamp=datetime.fromtimestamp(now,timezone.utc).isoformat()
        # Only changes in decisions/state create records; quotes are context,
        # never a perpetual raw tick feed. Repeated unchanged snapshots are skipped.
        context=project(dict(view or {},settings=state.get('basket_settings',{}),phase=state.get('phase'),cycle_id=(state.get('cycle') or {}).get('id'),recovery=(state.get('cycle') or {}).get('recovery'),commands=list(state.get('commands',{}).values())[-20:]))
        stable=dict(context);stable.pop('quote',None)
        signature=json.dumps(stable,sort_keys=True)
        if self.signatures.get(scope)!=signature:
            self.signatures[scope]=signature
            record=dict(version=VERSION,base_commit=BASE,scope=scope,observed_utc=stamp,server_display_utc_plus_2=datetime.fromtimestamp(now,timezone(timedelta(hours=2))).isoformat(),context=context)
            self.recent.setdefault(scope,[]).append(record);self.recent[scope]=self.recent[scope][-100:]
            if self.runtime_path:
                try:
                    if scope not in self.handlers:
                        handler=RotatingFileHandler(str(self.runtime_path)+'.'+scope+'.diagnostic.jsonl',maxBytes=5_000_000,backupCount=3,encoding='utf-8');handler.setFormatter(logging.Formatter('%(message)s'));self.handlers[scope]=handler
                    self.handlers[scope].emit(logging.LogRecord('diagnostic',logging.INFO,'',0,json.dumps(record,ensure_ascii=False),(),None))
                except OSError:pass # diagnostics never authorize or alter a trade
        rows=[]
        # Owner thread only. Old audit rows have no timestamp: never invent it.
        for seq,event,payload in store.db.execute('SELECT seq,event,payload FROM audit ORDER BY seq DESC LIMIT 200').fetchall()[::-1]:
            name=event.split(':',1)[0]
            if name not in EVENTS:continue
            data=json.loads(payload);rows.append(json.dumps(dict(audit_seq=seq,event=name,phase=data.get('phase') if data.get('phase') in PHASES else 'OTHER',event_utc=None),ensure_ascii=False))
        for item in state.get('recovery_writeoffs',[])[-100:]:
            try:
                from .settings import number
                if not re.fullmatch('[a-f0-9]{32}',item['id']):continue
                rows.append(json.dumps(dict(event='RECOVERY_WAIVED',id=item['id'],amount=str(number(item['amount'])),event_utc=datetime.fromtimestamp(float(number(item['at_utc'])),timezone.utc).isoformat(),accounting='WAIVE_RECOVERY_ONLY_NO_PNL_DEBIT'),ensure_ascii=False))
            except (ValueError,TypeError,KeyError,OverflowError):continue
        for rid,r in list(state.get('control_receipts',{}).items())[-200:]:
            if re.fullmatch(r'[a-zA-Z0-9_-]{8,80}',rid) and r.get('status') in STATUS:
                rows.append(json.dumps(project(dict(receipt_id=rid,status=r['status'],revision=r.get('revision'))),ensure_ascii=False))
        rows.extend(json.dumps(r,ensure_ascii=False) for r in reversed(self.recent.get(scope,[])))
        header=f'Manifest: version={VERSION}; base={BASE}; scope={scope}; max_export_bytes={LIMIT}; recent_snapshots=100; audit_tail=200; raw_messages/secrets/headers/credentials/account_ids omitted.\nЛоги: {scope}. Снимок UTC: {stamp}\nВремя сервера: UTC+2 только отображение, не сдвиг epoch. Старые audit без event_utc не датируются задним числом.\nОграниченная диагностическая проекция; секреты, счета и произвольные сообщения не экспортируются.\n'
        text=header+'\n'.join(rows)
        with self.lock:self.documents[scope]=text.encode('utf-8')[-LIMIT:].decode('utf-8',errors='replace')
    def document(self,scope):
        if scope=='runtime':
            if not self.runtime_path:return 'Лог-файл не настроен.\n'
            return build_document(self.runtime_path).decode('utf-8',errors='replace')
        if scope not in {'live','paper'}:raise ValueError('Invalid log scope')
        with self.lock:text=self.documents.get(scope,'Логи этого контура пока отсутствуют.\n')
        archived=[]
        if self.runtime_path:
            for suffix in ('.3','.2','.1',''):
                path=Path(str(self.runtime_path)+'.'+scope+'.diagnostic.jsonl'+suffix)
                try:
                    if path.is_symlink() or not path.is_file():continue
                    with path.open('rb') as f:
                        f.seek(0,2);size=f.tell();start=max(0,size-128*1024);f.seek(start);raw=f.read(128*1024)
                    if start:raw=raw.split(b'\n',1)[-1]
                    for line in raw.splitlines():
                        if len(line)>65536:continue
                        try:r=json.loads(line)
                        except (ValueError,UnicodeError):continue
                        if not isinstance(r,dict) or r.get('scope')!=scope or r.get('version') not in {VERSION,'demo-diagnostics-v1'}:continue
                        safe={'scope':scope,'version':VERSION,'context':project(r.get('context',{}))}
                        for key in ('observed_utc','server_display_utc_plus_2'):
                            if isinstance(r.get(key),str) and re.fullmatch(r'[0-9T:.+Z-]{20,40}',r[key]):safe[key]=r[key]
                        archived.append(json.dumps(safe,ensure_ascii=False))
                except OSError:continue
        combined=text+'\nСохранённые контекстные записи (возможны повторы текущего снимка; ограниченные хвосты 4 × 128 KiB):\n'+'\n'.join(archived)
        raw=combined.encode('utf-8');cut=len(raw)>LIMIT-256
        if cut:combined=raw[-(LIMIT-512):].decode('utf-8',errors='replace')
        return f'Manifest version={VERSION}; scope={scope}; max_bytes={LIMIT}; truncated={cut}; secrets/raw_comments omitted; UTC+2 display only.\n'+combined
    def close(self):
        for h in self.handlers.values():h.close()
    def response(self,scope,page=0,download=False):
        text=self.document(scope)
        if download:return text.encode('utf-8')[:LIMIT]
        if type(page) is not int or not 0<=page<=100:raise ValueError('Invalid page')
        return dict(scope=scope,page=page,text=text[page*PAGE:(page+1)*PAGE],has_more=(page+1)*PAGE<len(text),bounded=True)
