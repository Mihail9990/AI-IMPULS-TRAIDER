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
import socket
from .web_auth import validate_password
from urllib.parse import urlsplit,parse_qs
from .control import OwnerMailbox,ActionGateway
from .chart_feed import ChartFeed


class WebTransport:
    def __init__(self,mailbox,*,authority='127.0.0.1:8765',password=None,clock=time.monotonic,public_origin=None,paper_mailbox=None,logs=None):
        if urlsplit('http://'+authority).hostname not in {'127.0.0.1','::1'}:raise ValueError('Web server must bind loopback')
        self.logs=logs
        self.mailbox=mailbox;self.paper_mailbox=paper_mailbox;self.authority=authority;self.password=password;self.clock=clock
        self.origin=public_origin or 'http://'+authority
        parsed=urlsplit(self.origin)
        if public_origin and (parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('public_origin must be an explicit HTTPS origin without path or credentials')
        self.secure=bool(public_origin)
        self.sessions={};self.attempts=[];self.lock=threading.Lock()
        if password is not None:validate_password(password,host=urlsplit('http://'+authority).hostname,public_origin=public_origin)
    def request(self,method,path,headers,body=b''):
        headers={k.lower():v for k,v in headers.items()};origin=self.origin
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
            return reply(200,{'status':'AUTHENTICATED'},'tradfi_session='+sid+'; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600'+('; Secure' if self.secure else ''))
        cookie=SimpleCookie()
        try:cookie.load(headers.get('cookie',''));sid=cookie['tradfi_session'].value
        except (KeyError,ValueError,CookieError):return reply(401,{'error':'AUTH_REQUIRED'})
        with self.lock:
            if self.sessions.get(sid,0)<=self.clock():return reply(401,{'error':'SESSION_EXPIRED'})
        if method=='GET' and route in {'/api/logs','/api/logs/download'}:
            if self.logs is None:return reply(404,{'error':'LOGS_UNAVAILABLE'})
            try:
                query=parse_qs(urlsplit(path).query,strict_parsing=True)
                if set(query)-{'scope','page'} or any(len(v)!=1 for v in query.values()):raise ValueError()
                scope=query.get('scope',['live'])[0];page=int(query.get('page',['0'])[0])
                value=self.logs.response(scope,page,download=route.endswith('/download'))
                if isinstance(value,bytes):return 200,value,{'Content-Disposition':'attachment; filename="tradfi-'+scope+'-logs.txt"'}
                return reply(200,value)
            except ValueError:return reply(400,{'error':'INVALID_LOG_REQUEST'})
        mailbox=self.paper_mailbox if route.startswith('/api/paper/') else self.mailbox
        if route.startswith('/api/paper/') and mailbox is None:return reply(404,{'error':'PAPER_UNAVAILABLE'})
        if method=='GET' and route in {'/api/state','/api/paper/state'}:return reply(200,mailbox.view())
        if method=='POST' and route=='/api/logout':
            with self.lock:self.sessions.pop(sid,None)
            return reply(200,{'status':'SIGNED_OUT'},'tradfi_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0')
        if method=='POST' and route in {'/api/action','/api/paper/action'}:
            view=mailbox.view()
            if not view.get('scope') or headers.get('x-controller-scope')!=view['scope']:return reply(409,{'error':'SCOPE_CHANGED'})
            try:return reply(202,mailbox.submit(json.loads(body)))
            except (ValueError,TypeError):return reply(400,{'error':'INVALID_ACTION'})
            except queue.Full:return reply(429,{'error':'QUEUE_FULL'})
        return reply(404,{'error':'NOT_FOUND'})


class NativePanel:
    def __init__(self,controller,*,authority='127.0.0.1:8765',password=None,clock=time.time,monotonic=time.monotonic,public_origin=None,paper_session=None):
        self.controller=controller;self.clock=clock;self.monotonic=monotonic;self.paper_session=paper_session
        self.mailbox=OwnerMailbox();self.gateway=ActionGateway(controller,execute=controller.action)
        self.feed=ChartFeed(controller.broker,clock=clock,monotonic=monotonic)
        from .web_logs import DiagnosticLogs
        self.logs=DiagnosticLogs(controller.broker.config.get('log_file','tradfi_mt5.log') if hasattr(controller.broker,'config') else None)
        self.transport=WebTransport(self.mailbox,authority=authority,password=password,clock=monotonic,public_origin=public_origin,paper_mailbox=paper_session.mailbox if paper_session else None,logs=self.logs)
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
        self.logs.capture('live',self.controller.store,self.controller.state,self.clock(),self.view())
        if self.paper_session:self.logs.capture('paper',self.paper_session.controller.store,self.paper_session.controller.state,self.clock(),self.paper_session.view())
    def time_status(self):
        # Presentation only: use cached diagnostics, never probe MT5/network or
        # replace/clear the latched execution guard while rendering a view.
        state=self.controller.state
        safety=getattr(self.controller.broker,'time_safety',None)
        verifier=getattr(safety,'semantics_verifier',None)
        current=getattr(verifier,'reason',None)
        result=getattr(safety,'result',None)
        if current is None and isinstance(result,dict) and not result.get('ok'):
            current=result.get('reason')
        return dict(latched_reason=(state.get('time_block') or {}).get('reason'),current_reason=current)

    def view(self):
        s=self.controller.state;c=s.get('cycle') or {};snap=self.snapshot or {}
        feed=self.feed.view()
        if 'basket_settings' in s:
            return self.basket_view(s,c,snap,feed)
        return dict(source=feed['source'],scope=self.scope,revision=s.get('control_revision',0),
            symbol=self.controller.broker.symbol,phase=s['phase'],mode=s['operation_mode'],
            settings=deepcopy(s['settings']),read_only=bool(getattr(self.controller.broker,'read_only',False)),
            legacy_recovery=c.get('recovery'),cycle_engine=c.get('engine'),stopped=s['stop'],
            modules=deepcopy(s['modules']),cycle_id=c.get('id'),recovery=deepcopy(c.get('fixed_recovery') or (s.get('fixed_recovery_carry') or {}).get('fixed_recovery')),
            time_block=deepcopy(s.get('time_block')),time_status=self.time_status(),feed=feed,quote=feed.get('quote'),timestamp=feed.get('timestamp'),
            orders=deepcopy(snap.get('orders',[])),positions=deepcopy(snap.get('positions',[])),
            journal=[n['text'] for n in s['notifications'][-30:]],
            action_results={key:{'status':r['status'],'revision':r['revision'],'reason':r.get('reason')} for key,r in s.get('control_receipts',{}).items()})
    def basket_view(self,s,c,snap,feed):
        projection=None
        try:
            key=(str(snap.get('positions',[])),c.get('recovery'),str(c.get('cfg')))
            if getattr(self,'_basket_projection_key',None)!=key:
                from decimal import Decimal as D
                ps=snap.get('positions',[])
                gross=sum((self.controller.broker.profit(p['side'],D(p['size']),D(p['entry']),D(p['tp'])) for p in ps),D(0))
                debt=D(c.get('recovery') or s.get('basket_carry','0'));reserve=D(0) if 'tp_distances' in s else D(c.get('cfg',s['basket_settings'])['COST_RESERVE'])
                self._basket_projection=dict(gross_tp=str(gross),cost_reserve=str(reserve),shortfall=str(max(D(0),debt+reserve-gross)))
                self._basket_projection_key=key
            projection=self._basket_projection
        except (ValueError,OSError,TimeoutError):pass
        return dict(**(self.controller.v3_view(snap) if hasattr(self.controller,'v3_view') else {}),contract='ticket_basket_v1',source=feed['source'],scope=self.scope,revision=s.get('control_revision',0),symbol=self.controller.broker.symbol,
            phase=s['phase'],read_only=bool(getattr(self.controller.broker,'read_only',False)),stopped=s['stop'],settings=deepcopy(s['basket_settings']),
            reverse_controls=self.controller.reverse_controls(snap) if hasattr(self.controller,'reverse_controls') else dict(can_enable=False,can_disable=False,reason='Нет поддержки обратной лестницы'),reverse_ladders=[dict(group=g['id'],**deepcopy(g['reverse_ladder'])) for g in c.get('groups',[]) if g.get('reverse_ladder') and not g.get('retired')],manual_close=deepcopy(c.get('manual_close')),range_override=deepcopy(c.get('range_override')),manual_close_settings=deepcopy(s.get('manual_close_settings',dict(mode='from_close',duration='once',same_distance='1',opposite_distance='2'))),tp_distances=deepcopy(s.get('tp_distances')),active_tp_distances=deepcopy(c.get('tp_distances')),range_policy=c.get('range_policy'),active_settings=deepcopy(c.get('cfg')),cycle_id=c.get('id'),debt=c.get('recovery',s.get('basket_carry','0')),realized=c.get('realized','0'),
            main_range=deepcopy(c.get('main_range')),frozen_levels=deepcopy(c.get('expanded')),levels=self.controller.levels(),edit=deepcopy(c.get('edit')),projection=projection,
            recovery_writeoffs=deepcopy(s.get('recovery_writeoffs',[])[-20:]),migration_block=s.get('migration_block'),manual_reason=c.get('manual_reason'),time_block=deepcopy(s.get('time_block')),time_status=self.time_status(),feed=feed,
            tickets=deepcopy(list(c.get('tickets',{}).values())),orders=deepcopy(snap.get('orders',[])),positions=deepcopy(snap.get('positions',[])),
            journal=[n['text'] for n in s['notifications'][-50:]],
            action_results={key:{'status':r['status'],'revision':r['revision'],'reason':r.get('reason')} for key,r in s.get('control_receipts',{}).items()})

    def start(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from .local_panel import ROOT
        if 'basket_settings' in self.controller.state:ROOT=ROOT.parent/'basket-panel'
        transport=self.transport;authority=self.authority
        class Handler(BaseHTTPRequestHandler):
            def setup(self):super().setup();self.connection.settimeout(2)
            def do_GET(self):self.handle_call()
            def do_POST(self):self.handle_call()
            def handle_call(self):
                route=urlsplit(self.path).path
                static={'/':('index.html','text/html'),'/panel.js':('panel.js','text/javascript'),'/panel.css':('panel.css','text/css')}
                if self.command=='GET' and route in static and self.headers.get('Host')==authority:
                    filename,mime=static[route];body=(ROOT/filename).read_bytes()
                    if filename=='index.html':body=body.replace(b'<body>',b'<body data-controller="native">')
                    self.reply(200,body,mime,{});return
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:size=-1
                if not 0<=size<=8192:self.reply(413,b'{}','application/json',{});return
                code,data,extra=transport.request(self.command,self.path,dict(self.headers),self.rfile.read(size))
                self.reply(code,data if isinstance(data,bytes) else json.dumps(data,allow_nan=False).encode(),'text/plain; charset=utf-8' if isinstance(data,bytes) else 'application/json',extra)
            def reply(self,code,body,mime,extra):
                self.send_response(code)
                for k,v in dict({'Content-Type':mime,'Content-Length':str(len(body)),'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"frame-ancestors 'none'"},**extra).items():self.send_header(k,v)
                self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        class BoundedServer(ThreadingHTTPServer):
            address_family=socket.AF_INET6 if urlsplit('http://'+authority).hostname=='::1' else socket.AF_INET
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
        endpoint=urlsplit('http://'+authority);host,port=endpoint.hostname,endpoint.port
        self.server=BoundedServer((host,int(port)),Handler)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
    def close(self):
        self.logs.close()
        if self.server:self.server.shutdown();self.server.server_close()
