"""Standalone Windows Python 3.13 GUI installer. Only the standard library is needed."""
from contextlib import contextmanager, nullcontext
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


def state_path(root,config):
    p=Path(config.get('state_file','tradfi_mt5_state.sqlite3'))
    return p.resolve() if p.is_absolute() else (root/'data'/p).resolve()


def inspect_state(root, config):
    p=state_path(root,config); binding=root/'data'/'state-binding.json'
    if binding.exists():
        if read_json(binding)!={'path':str(p)} or not p.is_file():
            raise SetupError('Путь сохранённой SQLite изменён или файл потерян. Автосброс запрещён.')
    if not p.exists(): return
    try:
        db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            row=db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()
            if row is None: raise ValueError()
            s=json.loads(row[0])
            if s.get('schema')!=1 or s.get('backend')!='mt5_direct_v1' or s.get('ledger_contract')!='deal_ledger_v1': raise ValueError()
            if s.get('commands') and s.get('scope_contract')!='cycle_scope_v1': raise ValueError()
            if s.get('cycle') or s.get('phase') not in {'IDLE','PAUSED','WAIT_START'}: raise ValueError()
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


class Manager:
    def __init__(self,root,fetch=download,runner=subprocess.run,report=print):
        self.root=Path(root).resolve(); self.fetch=fetch; self.runner=runner; self.report=report
    @property
    def config_path(self): return self.root/'data'/'service.json'
    def config(self): return read_json(self.config_path) if self.config_path.exists() else {}
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
                state_file=str(self.root/'data'/'state.sqlite3'),log_file=str(self.root/'data'/'tradfi.log'),
                telegram_credentials_file='telegram.json')
            atomic_json(self.config_path,config)
        token=self.root/'data'/'telegram.json'
        if not token.exists():
            if self.pointer()['current']: raise SetupError('Telegram-файл потерян; не создаём замену.')
            atomic_json(token,{k:'' for k in ('TRADFI_TELEGRAM_TOKEN','TRADFI_TELEGRAM_CHAT_ID','TRADFI_TELEGRAM_USER_ID')})
    def prepare_python(self,release):
        env=dict(os.environ)
        for k in list(env):
            if k.startswith(('PIP_','PYTHON','TRADFI_TELEGRAM_')): env.pop(k)
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
        with lock(self.root/'operation.lock'),account_lock(self.root,self.config()):
            old=self.pointer()
            if old['current'] and not self.config_path.is_file(): raise SetupError('Конфигурация потеряна.')
            inspect_state(self.root,self.config())
            sha=latest_sha(self.fetch); self.report(f'{REPO}\nwork → {sha}')
            if sha==old['current']:
                self.current_release(); self.report('Уже установлена актуальная версия.'); return
            release=self.version(sha)
            if release.exists():
                validate_release(release/'code')
                if read_json(release/'ready.json')!={'sha':sha,'contract':CONTRACT}:
                    raise SetupError('Папка версии неполная. Проверьте её вручную; ничего не удалено.')
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
                self.initialize(release/'code')
                atomic_json(release/'ready.json',{'sha':sha,'contract':CONTRACT})
                atomic_json(self.root/'current.json',{'current':sha,'previous':old['current']})
            except BaseException:
                if created: shutil.rmtree(release,ignore_errors=True)
                raise
            finally: shutil.rmtree(stage,ignore_errors=True)
            self.report('Установка завершена. Настройки и база — в папке data. Торговля не включалась.')
    def current_release(self):
        sha=self.pointer()['current']
        if not sha: raise SetupError('Сначала установите код.')
        release=self.version(sha)
        if read_json(release/'ready.json')!={'sha':sha,'contract':CONTRACT}: raise SetupError('Версия не подготовлена.')
        validate_release(release/'code')
        return release
    def rollback(self):
        with lock(self.root/'operation.lock'),account_lock(self.root,self.config()):
            if not self.config_path.is_file(): raise SetupError('Конфигурация потеряна; rollback заблокирован.')
            pointer=self.pointer(); sha=pointer['previous']
            if not sha: raise SetupError('Предыдущей версии нет.')
            inspect_state(self.root,self.config())
            release=self.version(sha); validate_release(release/'code')
            if read_json(release/'ready.json')!={'sha':sha,'contract':CONTRACT}: raise SetupError('Предыдущая версия неполная.')
            atomic_json(self.root/'current.json',{'current':sha,'previous':pointer['current']})
            self.report('Переключён только код. SQLite, конфигурация и логи не откатывались.')
    def launch(self,execute=False):
        # Runtime acquires the same lock for its whole lifetime, including direct launches.
        with lock(self.root/'operation.lock'),account_lock(self.root,self.config()):
            release=self.current_release()
            if not self.config_path.is_file(): raise SetupError('Конфигурация отсутствует.')
            session=uuid.uuid4().hex
            env=dict(os.environ,TRADFI_LAUNCH_SESSION=session)
            for key in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP'): env.pop(key,None)
            args=[str(release/'venv'/'Scripts'/'python.exe'),str(release/'code'/'tradfi_main.py'),
                  '--mode','mt5','--service','--config',str(self.config_path)]
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
    app.title('TradFi — установка и запуск'); app.geometry('880x620')
    if os.name!='nt' or sys.version_info[:2]!=(3,13) or sys.maxsize<=2**32:
        messagebox.showerror('Среда','Нужен Windows и Python 3.13 x64 из python.org.'); app.destroy(); return
    root=Path(os.environ['LOCALAPPDATA'])/'AI_IMPULS_TRADFI_Manager'
    events=queue.Queue(); manager=Manager(root,report=lambda s:events.put(('log',s)))
    ttk.Label(app,text='Репозиторий: '+REPO+' • ветка work').pack(anchor='w',padx=12,pady=8)
    ttk.Label(app,text='Данные: '+str(root/'data'),wraplength=750).pack(anchor='w',padx=12)
    text=tk.Text(app,height=17,state='disabled',wrap='word'); text.pack(fill='both',expand=True,padx=12,pady=8)
    buttons=[]; busy=False; child=None; session=None
    def log(s):
        text.configure(state='normal'); text.insert('end',s+'\n'); text.see('end'); text.configure(state='disabled')
    def refresh():
        for b in buttons: b.configure(state='disabled' if busy or child else 'normal')
        stop.configure(state='normal' if child else 'disabled')
    def work(action):
        nonlocal busy
        if busy or child: return
        busy=True; refresh()
        def worker():
            try:
                result=action()
                if isinstance(result,tuple): events.put(('started',result))
            except SetupError as exc: events.put(('error',str(exc)))
            except Exception: events.put(('error','Операция не завершена. Проверьте сеть, файлы и свободное место. Секреты не выводятся.'))
            finally: events.put(('done',None))
        threading.Thread(target=worker,daemon=True).start()
    def launch(live):
        if live and not messagebox.askyesno('Реальное исполнение',
            'Это разрешит реальное исполнение, если ваши флаги и MT5 это допускают.\n'
            'Сохранённый цикл или отложенный /start могут исполниться сразу.\n'
            'Никакие флаги программа не меняет. Вы проверили состояние и очередь Telegram?'):
            return
        work(lambda:manager.launch(live))
    def edit_config():
        try:
            with lock(root/'operation.lock'):
                manager.current_release()
                if not manager.config_path.is_file(): raise SetupError('Конфигурация отсутствует.')
            os.startfile(str(manager.config_path),'edit')
        except Exception: messagebox.showerror('Настройки','Не удалось открыть редактор. Откройте service.json в папке data через Блокнот.')
    def telegram():
        if not manager.config_path.exists(): messagebox.showinfo('Telegram','Сначала установите код.'); return
        dialog=tk.Toplevel(app); dialog.title('Telegram — только локальный ввод'); dialog.transient(app); dialog.grab_set()
        entries=[]
        for label,secret in [('Токен',True),('Chat ID',False),('User ID',False)]:
            ttk.Label(dialog,text=label).pack(anchor='w',padx=12,pady=3)
            e=ttk.Entry(dialog,width=55,show='*' if secret else ''); e.pack(padx=12); entries.append(e)
        ttk.Label(dialog,text='Файл не зашифрован. ACL настраиваются вручную.\nПоля не показывают ранее сохранённые значения.').pack(padx=12,pady=8)
        def save():
            values=[e.get() for e in entries]
            if not values[0] or any(c.isspace() for c in values[0]) or not re.fullmatch(r'-?[0-9]+',values[1]) or int(values[1])==0 or not re.fullmatch(r'[0-9]+',values[2]) or int(values[2])<=0:
                messagebox.showerror('Telegram','Заполните токен и числовые ID; User ID положительный.',parent=dialog); return
            try:
                with lock(root/'operation.lock'),account_lock(root,manager.config()):
                    config=manager.config()
                    if config.get('telegram_credentials_file')!='telegram.json':
                        raise SetupError('Конфигурация использует другой файл credentials. Измените его вручную.')
                    atomic_json(root/'data'/'telegram.json',dict(zip(('TRADFI_TELEGRAM_TOKEN','TRADFI_TELEGRAM_CHAT_ID','TRADFI_TELEGRAM_USER_ID'),values)))
                for e in entries: e.delete(0,'end')
                dialog.destroy(); log('Telegram-файл сохранён локально. Значения не выводятся.')
            except Exception: messagebox.showerror('Telegram','Не удалось сохранить; проверьте блокировки и путь.',parent=dialog)
        ttk.Button(dialog,text='Сохранить',command=save).pack(pady=10)
    def stop_service():
        if child and messagebox.askyesno('Остановить процесс?',
            'Это остановит обслуживание сервиса, а не закроет позиции и не выполнит /stop.\n'
            'Для завершения цикла используйте /stop в Telegram. Остановить процесс?'):
            try: (root/'data'/('stop-'+session+'.request')).touch(exist_ok=True)
            except OSError: messagebox.showerror('Остановка','Не удалось передать запрос остановки.')
    panel=ttk.Frame(app); panel.pack(fill='x',padx=12,pady=4)
    for label,fn in [('Установить / обновить',lambda:work(manager.install)),('Настройки (Блокнот)',edit_config),
                     ('Telegram',telegram),('READ_ONLY',lambda:launch(False)),('Реальный запуск…',lambda:launch(True)),
                     ('Rollback кода',lambda:work(manager.rollback))]:
        b=ttk.Button(panel,text=label,command=fn); b.pack(side='left',padx=2); buttons.append(b)
    stop=ttk.Button(app,text='Остановить процесс…',command=stop_service,state='disabled'); stop.pack(pady=5)
    ttk.Label(app,text='Первый запуск: установить → заполнить service.json и Telegram → READ_ONLY.\n'
              'Старые папки, базы и credentials не удаляются и автоматически не переносятся.',wraplength=740).pack(padx=12,pady=8)
    def poll():
        nonlocal busy,child,session
        try:
            while True:
                kind,value=events.get_nowait()
                if kind=='log': log(value)
                elif kind=='error': log(value); messagebox.showerror('TradFi',value)
                elif kind=='started': child,session=value; log('Процесс запущен. Проверьте сообщение режима и /status в Telegram.')
                elif kind=='done': busy=False
        except queue.Empty: pass
        if child and child.poll() is not None:
            code=child.returncode; child=None
            (root/'data'/('stop-'+session+'.request')).unlink(missing_ok=True)
            log(f'Процесс завершён (код {code}). При ошибке проверьте конфигурацию, MT5 и свежесть котировок. Повторно автоматически не запускается.')
        refresh(); app.after(200,poll)
    def close():
        if busy: messagebox.showinfo('TradFi','Дождитесь завершения операции.'); return
        if child: messagebox.showinfo('TradFi','Сначала остановите процесс кнопкой или оставьте это окно открытым.'); return
        app.destroy()
    app.protocol('WM_DELETE_WINDOW',close); app.after(200,poll)
    log('Установка и READ_ONLY не включают торговлю. Реальный запуск — отдельное действие.')
    app.mainloop()

if __name__=='__main__':
    gui()
