"""Optional XAUUSDT-only service; reuses authenticated main-screen UI."""
from pathlib import Path
from types import SimpleNamespace
from contextlib import ExitStack
import os
import time
from .control import ActionRejected, ActionGateway, OwnerMailbox
from .demo_test import DemoSession
from .web_control import NativePanel, WebTransport


def strategy_flat(controller):
    s=controller.state
    if not s.get('stop') or s.get('cycle') or s.get('migration_block') or s.get('phase') not in {'IDLE','STOPPED'}:return False
    if any(c.get('status') not in {'DONE','REJECTED','CANCELLED','SKIPPED'} for c in s.get('commands',{}).values()):return False
    if any(r.get('status')=='UNKNOWN' for r in s.get('control_receipts',{}).values()):return False
    try:
        snap=controller.snapshot()
        return not snap['positions'] and not snap['orders'] and not snap.get('evidence_blocked',False)
    except Exception:return False


class DemoPanel(NativePanel):
    """No live controller/DB is loaded in the dedicated demo service."""
    def __init__(self,demo,*,authority,password,public_origin=None):
        self.demo_session=demo;self.authority=authority;self.server=None
        self.controller=SimpleNamespace(state={'basket_settings':{}})
        from .web_logs import DiagnosticLogs
        self.logs=DiagnosticLogs(str(demo.store.path)+'.log')
        self.mailbox=OwnerMailbox();self.transport=WebTransport(self.mailbox,authority=authority,password=password,public_origin=public_origin,demo_mailbox=demo.mailbox,logs=self.logs)
    def step(self):
        self.demo_session.step()
        self.logs.capture('demo',self.demo_session.store,self.demo_session.state,self.demo_session.clock(),self.demo_session.view())
        while not self.mailbox.requests.empty():
            req=self.mailbox.requests.get_nowait();self.mailbox.outcomes[req['id']]={'status':'REJECTED'};self.mailbox.requests.task_done()
        view=dict(contract='ticket_basket_v1',scope='demo-only-no-strategy',revision=0,source='NONE',symbol='XAUUSD.s',phase='DEMO ONLY',read_only=True,stopped=True,settings={},cycle_id=None,debt='0',realized='0',levels={},positions=[],orders=[],journal=['Основная стратегия и симулятор не запущены. Используйте секцию DEMO XAUUSDT.'],feed=dict(status='DISABLED',candles=[],quote=None),action_results=dict(self.mailbox.outcomes))
        with self.mailbox.lock:self.mailbox._view=view


def run_demo_service(config,*,api=None,stop_event=None,clock=time.time,monotonic=time.monotonic,panel_factory=DemoPanel):
    from .runtime import owner_lock,account_lock_path
    path=config.get('terminal_path');server=config.get('expected_server');login=config.get('expected_login')
    if not path or not server or type(login) is not int or login<=0:raise ValueError('Explicit terminal/account binding required')
    if config.get('symbol')!='XAUUSDT':raise ValueError('Dedicated demo config requires exact XAUUSDT')
    if config.get('enable_trading') is True:raise ValueError('Demo service cannot enable strategy trading')
    web=config.get('web',{});port=web.get('port',8765)
    if web.get('host','127.0.0.1')!='127.0.0.1' or type(port) is not int or not 1024<=port<=65535:raise ValueError('Loopback web binding required')
    password=os.environ.get('TRADFI_WEB_PASSWORD','')
    if len(password)<32:raise ValueError('Strong web password required')
    state=Path(config.get('demo_state_file','tradfi_xauusdt_demo.sqlite3')).resolve();state.parent.mkdir(parents=True,exist_ok=True)
    with ExitStack() as resources:
        resources.enter_context(owner_lock(str(state)+'.lock'))
        # Shared account lock with normal service's diagnostic owner.
        resources.enter_context(owner_lock(account_lock_path(config)))
        if api is None:import MetaTrader5 as api
        if not api.initialize(path):raise OSError('MT5 connection unavailable')
        resources.callback(api.shutdown)
        demo=DemoSession(state,api,expected_login=login,expected_server=server,live_safe=lambda:True,clock=clock,monotonic=monotonic)
        resources.callback(demo.close)
        panel=panel_factory(demo,authority='127.0.0.1:'+str(port),password=password,public_origin=web.get('public_origin'))
        resources.callback(panel.close);panel.start()
        while stop_event is None or not stop_event.is_set():
            panel.step()
            if stop_event is None:time.sleep(.2)
            else:stop_event.wait(.2)
