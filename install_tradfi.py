"""Standalone Windows Python 3.13 GUI installer. Only the standard library is needed."""
from contextlib import closing, contextmanager, nullcontext, ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile

REPO = 'Mihail9990/AI-IMPULS-TRAIDER'
CONTRACT = {'format': 1, 'backend': 'mt5_direct_v1', 'state_schema': 1,
            'ledger_contract': 'deal_ledger_v1', 'scope_contract': 'cycle_scope_v1', 'python': '3.13'}
REQUIREMENTS = ['MetaTrader5==5.0.6231; sys_platform == "win32"', 'requests>=2.31,<3']
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_EXPANDED = 96 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{40}')

class SetupError(Exception):
    pass


def read_json(path):
    with Path(path).open(encoding='utf-8-sig') as f:
        raw=f.read(65537)
    if len(raw)>65536:
        raise SetupError('Слишком большой файл настроек.')
    return json.loads(raw)


def atomic_json(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with tmp.open('x',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def lock(path):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    f=path.open('a+b'); acquired=False
    try:
        if os.name=='nt':
            import msvcrt
            if path.stat().st_size==0: f.write(b'0'); f.flush()
            f.seek(0)
            try: msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
            except OSError: raise SetupError('Другой сервис или установщик уже работает.') from None
        else:
            import fcntl
            try: fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError: raise SetupError('Другой сервис или установщик уже работает.') from None
        acquired=True
        yield
    finally:
        if acquired and os.name=='nt':
            import msvcrt
            f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        f.close()


def download(url, limit=MAX_DOWNLOAD):
    req=urllib.request.Request(url,headers={'User-Agent':'TradFi-Windows-Installer','Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(req,timeout=30) as r:
        from urllib.parse import urlsplit
        end=urlsplit(r.url)
        if end.scheme!='https' or end.hostname not in {'api.github.com','github.com','codeload.github.com'}:
            raise SetupError('Неожиданный источник загрузки.')
        if int(r.headers.get('Content-Length','0'))>limit:
            raise SetupError('Архив слишком большой.')
        data=bytearray(); deadline=time.monotonic()+120
        while True:
            chunk=r.read(min(65536,limit+1-len(data)))
            if not chunk: return bytes(data)
            data.extend(chunk)
            if len(data)>limit or time.monotonic()>deadline:
                raise SetupError('Превышен лимит загрузки.')


def latest_sha(fetch=download):
    data=json.loads(fetch(f'https://api.github.com/repos/{REPO}/git/ref/heads/work',65536))
    if not isinstance(data,dict) or data.get('ref')!='refs/heads/work' or not isinstance(data.get('object'),dict) or data['object'].get('type')!='commit':
        raise SetupError('GitHub не вернул ссылку на ветку work.')
    sha=data['object'].get('sha','')
    if not isinstance(sha,str) or not SHA.fullmatch(sha):
        raise SetupError('GitHub не вернул корректный SHA ветки work.')
    return sha


def extract_archive(blob, destination, sha):
    """Validate the entire central directory before writing any member."""
    if len(blob)>MAX_DOWNLOAD: raise SetupError('Архив слишком большой.')
    root=f'AI-IMPULS-TRAIDER-{sha}'
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        infos=z.infolist(); total=0; seen=set(); planned=[]
        if len(infos)>3000: raise SetupError('Слишком много файлов в архиве.')
        for info in infos:
            raw=info.filename
            if '\\' in raw or '\x00' in raw: raise SetupError('Недопустимый путь в архиве.')
            parts=raw.rstrip('/').split('/')
            if not parts or parts[0]!=root or any(p in ('','.', '..') for p in parts):
                raise SetupError('Недопустимый путь в архиве.')
            for p in parts:
                if ':' in p or p.endswith((' ','.')) or p.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
                    raise SetupError('Недопустимое имя Windows в архиве.')
            mode=(info.external_attr>>16)&0xffff
            if stat.S_IFMT(mode) not in (0,stat.S_IFREG,stat.S_IFDIR) or info.flag_bits&1:
                raise SetupError('Ссылки и специальные файлы запрещены.')
            key='/'.join(parts).casefold()
            if key in seen: raise SetupError('Повторяющийся путь в архиве.')
            seen.add(key); total+=info.file_size
            if total>MAX_EXPANDED or info.file_size>8*1024*1024 or info.file_size>max(info.compress_size,1)*200:
                raise SetupError('Превышены лимиты распаковки.')
            if len(parts)>1: planned.append((info,Path(destination).joinpath(*parts[1:])))
        for info,path in planned:
            if info.is_dir(): path.mkdir(parents=True,exist_ok=True)
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                with z.open(info) as src,path.open('xb') as dst: shutil.copyfileobj(src,dst,65536)


def validate_release(code):
    if not (code/'managed_release.json').is_file():
        raise SetupError('Эта версия work ещё не поддерживает установщик. Дождитесь публикации совместимой версии.')
    if read_json(code/'managed_release.json')!=CONTRACT:
        raise SetupError('Версия несовместима с этим установщиком. Скачайте новый установщик.')
    lines=[x.strip() for x in (code/'requirements-tradfi-mt5.txt').read_text().splitlines() if x.strip() and not x.lstrip().startswith('#')]
    if lines!=REQUIREMENTS:
        raise SetupError('Зависимости изменились: требуется проверенный новый установщик.')
    for name in ('tradfi_main.py','tradfi/runtime.py','tradfi/managed.py','tradfi_config.example.json'):
        if not (code/name).is_file(): raise SetupError('Архив неполный.')


def validate_instance(instance):
    if instance not in {'live','paper'}: raise SetupError('Неизвестный экземпляр сервиса.')
    return instance


def config_path(root, instance='live'):
    validate_instance(instance)
    return Path(root)/'data'/('service.json' if instance=='live' else 'paper-service.json')


def binding_path(root, instance='live'):
    validate_instance(instance)
    return Path(root)/'data'/('state-binding.json' if instance=='live' else 'paper-state-binding.json')


def default_paper_config(root, live):
    # Allowlist only market-source identity, never copy passwords, tokens or credentials.
    config={key:live[key] for key in ('terminal_path','time_profile','market_time_offset_seconds','expected_server','expected_login','symbol','magic') if key in live}
    live_state=state_path(root,live)
    config.update(backend='paper_mt5_quotes_v1',enable_trading=False,
        live_state_file=str(live_state),state_file=str(live_state.with_name(live_state.stem+'.paper.sqlite3')),
        log_file=str(Path(root)/'data'/'paper.log'),
        web={'enabled':True,'host':'127.0.0.1','port':8766,'password':''})
    return config


def state_path(root,config):
    p=Path(config.get('state_file','tradfi_mt5_state.sqlite3'))
    return p.resolve() if p.is_absolute() else (root/'data'/p).resolve()


def inspect_state(root, config, instance="live"):
    p=state_path(root,config); binding=binding_path(root,instance)
    if binding.exists():
        if read_json(binding)!={'path':str(p)} or not p.is_file():
            raise SetupError('Путь сохранённой SQLite изменён или файл потерян. Автосброс запрещён.')
    if not p.exists(): return
    try:
        db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise ValueError()
            row=db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
            if row is None: raise ValueError()
            s=json.loads(row[0])
            if s.get('schema')!=1 or s.get('backend')!=('mt5_direct_v1' if instance=='live' else 'paper_mt5_quotes_v1') : raise ValueError()
            if instance=='live' and s.get('ledger_contract')!='deal_ledger_v1':raise ValueError()
            if instance=='live' and s.get('commands') and s.get('scope_contract')!='cycle_scope_v1': raise ValueError()
            if instance=='live' and (s.get('cycle') or s.get('phase') not in {'IDLE','PAUSED','WAIT_START'}): raise ValueError()
            if instance=='paper':
                paper=json.loads(db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
                if not isinstance(paper.get('identity'),str) or not paper['identity'].startswith('PAPER-'):raise ValueError()
                if not all(isinstance(paper.get(key),list) for key in ('orders','positions','deals','order_history')):raise ValueError()
                if not isinstance(paper.get('paused_gap'),bool):raise ValueError()
                if s.get('ledger_contract') is None:
                    if s.get('seen_deals'):raise ValueError()
                    return
                if s.get('ledger_contract')!='deal_ledger_v1':raise ValueError()
            if instance=='live':
                if any(v.get('status') not in {'DONE','REJECTED','CANCELLED','FILLED','OBSOLETE'} for v in s.get('commands',{}).values()): raise ValueError()
                journal=db.execute('SELECT id FROM mt5_commands').fetchall()
                if any(cid not in s.get('commands',{}) for (cid,) in journal): raise ValueError()
            tickets=[r[0] for r in db.execute('SELECT ticket FROM consumed_deals')]
            seen=s.get('seen_deals',[])
            if len(seen)!=len(set(seen)) or len(tickets)!=len(set(tickets)) or set(tickets)!=set(seen): raise ValueError()
        finally: db.close()
    except Exception:
        raise SetupError('Состояние активно, неизвестно или несовместимо. Обновление/rollback заблокировано; SQLite сохранена.') from None


def account_lock(root, config):
    identity=[config.get('expected_server'),config.get('expected_login'),config.get('symbol')]
    if not all(identity): return nullcontext()
    digest=hashlib.sha256(json.dumps(identity,ensure_ascii=True).encode('ascii')).hexdigest()
    base=Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home()/'.local'/'state'
    return lock(base/'AI IMPULS TradFi'/'locks'/(digest+'.lock'))


def state_lock(root,config):
    p=state_path(root,config)
    return lock(str(p)+'.lock') if p.exists() else nullcontext()


class Manager:
    def __init__(self,root,fetch=download,runner=subprocess.run,report=print):
        self.root=Path(root).resolve(); self.fetch=fetch; self.runner=runner; self.report=report
    @property
    def config_path(self): return self.path_for('live')
    def path_for(self,instance='live'): return config_path(self.root,instance)
    def config(self,instance='live'):
        path=self.path_for(instance)
        if path.exists():return read_json(path)
        if instance=='paper':
            if binding_path(self.root,'paper').exists():raise SetupError('Конфигурация PAPER потеряна; автоматический сброс запрещён.')
            return default_paper_config(self.root,self.config())
        return {}
    def prepare_paper(self):
        if not self.path_for('paper').exists():atomic_json(self.path_for('paper'),self.config('paper'))
    @contextmanager
    def maintenance_guard(self):
        with ExitStack() as stack:
            for name in ('operation.lock','instance-live.lock','instance-paper.lock'):
                stack.enter_context(lock(self.root/name))
            live=self.config();paper=self.config('paper')
            if state_path(self.root,live)==state_path(self.root,paper):raise SetupError('Базы LIVE и PAPER должны различаться.')
            stack.enter_context(account_lock(self.root,live))
            for config in (live,paper):stack.enter_context(state_lock(self.root,config))
            yield
    def inspect_states(self):
        for instance in ('live','paper'):inspect_state(self.root,self.config(instance),instance)
    def pointer(self):
        path=self.root/'current.json'
        value=read_json(path) if path.exists() else {'current':None,'previous':None}
        if set(value)!={'current','previous'} or any(x is not None and (not isinstance(x,str) or not SHA.fullmatch(x)) for x in value.values()):
            raise SetupError('Повреждён указатель версии; автоматический сброс запрещён.')
        return value
    def version(self,sha): return self.root/'releases'/sha
    def initialize(self,code):
        marker=self.root/'managed-install.json'
        if not marker.exists():
            if (self.root/'data').exists() and any((self.root/'data').iterdir()):
                raise SetupError('Найдены неизвестные данные. Автоматическое присвоение запрещено.')
            atomic_json(marker,{'format':1})
        elif read_json(marker)!={'format':1}: raise SetupError('Неизвестный формат установки.')
        if not self.config_path.exists():
            if self.pointer()['current']: raise SetupError('Конфигурация потеряна; не создаём замену.')
            config=read_json(code/'tradfi_config.example.json')
            config.update(enable_trading=False,legacy_ea_removed=False,legacy_state_reconciled=False,
                state_file=str(self.root/'data'/'state.sqlite3'),log_file=str(self.root/'data'/'tradfi.log'))
            atomic_json(self.config_path,config)
        self.prepare_paper()
    def prepare_python(self,release):
        env=dict(os.environ)
        for k in list(env):
            if k.startswith(('PIP_','PYTHON','TRADFI_')): env.pop(k)
        env['PIP_CONFIG_FILE']=os.devnull
        flags={'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)} if os.name=='nt' else {}
        def run(args):
            result=self.runner(args,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=600,**flags)
            if result.returncode: raise SetupError('Не удалось установить зависимости. Предыдущая версия сохранена.')
        run([sys.executable,'-I','-m','venv',str(release/'venv')])
        python=release/'venv'/'Scripts'/'python.exe'
        run([str(python),'-I','-m','pip','install','--index-url','https://pypi.org/simple','--only-binary=:all:',
             '--disable-pip-version-check','--no-input','-r',str(release/'code'/'requirements-tradfi-mt5.txt')])
        run([str(python),'-I','-c','import MetaTrader5, requests'])
    def install(self):
        with self.maintenance_guard():
            old=self.pointer()
            if old['current'] and not self.config_path.is_file(): raise SetupError('Конфигурация потеряна.')
            self.inspect_states()
            self.backup()
            sha=latest_sha(self.fetch); self.report(f'{REPO}\nwork → {sha}')
            if sha==old['current']:
                self.current_release(); self.report('Уже установлена актуальная версия.'); return
            release=self.version(sha)
            if release.exists():
                validate_release(release/'code')
                if read_json(release/'ready.json')!={'sha':sha,'contract':CONTRACT}:
                    raise SetupError('Папка версии неполная. Проверьте её вручную; ничего не удалено.')
                self.verify_prepared(release)
                self.initialize(release/'code')
                atomic_json(self.root/'current.json',{'current':sha,'previous':old['current']})
                self.report('Переключено на уже установленную проверенную версию.')
                return
            self.root.joinpath('releases').mkdir(parents=True,exist_ok=True)
            stage=Path(tempfile.mkdtemp(prefix='.download-',dir=self.root/'releases'))
            created=False
            try:
                blob=self.fetch(f'https://github.com/{REPO}/archive/{sha}.zip')
                extract_archive(blob,stage/'code',sha); validate_release(stage/'code')
                # venv scripts embed absolute paths: build only at final immutable location.
                stage.rename(release); created=True
                self.prepare_python(release)
                self.verify_prepared(release)
                self.initialize(release/'code')
                atomic_json(release/'ready.json',{'sha':sha,'contract':CONTRACT})
                atomic_json(self.root/'current.json',{'current':sha,'previous':old['current']})
            except BaseException:
                if created:
                    try:referenced=sha in self.pointer().values()
                    except Exception:referenced=True  # ambiguous pointer outcome: preserve code in place
                    if not referenced:
                        failed=self.root/'failed';failed.mkdir(exist_ok=True)
                        release.rename(failed/(sha+'-'+uuid.uuid4().hex))
                raise
            finally: shutil.rmtree(stage,ignore_errors=True)
            self.report('Установка завершена. Настройки и база — в папке data. Торговля не включалась.')
    def verify_prepared(self,release):
        pass

    def backup(self):
        """Local consistent SQLite copy; secrets/configs stay in place, never imported."""
        old=self.pointer()
        if not old['current']:return None
        target=self.root/'backups'/('before-update-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex)
        target.mkdir(parents=True)
        states={}
        for instance in ('live','paper'):
            p=state_path(self.root,self.config(instance))
            filename='state.sqlite3' if instance=='live' else 'paper-state.sqlite3'
            states[instance]={'source':str(p),'backup':filename,'copied':p.exists()}
            if p.exists():
                src=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
                try:
                    with closing(sqlite3.connect(target/filename)) as dst:
                        deadline=time.monotonic()+45
                        def progress(*_):
                            if time.monotonic()>deadline:raise SetupError('Резервное копирование превысило время; версия не переключена.')
                        src.backup(dst,pages=256,progress=progress,sleep=.05)
                        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise SetupError('Проверка резервной SQLite не пройдена.')
                finally:src.close()
        atomic_json(target/'manifest.json',dict(format=2,current=old['current'],previous=old['previous'],
            code_retained=str(self.version(old['current'])),state_copied=states['live']['copied'],states=states,
            credentials_copied=False,configuration_copied=False))
        self.report('Резервная SQLite: '+str(target)+'. Прежний код сохранён. Конфигурация и секреты остаются на месте.')
        return target

    def current_release(self):
        sha=self.pointer()['current']
        if not sha: raise SetupError('Сначала установите код.')
        release=self.version(sha)
        if read_json(release/'ready.json')!={'sha':sha,'contract':CONTRACT}: raise SetupError('Версия не подготовлена.')
        validate_release(release/'code')
        return release
    def rollback(self):
        with self.maintenance_guard():
            if not self.config_path.is_file(): raise SetupError('Конфигурация потеряна; rollback заблокирован.')
            pointer=self.pointer(); sha=pointer['previous']
            if not sha: raise SetupError('Предыдущей версии нет.')
            self.inspect_states()
            self.backup()
            release=self.version(sha); validate_release(release/'code')
            if read_json(release/'ready.json')!={'sha':sha,'contract':CONTRACT}: raise SetupError('Предыдущая версия неполная.')
            atomic_json(self.root/'current.json',{'current':sha,'previous':pointer['current']})
            self.report('Переключён только код. SQLite, конфигурация и логи не откатывались.')
    def prepare_settings(self,instance='live'):
        if instance=='paper':self.prepare_paper()
        config=self.config(instance)
        web=config.setdefault('web',{})
        if not isinstance(web,dict):raise SetupError('web должен быть объектом настроек.')
        if 'password' not in web:
            web['password']=''
            atomic_json(self.path_for(instance),config)
    def diagnose(self):
        # A separate, bounded, read-only probe. No trading-service launch or account login.
        release=self.current_release()
        script=Path(__file__).resolve().parent/'startup_diagnostics.py'
        if not script.is_file(): raise SetupError('Нужен полный новый архив со startup_diagnostics.py рядом с установщиком.')
        output=self.root/'data'/'startup-diagnostic.txt'
        from startup_diagnostics import Report
        report=Report(output)
        env=dict(os.environ)
        for key in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP','TRADFI_LAUNCH_SESSION'):env.pop(key,None)
        try:
            result=self.runner([str(release/'venv'/'Scripts'/'python.exe'),str(script),'--root',str(self.root),'--output',str(output)],
                cwd=self.root/'data',env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=40,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if result.returncode not in (0,2) or not any(marker in output.read_text(encoding='utf-8-sig') for marker in ('ERROR ', 'INFO COMPLETE:')):
                Report.resume(output).add('ERROR','PROBE_FAILED')
        except subprocess.TimeoutExpired:
            Report.resume(output).add('ERROR','TIMEOUT')
        except Exception:
            Report.resume(output).add('ERROR','PROBE_FAILED')
        self.report('Диагностика сохранена: '+str(output)+'. Сделки и торговый сервис не запускались.')
        if os.name=='nt':subprocess.Popen(['notepad.exe',str(output)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return output
    def request_stop(self,session,instance='live'):
        validate_instance(instance)
        if not isinstance(session,str) or not re.fullmatch(r'[0-9a-f]{32}',session):
            raise SetupError('Некорректная сессия запуска.')
        path=self.root/'data'/('stop-'+instance+'-'+session+'.request')
        path.touch(exist_ok=True)
        return path

    def launch(self,execute=False,instance="live"):
        validate_instance(instance)
        if instance=="paper" and execute:raise SetupError("PAPER не допускает --execute.")
        # Runtime acquires the same lock for its whole lifetime, including direct launches.
        with lock(self.root/'operation.lock'),lock(self.root/('instance-'+instance+'.lock')):
            if instance=='paper':self.prepare_paper()
            if state_path(self.root,self.config())==state_path(self.root,self.config('paper')):
                raise SetupError('Базы LIVE и PAPER должны различаться.')
            release=self.current_release()
            if instance=='paper' and not (release/'code'/'tradfi'/'paper_runtime.py').is_file():
                raise SetupError('Эта версия не поддерживает отдельный симулятор PAPER. Выберите совместимую версию; база сохранена.')
            if not self.path_for(instance).is_file(): raise SetupError('Конфигурация отсутствует.')
            web=self.config(instance).get('web',{})
            if not isinstance(web,dict):raise SetupError('web должен быть объектом настроек.')
            if web.get('enabled',True):
                if 'password' in web and not isinstance(web['password'],str):raise SetupError('web.password должен быть строкой в кавычках.')
                password=os.environ.get('TRADFI_WEB_PASSWORD') if instance=='live' and 'TRADFI_WEB_PASSWORD' in os.environ else web.get('password','')
                minimum=5 if web.get('host','127.0.0.1') in {'127.0.0.1','::1'} and web.get('public_origin') is None and not any(bool(v) for k,v in web.items() if 'proxy' in str(k).lower() or 'external' in str(k).lower()) else 32
                if not isinstance(password,str) or not password.strip() or len(password)<minimum:
                    hint=' Если задана TRADFI_WEB_PASSWORD, она имеет приоритет.' if instance=='live' else ' PAPER использует только свой пароль из paper-service.json.'
                    raise SetupError(f'Настройки {instance.upper()} (Блокнот): задайте web.password, минимум {minimum} символов.'+hint)
            session=uuid.uuid4().hex
            env=dict(os.environ,TRADFI_LAUNCH_SESSION=session,TRADFI_LAUNCH_INSTANCE=instance)
            for key in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP'): env.pop(key,None)
            if instance=='paper':env.pop('TRADFI_WEB_PASSWORD',None)
            args=[str(release/'venv'/'Scripts'/'python.exe'),str(release/'code'/'tradfi_main.py'),
                  '--mode','paper' if instance=='paper' else 'mt5','--service','--config',str(self.path_for(instance))]
            if execute: args.append('--execute')
        # If an update wins this small handoff race, runtime refuses its lock; no mutation.
        process=subprocess.Popen(args,cwd=self.root/'data',env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return process,session


def gui():
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        if os.name=='nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0,'Нужен Python 3.13 x64 с компонентом Tcl/Tk из python.org.','TradFi',16)
        else: print('Нужен Python 3.13 с Tkinter (python.org).')
        return
    try:
        app=tk.Tk()
    except Exception:
        if os.name=='nt':
            import ctypes
            ctypes.windll.user32.MessageBoxW(0,'Не удалось открыть Tkinter. Восстановите Tcl/Tk в установщике Python.','TradFi',16)
        return
    app.title('TradFi — установка и запуск'); app.geometry('1020x640')
    if os.name!='nt' or sys.version_info[:2]!=(3,13) or sys.maxsize<=2**32:
        messagebox.showerror('Среда','Нужен Windows и Python 3.13 x64 из python.org.'); app.destroy(); return
    root=Path(os.environ['LOCALAPPDATA'])/'AI_IMPULS_TRADFI_Manager'
    events=queue.Queue(); manager=Manager(root,report=lambda s:events.put(('log',s)))
    ttk.Label(app,text='Репозиторий: '+REPO+' • ветка work').pack(anchor='w',padx=12,pady=8)
    ttk.Label(app,text='Данные: '+str(root/'data'),wraplength=750).pack(anchor='w',padx=12)
    text=tk.Text(app,height=17,state='disabled',wrap='word'); text.pack(fill='both',expand=True,padx=12,pady=8)
    buttons=[]; role_buttons={'live':[],'paper':[]}; stops={}; statuses={}; busy=False; children={}; sessions={}; https_window=None
    def log(s):
        text.configure(state='normal'); text.insert('end',s+'\n'); text.see('end'); text.configure(state='disabled')
    def refresh():
        blocked=busy or (https_window is not None and https_window.winfo_exists())
        for b in buttons:b.configure(state='disabled' if blocked or children else 'normal')
        for role in ('live','paper'):
            for b in role_buttons[role]:b.configure(state='disabled' if blocked or role in children else 'normal')
            stops[role].configure(state='normal' if role in children else 'disabled')
            statuses[role].configure(text=role.upper()+(': работает' if role in children else ': остановлен'))
    def work(action,instance=None):
        nonlocal busy
        if busy or (children if instance is None else instance in children): return
        busy=True; refresh()
        def worker():
            try:
                result=action()
                if isinstance(result,tuple): events.put(('started',(instance,result)))
            except SetupError as exc: events.put(('error',str(exc)))
            except Exception: events.put(('error','Операция не завершена. Проверьте сеть, файлы и свободное место. Секреты не выводятся.'))
            finally: events.put(('done',None))
        threading.Thread(target=worker,daemon=True).start()
    def launch(execute=False,instance="live"):
        if execute and not messagebox.askyesno('Реальное исполнение',
            'Это разрешит реальное исполнение, если ваши флаги и MT5 это допускают.\n'
            'Сохранённый цикл или отложенный /start могут исполниться сразу.\n'
            'Никакие флаги программа не меняет. Вы проверили состояние в веб-панели?'):
            return
        work(lambda:manager.launch(execute,instance),instance)
    def edit_config(instance="live"):
        try:
            with lock(root/'operation.lock'):
                manager.current_release()
                manager.prepare_settings(instance)
                if not manager.path_for(instance).is_file(): raise SetupError('Конфигурация отсутствует.')
            os.startfile(str(manager.path_for(instance)),'edit')
        except Exception: messagebox.showerror('Настройки','Не удалось открыть редактор. Откройте '+manager.path_for(instance).name+' в папке data через Блокнот.')
    def stop_service(instance):
        if instance in children and messagebox.askyesno('Остановить процесс?',
            'Это остановит обслуживание сервиса, а не закроет позиции и не выполнит /stop.\n'
            'Для завершения цикла используйте Стоп в веб-панели. Остановить процесс?'):
            try: manager.request_stop(sessions[instance],instance)
            except OSError: messagebox.showerror('Остановка','Не удалось передать запрос остановки.')
    panel=ttk.Frame(app); panel.pack(fill='x',padx=12,pady=4)
    for label,fn in [('Установить / обновить',lambda:work(manager.install)),
                     ('Диагностика запуска',lambda:work(manager.diagnose)),
                     ('Rollback кода',lambda:work(manager.rollback))]:
        b=ttk.Button(panel,text=label,command=fn); b.pack(side='left',padx=2); buttons.append(b)
    for role in ('live','paper'):
        row=ttk.Frame(app);row.pack(fill='x',padx=12,pady=4)
        statuses[role]=ttk.Label(row,text=role.upper()+': остановлен');statuses[role].pack(side='left')
        options=[('Настройки '+role.upper(),lambda r=role:edit_config(r)),
                 ('Запуск '+('LIVE READ_ONLY' if role=='live' else 'PAPER'),lambda r=role:launch(False,r))]
        if role=='live':options.append(('Реальный запуск…',lambda:launch(True,'live')))
        for label,fn in options:
            b=ttk.Button(row,text=label,command=fn);b.pack(side='left',padx=2);role_buttons[role].append(b)
        stops[role]=ttk.Button(row,text='Остановить '+role.upper()+'…',command=lambda r=role:stop_service(r),state='disabled')
        stops[role].pack(side='left',padx=2)
    def https_wizard():
        nonlocal https_window
        if busy or children:return
        if https_window is not None and https_window.winfo_exists():https_window.lift();return
        try:
            from https_setup import wizard
            https_window=wizard(manager)
        except ImportError:
            messagebox.showerror('HTTPS','Скачайте полный архив новой версии: рядом с install_tradfi.py нужен https_setup.py.')
    ttk.Button(app,text='HTTPS для телефона…',command=https_wizard).pack(pady=4)
    ttk.Label(app,text='LIVE: service.json → localhost:8765. PAPER: paper-service.json → localhost:8766.\n'
              'У каждого свой web.password. Обновление и rollback требуют остановки обоих процессов.\n'
              'Старые папки, базы и credentials не удаляются и автоматически не переносятся.',wraplength=740).pack(padx=12,pady=8)
    def poll():
        nonlocal busy
        try:
            while True:
                kind,value=events.get_nowait()
                if kind=='log': log(value)
                elif kind=='error': log(value); messagebox.showerror('TradFi',value)
                elif kind=='started':
                    instance,(child,session)=value;children[instance]=child;sessions[instance]=session
                    log(instance.upper()+': процесс запущен. Проверьте режим и состояние в его веб-панели.')
                elif kind=='done': busy=False
        except queue.Empty: pass
        for instance,child in list(children.items()):
            if child.poll() is not None:
                code=child.returncode;del children[instance]
                (root/'data'/('stop-'+instance+'-'+sessions[instance]+'.request')).unlink(missing_ok=True)
                log(f'{instance.upper()}: процесс завершён (код {code}). Автоматически не перезапускается.')
        refresh(); app.after(200,poll)
    def close():
        if https_window is not None and https_window.winfo_exists():messagebox.showinfo('TradFi','Сначала закройте HTTPS мастер, дождавшись завершения операции.');return
        if busy: messagebox.showinfo('TradFi','Дождитесь завершения операции.'); return
        if children: messagebox.showinfo('TradFi','Сначала остановите оба процесса кнопками или оставьте это окно открытым.'); return
        app.destroy()
    app.protocol('WM_DELETE_WINDOW',close); app.after(200,poll)
    log('Установка и READ_ONLY не включают торговлю. Реальный запуск — отдельное действие.')
    app.mainloop()

if __name__=='__main__':
    # Preserve imports for legacy migration/diagnostics, but new user launches
    # always select an independent application with its own installation root.
    from independent_install import main
    main()
