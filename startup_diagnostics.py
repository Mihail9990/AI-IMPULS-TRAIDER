"""Bounded read-only startup probe, invoked in its own disposable Python process.
No broker controller, database, order/position commands or symbol_select are used.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime,timezone
import importlib
import json
import math
import ntpath
import os
from pathlib import Path
import socket
import time

from tradfi.startup_errors import REASONS, reason

EXTRA = {
 'BEGIN':'Проверка запуска. Это не торговый сервис.',
 'PREVIOUS_FAILURE':'Следующая строка — сохранённая причина предыдущего отказа, не результат текущей проверки.',
 'CONFIG_OK':'Структура настроек проверена; значения и секреты не выводятся.',
 'TERMINAL_FILE':'Файл terminal_path не найден или это не terminal64.exe. Проверьте путь в Блокноте.',
 'TERMINAL_OK':'Файл терминала найден.',
 'PASSWORD_OK':'Политика пароля выполнена; пароль не выводится.',
 'PORT_OK':'Локальный порт свободен на момент проверки.',
 'MT5_CONNECTING':'Подключение SDK к локальному терминалу (тайм-аут 10 секунд). Торговые команды запрещены.',
 'MT5_CONNECTED':'Соединение и каталог терминала подтверждены.',
 'ACCOUNT_OK':'Счёт/сервер совпадают с настройками; значения не записаны. Валюта UST подтверждена.',
 'NON_HEDGING':'Режим счёта не hedging. READ_ONLY допустим, реальное исполнение требует отдельной проверки.',
 'SYMBOL_OK':'XAUUSD.s найден и виден в Обзоре рынка.',
 'TICK_STALE':'Последняя котировка устарела относительно часов Windows. Рынок может быть закрыт; это не доказанная причина exit 2.',
 'TICK_FUTURE':'Время котировки впереди часов Windows. Не сдвигайте timestamp вручную.',
 'TICK_INVALID':'Нет корректного UTC времени котировки или time/time_msc расходятся.',
 'TICK_OK':'Котировка свежая относительно часов Windows. Независимый источник UTC здесь не проверялся.',
 'BARS_WAIT':'Две актуальные M1 свечи недоступны или их время не совпадает с котировкой. Подождите свежие данные.',
 'BARS_OK':'Последние M1 времена согласованы с котировкой.',
 'COMPLETE':'Проверки завершены. Это не разрешение торговли: SQLite, цикл, история сделок и внешняя UTC аттестация не проверялись.',
 'TIMEOUT':'Диагностика превысила 40 секунд. Остановлен только её дочерний процесс; торговый сервис не запускался.',
 'PROBE_FAILED':'Диагностический процесс не завершил отчёт. Его сырой вывод не сохраняется, чтобы не раскрыть секреты.',
 'RELEASE_CHANGED':'Установленная версия изменилась. Повторите диагностику после завершения обновления.',
}

class Report:
    def __init__(self,path):
        self.path=Path(path);self.lines=['Диагностика запуска TradFi — '+datetime.now(timezone.utc).isoformat()]
        self.add('INFO','BEGIN')
    @classmethod
    def resume(cls,path):
        try:old=Path(path).read_text(encoding='utf-8-sig')[:16384]
        except OSError:old=''
        obj=cls(path)
        for line in old.splitlines()[2:]:
            parts=line.split(':',1)[0].split()
            if len(parts)==2 and parts[0] in ('INFO','OK','WARN','ERROR') and parts[1] in (REASONS.keys()|EXTRA.keys()):
                obj.add(parts[0],parts[1])
        return obj
    def add(self,status,code):
        if status not in ('INFO','OK','WARN','ERROR'):status='ERROR'
        message=REASONS.get(code,EXTRA.get(code))
        if message is None:code='STARTUP_UNKNOWN';message=REASONS[code]
        self.lines.append(f'{status} {code}: {message}')
        # Fixed allowlisted text only. Bound report size; no values/exceptions interpolated.
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_text('\n'.join(self.lines[-60:])+'\n',encoding='utf-8-sig')

def port_busy(host,port):
    family=socket.AF_INET6 if host=='::1' else socket.AF_INET
    with socket.socket(family,socket.SOCK_STREAM) as s:
        s.settimeout(.5)
        return s.connect_ex((host,port))==0

def read_config(path):
    raw=Path(path).read_bytes()
    if len(raw)>65536: raise ValueError('config too large')
    return json.loads(raw.decode('utf-8-sig'))

def check_config(config,report,exists=None,environ=None):
    exists=exists or (lambda p:Path(p).is_file());environ=os.environ if environ is None else environ
    if not isinstance(config,dict):report.add('ERROR','CONFIG_JSON');return False
    if config.get('backend')!='mt5_direct_v1':report.add('ERROR','CONFIG_BACKEND');return False
    if any(not isinstance(config.get(k,False),bool) for k in ('enable_trading','legacy_ea_removed','legacy_state_reconciled')):report.add('ERROR','CONFIG_FLAGS');return False
    login=config.get('expected_login')
    if isinstance(login,bool) or not isinstance(login,int) or login<=0:report.add('ERROR','CONFIG_LOGIN');return False
    if not isinstance(config.get('expected_server'),str) or not config['expected_server'] or config.get('symbol')!='XAUUSD.s':report.add('ERROR','CONFIG_IDENTITY');return False
    magic=config.get('magic',25101102)
    if isinstance(magic,bool) or not isinstance(magic,int) or not 0<magic<2**63:report.add('ERROR','CONFIG_MAGIC');return False
    from tradfi.time_profile import validate_profile
    try:validate_profile(config)
    except Exception:report.add('ERROR','TIME_PROFILE');return False
    report.add('OK','CONFIG_OK')
    path=config.get('terminal_path')
    if not isinstance(path,str) or not ntpath.isabs(path) or ntpath.basename(path).lower()!='terminal64.exe' or not exists(path):report.add('ERROR','TERMINAL_FILE');return False
    report.add('OK','TERMINAL_OK')
    web=config.get('web',{})
    if not isinstance(web,dict) or not isinstance(web.get('enabled',True),bool):report.add('ERROR','WEB_CONFIG');return False
    if web.get('enabled',True):
        port=web.get('port',8765)
        if web.get('host','127.0.0.1') not in ('127.0.0.1','::1') or isinstance(port,bool) or not isinstance(port,int) or not 1024<=port<=65535:report.add('ERROR','WEB_CONFIG');return False
        from tradfi.web_auth import resolve_password
        try:resolve_password(web,environ)
        except Exception:report.add('ERROR','WEB_PASSWORD');return False
        report.add('OK','PASSWORD_OK')
    return True

def probe(config,report,*,api=None,busy=port_busy,clock=time.time):
    web=config.get('web',{})
    if web.get('enabled',True):
        if busy(web.get('host','127.0.0.1'),web.get('port',8765)):report.add('ERROR','PORT_BUSY');return False
        report.add('OK','PORT_OK')
    if api is None:
        try:api=importlib.import_module('MetaTrader5')
        except Exception:report.add('ERROR','MT5_PACKAGE');return False
    report.add('INFO','MT5_CONNECTING')
    try:
        if not api.initialize(config['terminal_path'],timeout=10000):report.add('ERROR','MT5_INITIALIZE');return False
        terminal=api.terminal_info();account=api.account_info()
        if terminal is None or account is None or not terminal.connected:report.add('ERROR','MT5_DISCONNECTED');return False
        if ntpath.normcase(ntpath.normpath(terminal.path))!=ntpath.normcase(ntpath.dirname(ntpath.normpath(config['terminal_path']))):report.add('ERROR','MT5_PATH_MISMATCH');return False
        report.add('OK','MT5_CONNECTED')
        if account.login!=config['expected_login'] or account.server!=config['expected_server']:report.add('ERROR','MT5_ACCOUNT_MISMATCH');return False
        if account.currency!='UST':report.add('ERROR','MT5_CURRENCY_MODE');return False
        report.add('OK','ACCOUNT_OK')
        if account.margin_mode!=api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:report.add('WARN','NON_HEDGING')
        info=api.symbol_info(config['symbol'])
        if info is None or not info.visible:report.add('ERROR','MT5_SYMBOL');return False
        report.add('OK','SYMBOL_OK')
        tick=api.symbol_info_tick(config['symbol']);now=clock()
        numeric=lambda x:isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
        if tick is None or not all(numeric(getattr(tick,k,None)) for k in ('bid','ask')) or tick.bid<=0 or tick.ask<tick.bid:report.add('ERROR','MT5_QUOTE');return False
        raw=getattr(tick,'time_msc',None);seconds=getattr(tick,'time',None)
        if not all(numeric(x) and x>0 for x in (raw,seconds)) or not 0<=raw/1000-seconds<1:report.add('WARN','TICK_INVALID')
        else:
            age=now-raw/1000
            report.add('WARN' if age>10 or age < -2 else 'OK','TICK_STALE' if age>10 else 'TICK_FUTURE' if age < -2 else 'TICK_OK')
            bars=api.copy_rates_from_pos(config['symbol'],api.TIMEFRAME_M1,0,2)
            current=int(raw/1000//60)*60
            try:valid=bars is not None and len(bars)==2 and sorted(int(b['time']) for b in bars)==[current-60,current]
            except Exception:valid=False
            report.add('OK' if valid else 'WARN','BARS_OK' if valid else 'BARS_WAIT')
        report.add('INFO','COMPLETE');return True
    except Exception as exc:
        report.add('ERROR',reason(exc));return False
    finally:
        try:api.shutdown()
        except Exception:pass

def run(root,output,*,api=None,busy=port_busy,exists=None,clock=time.time):
    report=Report(output);root=Path(root)
    from install_tradfi import lock,account_lock,state_lock
    try:
        with ExitStack() as stack:
            try:
                stack.enter_context(lock(root/'operation.lock'))
                for instance in ('live','paper'):stack.enter_context(lock(root/('instance-'+instance+'.lock')))
            except Exception:report.add('ERROR','LOCK_BUSY');return False
            previous=root/'data'/'startup-error.txt'
            try:
                with previous.open(encoding='utf-8-sig') as f:lines=f.read(4096).splitlines()
                codes=[line.removeprefix('STARTUP ') for line in lines if line.startswith('STARTUP ')]
                if len(codes)==1 and codes[0] in REASONS:
                    report.add('INFO','PREVIOUS_FAILURE');report.add('WARN',codes[0])
            except (OSError,UnicodeError):pass
            try:config=read_config(root/'data'/'service.json')
            except (json.JSONDecodeError,UnicodeError):report.add('ERROR','CONFIG_JSON');return False
            except OSError:report.add('ERROR','CONFIG_READ');return False
            if not check_config(config,report,exists):return False
            try:
                stack.enter_context(account_lock(root,config));stack.enter_context(state_lock(root,config))
            except Exception:report.add('ERROR','LOCK_BUSY');return False
            return probe(config,report,api=api,busy=busy,clock=clock)
    except Exception as exc:report.add('ERROR',reason(exc));return False

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();raise SystemExit(0 if run(args.root,args.output) else 2)
