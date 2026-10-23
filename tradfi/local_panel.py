"""Loopback-only offline demonstration. No MT5, Telegram worker or credentials.

The owner loop runs independently of browser requests. All API actions use the
same InteractiveController.action entry point as /action Telegram messages.
"""
from copy import deepcopy
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from .control import ActionGateway, Conflict, OwnerMailbox
from .interactive import InteractiveController
from .interactive_simulator import InteractiveSimulator
from .strategies.fixed_recovery import RecoveryConfig

ROOT=Path(__file__).resolve().parents[1]/'web'/'local-panel'


class LocalPanel:
    def __init__(self, controller, *, clock=time.time):
        if not isinstance(controller.broker,InteractiveSimulator):
            raise ValueError('Local panel is exclusively a mock sandbox')
        self.controller=controller
        self.gateway=ActionGateway(controller,execute=controller.action)
        self.clock=clock
        self.last_tick=None

    def step(self):
        # A fixture heartbeat, explicitly not a real market tick.
        self.controller.broker.data['now']=self.clock()
        self.controller.tick()
        self.last_tick=self.clock()

    def view(self):
        s=self.controller.state;c=s.get('cycle') or {};b=self.controller.snapshot()
        return dict(source='MOCK',symbol='XAUUSD.s',revision=s.get('control_revision',0),
            action_results={key:{'status':value['status'],'revision':value['revision']} for key,value in s.get('control_receipts',{}).items()},
            phase=s['phase'],mode=s['operation_mode'],modules=deepcopy(s['modules']),
            recovery=deepcopy(c.get('fixed_recovery')),cycle_id=c.get('id'),
            time_block=deepcopy(s.get('time_block')),quote=deepcopy(b['quote']),
            timestamp=b['quote'].get('observed_at_utc'),age=None if b['quote'].get('observed_at_utc') is None else max(0,self.clock()-b['quote']['observed_at_utc']),
            positions=deepcopy(b['positions']),orders=deepcopy(b['orders']),
            journal=[n['text'] for n in s['notifications'][-30:]])

    def request(self,method,path,headers,body=b'',*,authority='127.0.0.1:8765'):
        """Pure request handler, tested with no sockets or real network."""
        headers={k.lower():v for k,v in headers.items()}
        origin='http://'+authority
        if headers.get('host')!=authority:return 403,{'error':'Loopback host required'}
        if headers.get('origin',origin)!=origin:return 403,{'error':'Cross-origin denied'}
        if headers.get('sec-fetch-site','same-origin') not in {'same-origin','none'}:return 403,{'error':'Cross-site denied'}
        route=urlsplit(path).path
        if route.startswith('/api/'):
            if headers.get('x-local-mock')!='1':return 403,{'error':'Explicit local mock request required'}
            if method=='GET' and route=='/api/state':return 200,self.view()
            if method=='POST' and route=='/api/action':
                if headers.get('content-type')!='application/json' or len(body)>8192:return 400,{'error':'Bounded JSON required'}
                try:result=self.gateway.apply(json.loads(body))
                except Conflict:return 409,{'error':'State or request changed; refresh preview'}
                except (ValueError,UnicodeDecodeError):return 400,{'error':'Invalid action'}
                return 200,result
            return 404,{'error':'Unknown endpoint'}
        return 404,{'error':'Unknown endpoint'}


class MockTransport:
    """No controller/store access on the HTTP worker."""
    def __init__(self,mailbox):self.mailbox=mailbox

    def request(self,method,path,headers,body=b''):
        import queue
        headers={k.lower():v for k,v in headers.items()}
        if headers.get('host')!='127.0.0.1:8765':return 403,{'error':'Loopback host required'}
        if headers.get('origin','http://127.0.0.1:8765')!='http://127.0.0.1:8765':return 403,{'error':'Cross-origin denied'}
        if headers.get('sec-fetch-site','same-origin') not in {'same-origin','none'}:return 403,{'error':'Cross-site denied'}
        if headers.get('x-local-mock')!='1':return 403,{'error':'Explicit mock required'}
        route=urlsplit(path).path
        if method=='GET' and route=='/api/state':return 200,self.mailbox.view()
        if method=='POST' and route=='/api/action':
            if headers.get('content-type')!='application/json' or len(body)>8192:return 400,{'error':'Bounded JSON required'}
            try:return 202,self.mailbox.submit(json.loads(body))
            except (ValueError,UnicodeDecodeError):return 400,{'error':'Invalid action'}
            except queue.Full:return 429,{'error':'Queue full; no action accepted'}
        return 404,{'error':'Unknown endpoint'}


def main():
    """Fresh temporary demo each process. Never opens an existing trade store."""
    import math
    import tempfile
    import threading
    from http.server import BaseHTTPRequestHandler,HTTPServer
    from .storage import Store
    with tempfile.TemporaryDirectory(prefix='ai-impuls-mock-') as folder:
        store=Store(Path(folder)/'mock.sqlite3')
        broker=InteractiveSimulator()
        controller=InteractiveController(store,broker)
        controller.configure('fixed',RecoveryConfig().dump())
        controller.command('/start semi')
        broker.quote('4135.9','4136')
        controller.triggers('prices',{'BUY':'4139','SELL':'4133'})
        app=LocalPanel(controller);mailbox=OwnerMailbox();transport=MockTransport(mailbox)
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup();self.connection.settimeout(.5)
            def do_GET(self):
                static={'/':('index.html','text/html'),'/panel.js':('panel.js','text/javascript'),'/panel.css':('panel.css','text/css')}
                route=urlsplit(self.path).path
                if self.headers.get('Host')=='127.0.0.1:8765' and route in static:
                    filename,mime=static[route]
                    self.reply(200,(ROOT/filename).read_bytes(),mime);return
                code,data=transport.request('GET',self.path,dict(self.headers));self.reply(code,json.dumps(data).encode(),'application/json')
            def do_POST(self):
                try:length=int(self.headers.get('Content-Length','-1'))
                except ValueError:length=-1
                if not 0<=length<=8192:self.reply(413,b'{}','application/json');return
                code,data=transport.request('POST',self.path,dict(self.headers),self.rfile.read(length))
                self.reply(code,json.dumps(data).encode(),'application/json')
            def reply(self,code,body,mime):
                self.send_response(code);self.send_header('Content-Type',mime)
                self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Content-Security-Policy',"frame-ancestors 'none'")
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=HTTPServer(('127.0.0.1',8765),Handler)
        # Only this transport thread can block on a browser socket.
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        print('LOCAL MOCK ONLY: http://127.0.0.1:8765/?controller=mock',flush=True)
        deadline=0;mock_tick=0
        try:
            while True:
                if time.monotonic()>=deadline:
                    mock_tick+=1
                    bid=round(4135.9+math.sin(mock_tick*.25)*.08,2)
                    broker.quote(str(bid),str(round(bid+.1,2)))
                    app.step();deadline=time.monotonic()+1
                mailbox.drain(app.gateway,app.view)
                time.sleep(.05)
        except KeyboardInterrupt:pass
        finally:
            # Daemon worker may still be handling a slow client; owner never joins it.
            server.server_close();store.close()


if __name__=='__main__':main()
