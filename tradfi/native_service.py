"""Native-EA web/Telegram service. No terminal API, prices or order execution here."""
import argparse
import json
from pathlib import Path
import threading
import time
from .native_transport import NativeOutbox
from .web_control import WebTransport
from .web_auth import resolve_password
from .web_logs import DiagnosticLogs

class NativeMailbox:
    def __init__(self, directory, identity, *, clock=time.time):
        self.root=Path(directory);self.identity=identity;self.clock=clock
        self.outbox=NativeOutbox(directory,identity)
        self.scope=identity['installation']

    def view(self):
        path=self.root/'native-view.json'
        result=dict(scope=self.scope,revision=0,phase='DISCONNECTED',reason='Нет свежего состояния EA',positions=[],orders=[],settings={},read_only=True)
        try:
            if not 0 <= self.clock()-path.stat().st_mtime <= 5 or path.stat().st_size>2_000_000:
                raise ValueError('Stale or oversized native state')
            data=json.loads(path.read_text(encoding='utf-8-sig'))
            if data.get('protocol')!='native_state_v1' or any(data.get(k)!=v for k,v in self.identity.items()):
                raise ValueError('Native state identity mismatch')
            if type(data.get('revision')) is not int or data['revision']<0:raise ValueError('Invalid revision')
            result.update(data)
        except (OSError,ValueError,TypeError):pass
        receipts={}
        with self.outbox.lock:
            ids=[r[0] for r in self.outbox.db.execute('SELECT id FROM requests ORDER BY rowid DESC LIMIT 100')]
        for rid in ids:
            path=self.root/'native_receipts'/f'{rid}.json'
            try:
                if path.stat().st_size>16384:raise ValueError('Oversized receipt')
                self.outbox.accept_receipt(json.loads(path.read_text(encoding='utf-8-sig')))
            except (OSError,ValueError,TypeError):pass
            receipts[rid]=self.outbox.status(rid)
        result['action_results']=receipts
        return result

    def submit(self, request):
        if not isinstance(request,dict) or set(request)!={'id','revision','action'}:
            raise ValueError('Invalid action envelope')
        return self.outbox.submit(request['id'],request['action'],request['revision'])

    def close(self):self.outbox.close()

class EAPanel:
    def __init__(self, config, *, clock=time.time):
        self.authority=config.get('web',{}).get('host','127.0.0.1')+':'+str(config.get('web',{}).get('port',8765))
        self.mailbox=NativeMailbox(config['common_files_directory'],config['identity'],clock=clock)
        self.logs=DiagnosticLogs(config.get('log_file','native-panel.log'))
        self.transport=WebTransport(self.mailbox,authority=self.authority,password=resolve_password(config.get('web',{})),public_origin=config.get('web',{}).get('public_origin'),logs=self.logs)
        self.server=None;self.last_revision=None;self.log_db=__import__('sqlite3').connect(':memory:')
        self.log_db.execute('CREATE TABLE audit(seq INTEGER,event TEXT,payload TEXT)')

    def start(self):
        # Reuse authenticated, bounded transport; static assets are specific to
        # this backend so a legacy controller can never receive these actions.
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from urllib.parse import urlsplit
        panel=self;root=Path(__file__).resolve().parents[1]/'web/native-panel'
        class Handler(BaseHTTPRequestHandler):
            def setup(self):super().setup();self.connection.settimeout(2)
            def do_GET(self):self.handle_call()
            def do_POST(self):self.handle_call()
            def handle_call(self):
                route=urlsplit(self.path).path
                static={'/':('index.html','text/html; charset=utf-8'),'/panel.js':('panel.js','text/javascript'),'/panel.css':('panel.css','text/css'),'/native-adapter.js':('native-adapter.js','text/javascript'),'/diagnostics.js':('diagnostics.js','text/javascript')}
                if self.command=='GET' and route in static and self.headers.get('Host')==panel.authority:
                    name,mime=static[route];return self.reply(200,(root/name).read_bytes(),mime,{})
                try:n=int(self.headers.get('Content-Length','0'))
                except ValueError:n=-1
                if not 0<=n<=8192:return self.reply(413,b'{}','application/json',{})
                code,data,extra=panel.transport.request(self.command,self.path,dict(self.headers),self.rfile.read(n))
                return self.reply(code,data if isinstance(data,bytes) else json.dumps(data,allow_nan=False).encode(),'application/json',extra)
            def reply(self,code,body,mime,extra):
                self.send_response(code)
                for k,v in {'Content-Type':mime,'Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'self'; frame-ancestors 'none'",**extra}.items():self.send_header(k,v)
                self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        class Server(ThreadingHTTPServer):
            daemon_threads=True
            slots=threading.BoundedSemaphore(8)
            def process_request(self,request,address):
                if not self.slots.acquire(False):self.shutdown_request(request);return
                try:super().process_request(request,address)
                except BaseException:self.slots.release();raise
            def process_request_thread(self,request,address):
                try:super().process_request_thread(request,address)
                finally:self.slots.release()
        endpoint=urlsplit('http://'+self.authority)
        self.server=Server((endpoint.hostname,endpoint.port),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()

    def capture(self):
        from types import SimpleNamespace
        view=self.mailbox.view()
        state=dict(phase=view['phase'],cycle=dict(id=view.get('cycle_id'),recovery=view.get('recovery')),commands={},completed=[],control_receipts={},basket_settings=view.get('settings',{}))
        self.logs.capture('live',SimpleNamespace(db=self.log_db),state,time.time(),view=view)

    def close(self):
        if self.server:self.server.shutdown();self.server.server_close()
        self.logs.close();self.mailbox.close();self.log_db.close()


def run_native(config,stop_event=None):
    if config.get('backend')!='ea_native_v1':raise ValueError('Native backend required')
    if config.get('web',{}).get('host','127.0.0.1')!='127.0.0.1':raise ValueError('Native panel must bind 127.0.0.1')
    panel=EAPanel(config)
    try:
        panel.start()
        while stop_event is None or not stop_event.wait(.2):
            panel.capture()
            if stop_event is None:time.sleep(.2)
    finally:panel.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    run_native(json.loads(Path(args.config).read_text(encoding='utf-8-sig')))
