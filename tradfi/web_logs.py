"""Authenticated bounded diagnostic projections, never arbitrary file access."""
from datetime import datetime,timezone,timedelta
import json
import re
import threading
import logging
import time
import queue
import os
import stat
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .context_diagnostics import project,VERSION,BASE
from .log_export import build_document,EVENTS,PHASES

LIMIT=1024*1024
PAGE=16000
class SafeRotatingFileHandler(RotatingFileHandler):
    """Never follow a log symlink; failures stay outside the trading path."""
    def _open(self):
        for suffix in ('', '.1', '.2', '.3'):
            path=Path(self.baseFilename+suffix)
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise OSError('Unsafe diagnostic destination')
        flags=os.O_WRONLY|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_NONBLOCK',0)
        fd=os.open(self.baseFilename,flags,0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd);raise OSError('Not a regular diagnostic file')
        return os.fdopen(fd,self.mode,encoding=self.encoding,errors=self.errors)
    def handleError(self,record):
        callback=getattr(self,'on_failure',None)
        if callback:callback()  # No record data or exception text is exposed.


def stable_context(value):
    if isinstance(value,dict):
        volatile={'quote','timestamp','sequence','age_seconds','time_msc','utc_time','profit','timings'}
        return {k:stable_context(v) for k,v in value.items() if k not in volatile}
    if isinstance(value,list):return [stable_context(v) for v in value]
    return value

STATUS={'UNKNOWN','APPLIED','REJECTED','CONFLICT','OBSERVED','ACKNOWLEDGED','NOT_SENT'}

