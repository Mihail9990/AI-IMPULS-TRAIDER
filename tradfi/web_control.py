"""Authenticated loopback control transport. Workers cannot touch MT5 or SQLite."""
from copy import deepcopy
import hashlib
import hmac
from http.cookies import SimpleCookie,CookieError
import json
import queue
import secrets
import uuid
import threading
import time
from urllib.parse import urlsplit
from .control import OwnerMailbox,ActionGateway
from .chart_feed import ChartFeed


class WebTransport:
    def __init__(self,mailbox,*,authority='127.0.0.1:8765',password=None,clock=time.monotonic):
        if authority.split(':')[0]!='127.0.0.1':raise ValueError('Web server must bind loopback')
        self.mailbox=mailbox;self.authority=authority;self.password=password;self.clock=clock
        self.sessions={};self.attempts=[];self.lock=threading.Lock()
        if password is not None and (not isinstance(password,str) or len(password)<32):raise ValueError('Explicit web credential must have at least 32 characters')
    def request(self,method,path,headers,body=b''):
        headers={k.lower():v for k,v in headers.items()};origin='http://'+self.authority
        def reply(code,value,cookie=None):return code,value,({'Set-Cookie':cookie} if cookie else {})
        if headers.get('host')!=self.authority or headers.get('origin',origin)!=origin:return reply(403,{'error':'ORIGIN_DENIED'})
        if headers.get('sec-fetch-site','same-origin') not in {'same-origin','none'}:return reply(403,{'error':'ORIGIN_DENIED'})
        if headers.get('x-local-control')!='1':return reply(403,{'error':'HEADER_REQUIRED'})
        if not self.password:return reply(403,{'error':'AUTH_NOT_CONFIGURED'})
        route=urlsplit(path).path
        if method=='POST' and (headers.get('content-type')!='application/json' or len(body)>8192):return reply(400,{'error':'BOUNDED_JSON_REQUIRED'})
        if method=='POST' and route=='/api/login':
            try:
                payload=json.loads(body)
                if set(payload)!={'password'} or not isinstance(payload['password'],str):raise ValueError()
                supplied_password=payload['password'].encode('utf-8')
            except (ValueError,TypeError):return reply(400,{'error':'INVALID_LOGIN'})
            with self.lock:
                now=self.clock();self.attempts=[t for t in self.attempts if now-t<60]
                if len(self.attempts)>=10:return reply(429,{'error':'LOGIN_RATE_LIMIT'})
                self.attempts.append(now)
                if not hmac.compare_digest(supplied_password,self.password.encode('utf-8')):return reply(403,{'error':'AUTH_FAILED'})
                self.sessions={k:v for k,v in self.sessions.items() if v>now}
                if len(self.sessions)>=32:return reply(429,{'error':'SESSION_LIMIT'})
                sid=secrets.token_urlsafe(32);self.sessions[sid]=now+3600
            return reply(200,{'status':'AUTHENTICATED'},'tradfi_session='+sid+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600')
        cookie=SimpleCookie()
        try:cookie.load(headers.get('cookie',''));sid=cookie['tradfi_session'].value
        except (KeyError,ValueError,CookieError):return reply(401,{'error':'AUTH_REQUIRED'})
        with self.lock:
            if self.sessions.get(sid,0)<=self.clock():return reply(401,{'error':'SESSION_EXPIRED'})
        if method=='GET' and route=='/api/state':return reply(200,self.mailbox.view())
        if method=='POST' and route=='/api/logout':
            with self.lock:self.sessions.pop(sid,None)
            return reply(200,{'status':'SIGNED_OUT'},'tradfi_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if method=='POST' and route=='/api/action':
            view=self.mailbox.view()
            if not view.get('scope') or headers.get('x-controller-scope')!=view['scope']:return reply(409,{'error':'SCOPE_CHANGED'})
            try:return reply(202,self.mailbox.submit(json.loads(body)))
            except (ValueError,TypeError):return reply(400,{'error':'INVALID_ACTION'})
            except queue.Full:return reply(429,{'error':'QUEUE_FULL'})
        return reply(404,{'error':'NOT_FOUND'})


class NativePanel:
    def __init__(self,controller,*,authority='127.0.0.1:8765',password=None,clock=time.time,monotonic=time.monotonic):
        self.controller=controller;self.clock=clock;self.monotonic=monotonic
        self.mailbox=OwnerMailbox();self.gateway=ActionGateway(controller,execute=controller.action)
        self.feed=ChartFeed(controller.broker,clock=clock,monotonic=monotonic)
        self.transport=WebTransport(self.mailbox,authority=authority,password=password,clock=monotonic)
        self.authority=authority;self.next_feed=0;self.snapshot=None;self.server=None
        if 'web_store_id' not in controller.state:
            controller.state['web_store_id']=uuid.uuid4().hex
            controller.save('bind_web_store_identity')
        self.scope=hashlib.sha256((controller.broker.account+'|'+controller.broker.symbol+'|'+controller.state['web_store_id']).encode()).hexdigest()
    def step(self):
        if self.monotonic()>=self.next_feed:
            try:
                self.snapshot=self.controller.snapshot();self.feed.capture(self.snapshot)
            except (ValueError,OSError,TimeoutError):self.feed.disconnected()
            self.next_feed=self.monotonic()+1
        self.mailbox.drain(self.gateway,self.view)
    def view(self):
        s=self.controller.state;c=s.get('cycle') or {};snap=self.snapshot or {}
        feed=self.feed.view()
        return dict(source=feed['source'],scope=self.scope,revision=s.get('control_revision',0),
            symbol=self.controller.broker.symbol,phase=s['phase'],mode=s['operation_mode'],
            modules=deepcopy(s['modules']),cycle_id=c.get('id'),recovery=deepcopy(c.get('fixed_recovery')),
            time_block=deepcopy(s.get('time_block')),feed=feed,quote=feed.get('quote'),timestamp=feed.get('timestamp'),
            orders=deepcopy(snap.get('orders',[])),positions=deepcopy(snap.get('positions',[])),
            journal=[n['text'] for n in s['notifications'][-30:]],
            action_results={key:{'status':r['status'],'revision':r['revision']} for key,r in s.get('control_receipts',{}).items()})
    def start(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from .local_panel import ROOT
        transport=self.transport;authority=self.authority
        class Handler(BaseHTTPRequestHandler):
            def setup(self):super().setup();self.connection.settimeout(2)
            def do_GET(self):self.handle_call()
            def do_POST(self):self.handle_call()
            def handle_call(self):
                route=urlsplit(self.path).path
                static={'/':('index.html','text/html'),'/panel.js':('panel.js','text/javascript'),'/panel.css':('panel.css','text/css')}
                if self.command=='GET' and route in static and self.headers.get('Host')==authority:
                    filename,mime=static[route];self.reply(200,(ROOT/filename).read_bytes(),mime,{});return
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:size=-1
                if not 0<=size<=8192:self.reply(413,b'{}','application/json',{});return
                code,data,extra=transport.request(self.command,self.path,dict(self.headers),self.rfile.read(size))
                self.reply(code,json.dumps(data,allow_nan=False).encode(),'application/json',extra)
            def reply(self,code,body,mime,extra):
                self.send_response(code)
                for k,v in dict({'Content-Type':mime,'Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"frame-ancestors 'none'"},**extra).items():self.send_header(k,v)
                self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        class BoundedServer(ThreadingHTTPServer):
            daemon_threads=True
            slots=threading.BoundedSemaphore(8)
            def process_request(self,request,address):
                if not self.slots.acquire(blocking=False):
                    self.shutdown_request(request);return
                try:super().process_request(request,address)
                except BaseException:self.slots.release();raise
            def process_request_thread(self,request,address):
                try:super().process_request_thread(request,address)
                finally:self.slots.release()
        host,port=authority.split(':')
        self.server=BoundedServer((host,int(port)),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def close(self):
        if self.server:self.server.shutdown();self.server.server_close()
