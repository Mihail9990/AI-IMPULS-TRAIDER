"""User-click-only REAL launcher. Importing does not connect or start anything.

No scheduled startup. Stopping the process does not close broker exposure.
Existing MT5 EAs are outside this launcher's control. Mock tests only.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
from connectivity_core import ConnectionSession, ConnectionError, EXE_SHA256, public_ip, safe_path, caddy_config

# Only these fixed strings may cross the child/GUI diagnostic boundary.
DIAGNOSTICS = {
    'MAIN_BOT_ACCOUNT_BUSY': 'Основной бот или другой RECOVERY уже владеет этим счётом и символом. Запуск отменён.',
    'EA_NO_SL': 'Нет подходящей исторической сделки SL. Работу EA нельзя подтвердить без неё; торговля не запущена.',
    'EA_TIMEOUT': 'EA не ответил на проверочный запрос вовремя. Проверьте его установку и работу в выбранном MT5.',
    'EA_MISMATCH': 'Ответ EA не совпал с проверяемой сделкой SL. Доказательства не приняты.',
    'MAILBOX_ACL': 'Права доступа к mailbox или его родительским папкам не подтверждены. Защита не ослаблена.',
    'MAILBOX_PATH': 'Не подтверждён безопасный локальный путь mailbox. Проверьте Data Folder MT5.',
    'REAL_IDENTITY': 'Выбранный REAL счёт, терминал или Data Folder не совпали с текущим MT5.',
    'HISTORY_UNAVAILABLE': 'История сделок MT5 недоступна или изменилась во время проверки.',
    'MT5_INITIALIZE': 'Не удалось подключиться к выбранному терминалу MT5.',
    'CADDY_CONFIG': 'Проверка конфигурации Caddy не пройдена.',
    'HTTPS_TIMEOUT': 'HTTPS не стал готов вовремя: доверенный сертификат или доступность HTTPS не подтверждены. Причина выдачи сертификата не установлена; стратегия не включена.',
    'HTTPS_IDENTITY': 'HTTPS обслуживается неподтверждённым процессом. Ключ не передан этому процессу.',
    'LOCAL_TIMEOUT': 'Собственный native API не подтвердил готовность вовремя.',
    'CHILD_EXIT': 'Native-процесс завершился до подтверждения готовности.',
    'ARM_UNKNOWN': 'Исход включения планировщика не подтверждён. Повторной отправки не будет; запрошена остановка.',
    'STOP_WAIT': 'Безопасное завершение native ещё не подтверждено. Процесс не убит; не запускайте второй экземпляр.',
    'START_FAILED': 'Запуск остановлен из-за неподтверждённого условия. Автоматического повтора нет.',
}
MARKER = 'NATIVE_LAUNCH_DIAGNOSTIC:'
class LaunchFailure(ConnectionError):
    def __init__(self, code):
        self.code=code if code in DIAGNOSTICS else 'START_FAILED'
        super().__init__('['+self.code+'] '+DIAGNOSTICS[self.code])

def failure_code(exc):
    if str(exc)=='MAIN_BOT_ACCOUNT_BUSY':return 'MAIN_BOT_ACCOUNT_BUSY'
    if isinstance(exc,LaunchFailure): return exc.code
    # Exact allowlisted messages only: never echo arbitrary exception text.
    mapping={
        'No recent historical SL; evidence cannot be proven without it':'EA_NO_SL',
        'EA evidence timeout':'EA_TIMEOUT',
        'Invalid SL evidence':'EA_MISMATCH',
        'EA evidence preflight failed':'EA_MISMATCH',
        'EA evidence pre-arm failed':'EA_MISMATCH',
        'Mailbox and ancestor permissions not verified':'MAILBOX_ACL',
        'Mailbox reparse path rejected':'MAILBOX_PATH',
        'Missing/reparse mailbox path rejected':'MAILBOX_PATH',
        'Local existing mailbox required':'MAILBOX_PATH',
        'REAL account/session mismatch':'REAL_IDENTITY',
        'Terminal path mismatch':'REAL_IDENTITY', 'Data path mismatch':'REAL_IDENTITY',
        'Expected data path mismatch':'REAL_IDENTITY',
        'Не подтверждён указанный REAL счёт MT5.':'REAL_IDENTITY',
        'History unavailable':'HISTORY_UNAVAILABLE', 'History changed':'HISTORY_UNAVAILABLE',
        'Duplicate historical exit':'EA_MISMATCH', 'Exit side invalid':'EA_MISMATCH',
        'MT5 initialization failed':'MT5_INITIALIZE',
    }
    return mapping.get(str(exc),'START_FAILED')

def emit_failure(exc):
    code=failure_code(exc)
    print(MARKER+code,file=sys.stderr,flush=True)
    return code

WARNING = ('РЕАЛЬНЫЙ счёт: запуск включает управление ордерами. Существующие диапазоны '
           'могут продолжить работу. Остановка приложения не закрывает позиции и ордера.')

def native_caddy_config(ip, storage):
    base = caddy_config(ip, storage)
    start = base.index(' @state {')
    return base[:start] + ''' @panel {
  method GET
  path /panel/ /panel/panel.css /panel/model.js /panel/panel.js /panel/controls.js /panel/control-policy.mjs /panel/recovery-actions.mjs
 }
 @read {
  method GET
  path /api/recovery/state
 }
 @receipt {
  method GET
  path_regexp receipt ^/api/recovery/commands/[0-9a-fA-F-]{36}$
 }
 @write {
  method POST
  path /api/recovery/ranges /api/recovery/cancel /api/recovery/reconcile /api/recovery/ranges/1/close-cancel /api/recovery/ranges/2/close-cancel
 }
 handle @panel {
  reverse_proxy 127.0.0.1:8767
 }
 handle @read {
  reverse_proxy 127.0.0.1:8767
 }
 handle @receipt {
  reverse_proxy 127.0.0.1:8767
 }
 handle @write {
  reverse_proxy 127.0.0.1:8767
 }
 handle {
  respond "NOT_ALLOWED" 404
 }
}
'''

def validate_scope(terminal, account, broker_server, symbol, magic, expected_data_path=None):
    terminal = safe_path(Path(terminal).absolute())
    if not terminal.is_file() or terminal.name.lower() != 'terminal64.exe':
        raise ConnectionError('Выберите существующий terminal64.exe.')
    if type(account) is not int or account <= 0 or type(magic) is not int or magic <= 0:
        raise ConnectionError('Укажите положительные account и magic.')
    if not isinstance(broker_server,str) or not broker_server.strip() or not isinstance(symbol,str) or not symbol.strip():
        raise ConnectionError('Нужны точные сервер MT5 и символ.')
    scope=dict(terminal=str(terminal), account=account, server=broker_server, symbol=symbol, magic=magic)
    if expected_data_path is not None:
        data=safe_path(Path(expected_data_path).absolute())
        if not data.is_dir() or str(data).startswith(('\\\\','//')):raise ConnectionError('Нужна локальная подтверждённая Data Folder MT5.')
        scope['data_path']=os.path.normcase(str(data.resolve()))
    return scope

def scoped_database(server, scope, *, resume_confirmed):
    folder = safe_path(Path(server)/'native-real'); folder.mkdir(exist_ok=True)
    owner = safe_path(folder/'owner.json'); db = safe_path(folder/'state.sqlite3')
    if owner.exists():
        if json.loads(owner.read_text('utf-8')) != scope:
            raise ConnectionError('Журнал принадлежит другому терминалу/счёту/символу/magic.')
        # A failed preflight can leave only the scope marker, before any SQLite
        # database was opened. With any other entry (including sidecars or
        # unexpected files), require the existing explicit resume permission.
        if not resume_confirmed and any(entry.name != owner.name for entry in folder.iterdir()):
            raise ConnectionError('Подтвердите повторный запуск: старые диапазоны могут возобновиться.')
    else:
        if any(folder.iterdir()):
            raise ConnectionError('В native-real есть данные без подтверждённого владельца. Не перезаписываю.')
        with owner.open('x',encoding='utf-8') as f: json.dump(scope,f)
    return db.absolute()

def verify_real(api, scope):
    info = api.account_info()
    if (info is None or getattr(api,'ACCOUNT_TRADE_MODE_REAL',None) is None
        or getattr(info,'trade_mode',None) != api.ACCOUNT_TRADE_MODE_REAL
        or getattr(info,'login',None) != scope['account'] or getattr(info,'server',None) != scope['server']):
        raise ConnectionError('Не подтверждён указанный REAL счёт MT5.')

def context_id(scope):
    return hashlib.sha256(json.dumps({k:scope[k] for k in ('account','server','symbol','magic')},sort_keys=True).encode()).hexdigest()

def validate_ready(body, scope, nonce=None):
    if nonce is not None and body.get('instance') != nonce:
        raise ConnectionError('Не подтверждён собственный native-процесс.')
    w=body.get('widget',{}); snap=w.get('snapshot',{})
    if (w.get('mode')!='native' or w.get('account_trade_mode')!='real'
        or snap.get('status')!='fresh' or snap.get('context_id')!=context_id(scope)
        or snap.get('symbol')!=scope['symbol']):
        raise ConnectionError('Нет подтверждённого свежего REAL native-состояния.')
    from datetime import datetime, timezone
    try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(snap['observed_at'].replace('Z','+00:00'))).total_seconds()
    except (KeyError,TypeError,ValueError): raise ConnectionError('Нет корректного времени снимка.')
    if not 0 <= age <= 15: raise ConnectionError('Снимок MT5 устарел.')

def required_preconditions(scope):
    if not (Path(__file__).parent/"native_evidence_preflight.py").is_file():
        raise ConnectionError("REAL launch blocked: EA/mailbox verifier is missing.")
    # Actual MT5 evidence verification occurs in the isolated child before open and arm.

class NativeLaunchSession(ConnectionSession):
    """Same process/log contract as ConnectionSession; caller must explicitly confirm."""
    def __init__(self, server, assets, *, run=subprocess.run, popen=subprocess.Popen, readiness=None, prerequisites=required_preconditions):
        self.prerequisites=prerequisites
        self._native_child=None
        self._nonce=None
        self._diagnostics=[]
        super().__init__(server,assets,run=run,popen=popen,readiness=readiness or self._native_ready)
    def _native_ready(self, child, token, nonce, ip, scope, phase):
        deadline=time.monotonic()+(90 if phase=='https' else 70)
        while time.monotonic()<deadline:
            if child.poll() is not None: raise LaunchFailure('CHILD_EXIT')
            c=(http.client.HTTPSConnection(ip,443,timeout=2) if phase=='https'
               else http.client.HTTPConnection('127.0.0.1',8767,timeout=65 if phase=='arm' else 2))
            try:
                path='/api/recovery/state' if phase=='https' else '/_local_ready'
                if phase=='arm': path='/_local_arm'
                if phase=='https':
                    c.request('GET',path) # Verify this child over trusted TLS before transmitting token.
                    probe=c.getresponse();probe.read(1000001)
                    if probe.status!=401 or probe.getheader('X-Recovery-Instance')!=hashlib.sha256(nonce.encode()).hexdigest():
                        raise LaunchFailure('HTTPS_IDENTITY')
                c.request('POST' if phase=='arm' else 'GET',path,
                          headers={'Authorization':'Bearer '+token,'X-Launch-Nonce':nonce})
                r=c.getresponse(); raw=r.read(1000001)
                if phase=='https' and r.getheader('X-Recovery-Instance')!=hashlib.sha256(nonce.encode()).hexdigest():
                    raise LaunchFailure('HTTPS_IDENTITY')
                if phase!='https' and len(raw)<=1000000 and r.status==503:
                    diagnostic=json.loads(raw).get('diagnostic')
                    if diagnostic in DIAGNOSTICS:raise LaunchFailure(diagnostic)
                if r.status==200 and len(raw)<=1000000:
                    data=json.loads(raw); validate_ready(data,scope,None if phase=='https' else nonce)
                    if phase=='arm' and data.get('armed') is not True: raise ConnectionError('Планировщик не подтверждён.')
                    return
            except (OSError, ValueError, http.client.HTTPException): pass
            finally: c.close()
            # Never repeat the arm POST: outcome may already have taken effect.
            if phase=='arm': raise LaunchFailure('ARM_UNKNOWN')
            time.sleep(.2)
        raise LaunchFailure('HTTPS_TIMEOUT' if phase=='https' else 'LOCAL_TIMEOUT')
    def start(self,ip,token,storage,*,terminal,account,broker_server,symbol,magic,
              expected_data_path,owner_confirmed=False,resume_confirmed=False):
        if not owner_confirmed: raise ConnectionError(WARNING+' Требуется явное подтверждение владельца.')
        if self.children: raise ConnectionError('Уже запущено.')
        self._diagnostics=[]
        ip=public_ip(ip)
        if not isinstance(token,str) or len(token)<32 or not token.isascii() or '\r' in token or '\n' in token:
            raise ConnectionError('Недействительный ключ.')
        scope=validate_scope(terminal,account,broker_server,symbol,magic,expected_data_path)
        safe_path(storage)
        self.prerequisites(scope) # Must fail closed until production EA/mailbox verifier exists.
        exe=safe_path(self.server/'tools/caddy/caddy.exe'); python=safe_path(self.server/'.venv-native/Scripts/python.exe')
        if not python.is_file() or not exe.is_file() or hashlib.sha256(exe.read_bytes()).hexdigest()!=EXE_SHA256:
            raise ConnectionError('Не подтверждены Python и Caddy.')
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',8767))
            except OSError:raise ConnectionError('Порт 8767 занят; чужой процесс не остановлен.')
        db=scoped_database(self.server,scope,resume_confirmed=resume_confirmed)
        self.temp=tempfile.TemporaryDirectory(prefix='recovery-native-');folder=Path(self.temp.name)
        cfg=folder/'Caddyfile';cfg.write_text(native_caddy_config(ip,Path(storage)/'certificates'),encoding='utf-8')
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        try:
            for action in ('adapt','validate'):
                r=self.run([str(exe),action,'--config',str(cfg),'--adapter','caddyfile'],capture_output=True,text=True,timeout=30,creationflags=flags,shell=False)
                if r.returncode or (action=='adapt' and json.loads(r.stdout).get('admin',{}).get('disabled') is not True):
                    raise LaunchFailure('CADDY_CONFIG')
            nonce=secrets.token_hex(24)
            log=(folder/'native.log').open('w',encoding='utf-8');self.files.append(log)
            child=self.popen([str(python),'-I',str(Path(__file__).absolute()),'--child'],stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,creationflags=flags,shell=False,cwd=folder)
            self.children.append(child);self._native_child=child;self._nonce=nonce
            child.stdin.write((json.dumps(dict(scope=scope,database=str(db),server_root=str(self.server.absolute()),token=token,nonce=nonce))+'\n').encode());child.stdin.flush()
            self.readiness(child,token,nonce,ip,scope,'local')
            log=(folder/'caddy.log').open('w',encoding='utf-8');self.files.append(log)
            proxy=self.popen([str(exe),'run','--config',str(cfg),'--adapter','caddyfile'],stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=flags,shell=False,cwd=folder);self.children.append(proxy)
            self.readiness(child,token,nonce,ip,scope,'https')
            if proxy.poll() is not None:raise ConnectionError('Caddy завершился до включения стратегии.')
            self.readiness(child,token,nonce,ip,scope,'arm')
        except BaseException as exc:
            self._capture_diagnostics()
            self._remember(failure_code(exc))
            try:self.stop()
            except Exception as cleanup:
                self._remember(failure_code(cleanup))
            if not isinstance(exc,Exception):raise
            raise ConnectionError(self.logs()) from None

    def _remember(self,code):
        if code in DIAGNOSTICS and code not in self._diagnostics and len(self._diagnostics)<8:
            self._diagnostics.append(code)
    def _capture_diagnostics(self):
        if not self.temp:return
        try:
            p=Path(self.temp.name)/'native.log'
            with p.open('rb') as f:
                f.seek(max(0,p.stat().st_size-16384));raw=f.read(16384).decode('utf-8','replace')
            for line in raw.splitlines():
                if line.startswith(MARKER):self._remember(line[len(MARKER):])
        except OSError:pass
    def logs(self):
        self._capture_diagnostics()
        return '\n'.join('['+code+'] '+DIAGNOSTICS[code] for code in self._diagnostics)[:4096]

    def stop(self):
        # No terminate/kill for native: a broker request may be in flight.
        native=self._native_child
        if native is not None and native.poll() is None:
            try:
                native.stdin.write((json.dumps({'stop':self._nonce})+'\n').encode());native.stdin.flush()
                native.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                self._remember('STOP_WAIT')
                raise LaunchFailure('STOP_WAIT')
        if native in self.children:self.children.remove(native)
        self._native_child=None
        self._capture_diagnostics()
        super().stop() # Remaining process is only Caddy.

def child_main():
    """Isolated child: readiness then explicit authenticated local arm, never auto-arm."""
    runtime=None;mt5=None;listener=None
    from contextlib import ExitStack
    resources=ExitStack()
    try:
        if sys.platform!='win32':raise ConnectionError('Windows required')
        config=json.loads(sys.stdin.readline(65537));scope=config['scope'];token=config['token'];nonce=config['nonce']
        sys.path.insert(0,config['server_root'])
        from run_recovery_native import reserve_loopback_listener
        from recovery.native_preflight import TerminalBinding
        from recovery.native_runtime import NativeRuntime
        from recovery.native_api import create_native_app,native_widget_projection
        from contextlib import asynccontextmanager
        from fastapi import Header,HTTPException
        from fastapi.responses import JSONResponse
        import threading
        import hmac
        import uvicorn
        import MetaTrader5 as mt5
        listener=reserve_loopback_listener(8767)
        if not mt5.initialize(path=scope['terminal']):raise ConnectionError('MT5 initialization failed')
        verify_real(mt5,scope) # Before NativeRuntime.open or any scheduler creation.
        from main_bot_owner import main_bot_owner
        resources.enter_context(main_bot_owner(scope))
        from native_evidence_preflight import verify_native_prerequisites
        evidence=verify_native_prerequisites(mt5,terminal=scope["terminal"],account=scope["account"],server=scope["server"],symbol=scope["symbol"],magic=scope["magic"],expected_data_path=scope["data_path"])
        if evidence.get("ok") is not True or evidence.get("evidence_verified") is not True:raise ConnectionError("EA evidence preflight failed")
        from native_state_freshness import capture_anchor, StateFreshness
        state_clock=capture_anchor(mt5,scope,verify_real)
        binding=TerminalBinding(scope['account'],scope['server'],scope['symbol'],frozenset({scope['magic']}))
        runtime=NativeRuntime(mt5,binding,database=config['database'],magic=scope['magic'],token=token)
        runtime.open() # Original app lifespan NEVER run: scheduler remains stopped.
        @asynccontextmanager
        async def lifespan(app):
            try:yield
            finally:runtime.close()
        app=create_native_app(token,runtime.coordinator,lifespan=lifespan)
        armed=False
        @app.middleware('http')
        async def prevent_prearm_commands(request,call_next):
            if request.method!='GET' and request.url.path.startswith('/api/') and not armed:
                return JSONResponse({'detail':'Native launcher not armed'},status_code=503)
            response=await call_next(request)
            response.headers['X-Recovery-Instance']=hashlib.sha256(nonce.encode()).hexdigest()
            return response
        def readiness(authorization,launch_nonce):
            if not hmac.compare_digest(authorization or '', 'Bearer '+token) or not hmac.compare_digest(launch_nonce or '',nonce):raise HTTPException(401)
            verify_real(mt5,scope)
            result=dict(instance=nonce,widget=native_widget_projection(runtime.coordinator),armed=armed)
            validate_ready(result,scope,nonce)
            return result
        @app.exception_handler(Exception)
        async def safe_error(request,exc):
            return JSONResponse({'diagnostic':emit_failure(exc)},status_code=503)
        @app.get('/_local_ready')
        def ready(authorization:str=Header(default=''),x_launch_nonce:str=Header(default='')):
            return readiness(authorization,x_launch_nonce)
        @app.post('/_local_arm')
        def arm(authorization:str=Header(default=''),x_launch_nonce:str=Header(default='')):
            nonlocal armed
            with runtime.session._lock:
                result=readiness(authorization,x_launch_nonce)
                evidence_now=verify_native_prerequisites(mt5,terminal=scope["terminal"],account=scope["account"],server=scope["server"],symbol=scope["symbol"],magic=scope["magic"],expected_data_path=evidence["data_path"])
                if evidence_now.get("ok") is not True or evidence_now.get("evidence_verified") is not True:raise ConnectionError("EA evidence pre-arm failed")
                if armed:raise HTTPException(409)
                result=readiness(authorization,x_launch_nonce) # New snapshot after slow EA verification.
                runtime.scheduler.start();armed=True;result['armed']=True
                return result
        app=StateFreshness(app,state_clock,token,web_armed=lambda:armed)
        from native_web_panel import PanelAssets
        app=PanelAssets(app,Path(__file__).resolve().parent.parent/"web")
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8767,workers=1,access_log=False))
        def stop_reader():
            try:
                line=sys.stdin.readline(65537)
                if not line or hmac.compare_digest(json.loads(line).get('stop',''),nonce):server.should_exit=True
            except Exception:server.should_exit=True
        threading.Thread(target=stop_reader,daemon=True).start()
        server.run(sockets=[listener])
        return 0
    except Exception as exc:
        emit_failure(exc);return 1
    finally:
        try:
            if runtime is not None:runtime.close()
        finally:
            try:
                if mt5 is not None:mt5.shutdown()
            finally:
                try:
                    if listener is not None:listener.close()
                finally:resources.close()

if __name__=='__main__':
    if sys.argv[1:]!=['--child']:raise SystemExit('Use the explicit user-confirmed launcher.')
    raise SystemExit(child_main())