class DiagnosticLogs:
    def __init__(self,runtime_path=None):
        self.runtime_path=runtime_path;self.lock=threading.Lock();self.documents={};self.signatures={};self.handlers={};self.recent={};self.last_record={};self.failures=0;self.last_capture_ms=0;self.monotonic=time.monotonic
        self.pending=queue.Queue(maxsize=100);self.dropped=0;self.closed=False
        self.worker=None
        if runtime_path:
            self.worker=threading.Thread(target=self._writer,daemon=True,name='diagnostic-writer');self.worker.start()
    def _write_failed(self):
        self.failures+=1
    def _writer(self):
        try:
            while True:
                try:item=self.pending.get(timeout=0.1)
                except queue.Empty:
                    if self.closed:return
                    continue
                try:
                    if item is None:return
                    scope,record=item
                    if scope not in self.handlers:
                        handler=SafeRotatingFileHandler(str(self.runtime_path)+'.'+scope+'.diagnostic.jsonl',maxBytes=5_000_000,backupCount=3,encoding='utf-8')
                        handler.setFormatter(logging.Formatter('%(message)s'));handler.on_failure=self._write_failed;self.handlers[scope]=handler
                    self.handlers[scope].emit(logging.LogRecord('diagnostic',logging.INFO,'',0,record,(),None))
                except Exception:
                    self.failures+=1
                finally:self.pending.task_done()
        finally:
            for handler in self.handlers.values():handler.close()

    def capture(self,scope,store,state,now,view=None):
        start=self.monotonic()
        try:
            if scope!='live':raise ValueError('Invalid log scope')
            self._capture(scope,store,state,now,view)
        except Exception:
            self.failures+=1  # No raw exception strings, secrets, or trading effects.
        finally:
            self.last_capture_ms=max(0,(self.monotonic()-start)*1000)

    def _capture(self,scope,store,state,now,view=None):
        stamp=datetime.fromtimestamp(now,timezone.utc).isoformat()
        # Only changes in decisions/state create records; quotes are context,
        # never a perpetual raw tick feed. Repeated unchanged snapshots are skipped.
        context=project(dict(view or {},settings=state.get('basket_settings',{}),phase=state.get('phase'),cycle_id=(state.get('cycle') or {}).get('id'),recovery=(state.get('cycle') or {}).get('recovery'),commands=list(state.get('commands',{}).values())[-20:],cycle=state.get('cycle') or {},completed=state.get('completed',[])[-20:],control_receipts=list(state.get('control_receipts',{}).values())[-50:],recovery_writeoffs=state.get('recovery_writeoffs',[])[-20:]))
        stable=stable_context(context)
        signature=json.dumps(stable,sort_keys=True)
        mono=self.monotonic()
        changed=self.signatures.get(scope)!=signature
        if changed or mono-self.last_record.get(scope,float('-inf'))>=60:
            self.signatures[scope]=signature
            self.last_record[scope]=mono
            context['record_kind']='CHANGE' if changed else 'HEARTBEAT'
            record=dict(version=VERSION,base_commit=BASE,scope=scope,observed_utc=stamp,server_display_utc_plus_2=datetime.fromtimestamp(now,timezone(timedelta(hours=2))).isoformat(),context=context)
            # The archive reader accepts 64 KiB lines. Keep every record below
            # that limit and disclose omitted fields instead of dropping a line.
            omitted=0
            while len(json.dumps(record,ensure_ascii=False).encode('utf-8'))>60000:
                keys=[k for k in context if k not in {'truncated','omitted_fields','record_kind'}]
                if not keys:break
                key=max(keys,key=lambda k:len(json.dumps(context[k],ensure_ascii=False)))
                del context[key];omitted+=1
                context.update(truncated=True,omitted_fields=omitted)
            self.recent.setdefault(scope,[]).append(record);self.recent[scope]=self.recent[scope][-100:]
            if self.runtime_path and not self.closed:
                try:self.pending.put_nowait((scope,json.dumps(record,ensure_ascii=False)))
                except queue.Full:self.dropped+=1
        else:
            return  # Audit export refreshes on change or heartbeat, not every tick.
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
                rows.append(json.dumps(dict(event='RECOVERY_WAIVED',id=project({'id':item['id']})['id'],amount=str(number(item['amount'])),event_utc=datetime.fromtimestamp(float(number(item['at_utc'])),timezone.utc).isoformat(),accounting='WAIVE_RECOVERY_ONLY_NO_PNL_DEBIT'),ensure_ascii=False))
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
        if scope != 'live':raise ValueError('Invalid log scope')
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
                        if not isinstance(r,dict) or r.get('scope')!=scope or r.get('version') not in {VERSION,'panel-diagnostics-v1','demo-diagnostics-v1'}:continue
                        safe={'scope':scope,'version':VERSION,'context':project(r.get('context',{}))}
                        for key in ('observed_utc','server_display_utc_plus_2'):
                            if isinstance(r.get(key),str) and re.fullmatch(r'[0-9T:.+Z-]{20,40}',r[key]):safe[key]=r[key]
                        archived.append(json.dumps(safe,ensure_ascii=False))
                except OSError:continue
        combined=text+'\nСохранённые контекстные записи (возможны повторы текущего снимка; ограниченные хвосты 4 × 128 KiB):\n'+'\n'.join(archived)
        raw=combined.encode('utf-8');cut=len(raw)>LIMIT-256
        if cut:combined=raw[-(LIMIT-512):].decode('utf-8',errors='replace')
        return f'Manifest version={VERSION}; scope={scope}; max_bytes={LIMIT}; truncated={cut}; queue_dropped={self.dropped}; diagnostic_failures={self.failures}; secrets/raw_comments omitted; UTC+2 display only.\n'+combined
    def close(self):
        if self.closed:return
        self.closed=True
        if self.worker:
            try:self.pending.put_nowait(None)
            except queue.Full:return  # Never wait for a blocked filesystem.
            self.worker.join(timeout=0.2)
            if self.worker.is_alive():return
        for h in self.handlers.values():h.close()
    def response(self,scope,page=0,download=False):
        text=self.document(scope)
        if download:return text.encode('utf-8')[:LIMIT]
        if type(page) is not int or not 0<=page<=100:raise ValueError('Invalid page')
        return dict(scope=scope,page=page,text=text[page*PAGE:(page+1)*PAGE],has_more=(page+1)*PAGE<len(text),bounded=True)
