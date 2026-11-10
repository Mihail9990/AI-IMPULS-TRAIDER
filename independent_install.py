"""Independent LIVE installer. No automatic legacy data migration."""
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import queue
import time
import uuid

import install_tradfi as legacy

CONTRACT = {'format': 2, 'roles': ['live'], 'layout': 'independent_roots_v1'}
ROOT_NAMES = {'live': 'AI_IMPULS_LIVE'}


def default_root(role):
    legacy.validate_instance(role)
    return Path(os.environ['LOCALAPPDATA']) / ROOT_NAMES[role]


def migration_lock_path(root):
    root=Path(root).resolve()
    return root.parent/('.'+root.name+'.migration.lock')


def own_data_directory(root):
    root=Path(root).resolve();data=root/'data'
    if data.is_symlink() or (hasattr(data,'is_junction') and data.is_junction()) or not data.resolve().is_relative_to(root):
        raise legacy.SetupError('Папка data не может быть ссылкой или junction на другой каталог.')
    return data.resolve()


def validate_code(code):
    legacy.validate_release(code)
    if not (code/'independent-install-contract.json').is_file() or legacy.read_json(code/'independent-install-contract.json') != CONTRACT:
        raise legacy.SetupError('Версия не поддерживает независимые установки. Нужна опубликованная совместимая версия; прежний код сохранён.')
    for filename in ('independent_install.py', 'tradfi/runtime.py', 'tradfi_config.example.json'):
        if not (code/filename).is_file():raise legacy.SetupError('Архив независимой установки неполный.')


def copy_database(source, destination):
    """Consistent SQLite backup, never a raw copy of a possible WAL database."""
    source=Path(source);destination=Path(destination)
    if source.is_symlink():raise legacy.SetupError('Ссылка вместо SQLite: нужна ручная проверка.')
    def validate_destination():
        if source.resolve()==destination.resolve() or destination.is_symlink():raise legacy.SetupError('Исходная SQLite и назначение должны быть разными обычными файлами.')
        if any(Path(str(destination)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
            raise legacy.SetupError('У базы назначения остался WAL/SHM/journal. Перенос остановлен без удаления файлов; требуется проверка сохранённой копии.')
    validate_destination()
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.'+uuid.uuid4().hex+'.migration')
    try:
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                if src.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise legacy.SetupError('Исходная SQLite повреждена; ничего не переключено.')
                deadline=time.monotonic()+45
                def progress(*_):
                    if time.monotonic()>deadline:raise legacy.SetupError('Перенос SQLite превысил время; исходная база сохранена.')
                src.backup(dst,pages=256,progress=progress,sleep=.05)
                if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise legacy.SetupError('Копия SQLite не прошла проверку.')
                # A backup must be a self-contained main file. Otherwise even a
                # later read-only inspection may create destination WAL/SHM.
                # Runtime Store restores WAL when it actually starts.
                if dst.execute('PRAGMA journal_mode=DELETE').fetchone()[0].lower()!='delete':
                    raise legacy.SetupError('Не удалось подготовить автономную копию SQLite.')
        validate_destination()
        os.replace(temporary,destination)
    finally:temporary.unlink(missing_ok=True)


class IndependentManager(legacy.Manager):
    def __init__(self,root,role,**kwargs):
        legacy.validate_instance(role)
        super().__init__(root,**kwargs)
        self.role=role;self._migrating=False
    def _instance(self,instance=None):
        if instance is not None and instance!=self.role:raise legacy.SetupError('Этот каталог принадлежит другому приложению.')
        return self.role
    def _check_installation(self):
        own_data_directory(self.root)
        role_path=self.root/'application.json'
        if role_path.exists() and legacy.read_json(role_path)!={'format':1,'role':self.role}:
            raise legacy.SetupError('Каталог принадлежит другому приложению.')
        pending=self.root/'migration-pending.json'
        if pending.exists():
            if not self._migrating or legacy.read_json(pending)!=self._migrating:
                raise legacy.SetupError('Перенос не завершён или принадлежит другой операции. Запуск и обновление заблокированы.')
        elif self._migrating:
            raise legacy.SetupError('Привязка незавершённого переноса потеряна; файлы не изменяются.')
        marker=self.root/'managed-install.json'
        if marker.exists():
            if legacy.read_json(marker)!=dict(format=2,role=self.role,layout='independent_roots_v1') or not role_path.exists():
                raise legacy.SetupError('Установка архивная, общая или повреждённая. Используйте явный перенос, не пересоздавайте привязку.')
    @property
    def config_path(self):return self.path_for()
    def path_for(self,instance=None):
        self._instance(instance)
        return self.root/'data'/'service.json'
    def config(self,instance=None):
        self._instance(instance)
        path=self.config_path
        if not path.exists():
            if self.pointer()['current'] or legacy.binding_path(self.root,self.role).exists():
                raise legacy.SetupError('Собственная конфигурация потеряна; автоматический сброс запрещён.')
            return {}
        config=legacy.read_json(path)
        if not isinstance(config,dict):raise legacy.SetupError('Конфигурация должна быть JSON-объектом.')
        for key in ('state_file','log_file'):
            if key in config:
                p=Path(config[key]);p=p.resolve() if p.is_absolute() else (self.root/'data'/p).resolve()
                if not p.is_relative_to(own_data_directory(self.root)):
                    raise legacy.SetupError('Данные приложения должны находиться в его собственной папке data.')
        return config
    @contextmanager
    def maintenance_guard(self):
        with ExitStack() as stack:
            if not self._migrating:stack.enter_context(legacy.lock(migration_lock_path(self.root)))
            for name in ('operation.lock','instance-'+self.role+'.lock'):stack.enter_context(legacy.lock(self.root/name))
            self._check_installation()
            config=self.config()
            if self.role=='live' and not self._migrating:stack.enter_context(legacy.account_lock(self.root,config))
            stack.enter_context(legacy.state_lock(self.root,config))
            yield
    def inspect_states(self):legacy.inspect_state(self.root,self.config(),self.role)
    def initialize(self,code):
        validate_code(code);self._check_installation()
        marker=self.root/'managed-install.json'
        if not marker.exists():
            if (self.root/'data').exists() and any((self.root/'data').iterdir()):raise legacy.SetupError('Неизвестные данные: используйте явный перенос.')
            legacy.atomic_json(self.root/'application.json',{'format':1,'role':self.role})
            legacy.atomic_json(marker,dict(format=2,role=self.role,layout='independent_roots_v1'))
        if not self.config_path.exists():
            if self.pointer()['current']:raise legacy.SetupError('Конфигурация потеряна; не создаём замену.')
            name='tradfi_config.example.json'
            config=legacy.read_json(code/name)
            config.update(enable_trading=False,state_file=str(self.root/'data'/'state.sqlite3'),log_file=str(self.root/'data'/'runtime.log'))
            if self.role=='live':config.update(legacy_ea_removed=False,legacy_state_reconciled=False)
            legacy.atomic_json(self.config_path,config)
        self.write_launcher()
    def write_launcher(self):
        # This copy lives inside the application's own folder, not the ZIP or
        # another installation. Each invocation resolves this root's pointer.
        loader='''from pathlib import Path
import json, subprocess, sys
root=Path(__file__).resolve().parent
role=json.loads((root/'application.json').read_text())['role']
sha=json.loads((root/'current.json').read_text())['current']
if role not in ('live',) or not isinstance(sha,str) or len(sha)!=40 or any(c not in '0123456789abcdef' for c in sha):
    raise SystemExit('Invalid local application pointer')
script=root/'releases'/sha/'code'/'independent_install.py'
raise SystemExit(subprocess.call([sys.executable,str(script),'--role',role,'--root',str(root)]))
'''
        (self.root/'launcher.py').write_text(loader,encoding='utf-8')
        (self.root/('START_'+self.role.upper()+'.bat')).write_text('@echo off\r\npy -3.13 "%~dp0launcher.py"\r\nif errorlevel 1 pause\r\n',encoding='ascii')
    def verify_prepared(self,release):validate_code(release/'code')
    def current_release(self):
        self._check_installation()
        release=super().current_release();validate_code(release/'code')
        return release
    def backup(self):
        old=self.pointer()
        if not old['current']:return None
        target=self.root/'backups'/('before-update-'+uuid.uuid4().hex);target.mkdir(parents=True)
        source=legacy.state_path(self.root,self.config())
        if source.exists():copy_database(source,target/'state.sqlite3')
        legacy.atomic_json(target/'manifest.json',dict(format=3,role=self.role,current=old['current'],previous=old['previous'],state_copied=source.exists(),credentials_copied=False))
        return target
    def rollback(self):
        with self.maintenance_guard():
            pointer=self.pointer();sha=pointer['previous']
            if not sha:raise legacy.SetupError('Предыдущей версии этого приложения нет.')
            self.inspect_states();self.backup()
            release=self.version(sha);validate_code(release/'code')
            if legacy.read_json(release/'ready.json')!={'sha':sha,'contract':legacy.CONTRACT}:raise legacy.SetupError('Предыдущая версия неполная.')
            legacy.atomic_json(self.root/'current.json',{'current':sha,'previous':pointer['current']})
            self.report(self.role.upper()+': переключён только собственный код; данные сохранены.')
    def prepare_settings(self,instance=None):
        self._instance(instance);self.current_release()
        config=self.config();web=config.setdefault('web',{})
        if not isinstance(web,dict):raise legacy.SetupError('web должен быть объектом настроек.')
        if 'password' not in web:web['password']='';legacy.atomic_json(self.config_path,config)
    def request_stop(self,session,instance=None):return super().request_stop(session,self._instance(instance))
    def launch(self,execute=False,instance=None):
        self._instance(instance)
        with legacy.lock(migration_lock_path(self.root)),legacy.lock(self.root/'operation.lock'),legacy.lock(self.root/('instance-'+self.role+'.lock')):
            release=self.current_release();config=self.config();web=config.get('web',{})
            if not isinstance(web,dict):raise legacy.SetupError('web должен быть объектом настроек.')
            if web.get('enabled',True):
                from tradfi.web_auth import resolve_password
                resolve_password(web)
            session=uuid.uuid4().hex
            env=dict(os.environ,TRADFI_LAUNCH_SESSION=session,TRADFI_LAUNCH_INSTANCE=self.role)
            for key in ('PYTHONPATH','PYTHONHOME','PYTHONSTARTUP'):env.pop(key,None)
            args=[str(release/'venv'/'Scripts'/'python.exe'),str(release/'code'/'tradfi_main.py'),'--mode','mt5','--service','--config',str(self.config_path)]
            if execute:args.append('--execute')
        return subprocess.Popen(args,cwd=self.root/'data',env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)),session


def gui(role,root=None):
    import tkinter as tk
    from tkinter import messagebox,ttk
    root=Path(root).resolve() if root is not None else default_root(role);manager=IndependentManager(root,role)
    legacy_root=Path(os.environ['LOCALAPPDATA'])/'AI_IMPULS_TRADFI_Manager'
    app=tk.Tk();app.title('AI IMPULS · '+role.upper());app.geometry('840x540')
    if os.name!='nt' or sys.version_info[:2]!=(3,13) or sys.maxsize<=2**32:
        messagebox.showerror('Среда','Нужен Windows и Python 3.13 x64.');app.destroy();return
    events=queue.Queue();child=None;session=None;busy=False;buttons=[]
    manager.report=lambda text:events.put(('log',text))
    ttk.Label(app,text=role.upper()+' · '+str(root),wraplength=800).pack(padx=12,pady=10)
    output=tk.Text(app,height=19,wrap='word',state='disabled');output.pack(fill='both',expand=True,padx=12,pady=8)
    def log(text):output.configure(state='normal');output.insert('end',text+'\n');output.see('end');output.configure(state='disabled')
    def work(fn):
        nonlocal busy
        if busy or child:return
        busy=True
        def run():
            try:
                result=fn()
                if isinstance(result,tuple):events.put(('started',result))
            except Exception as exc:events.put(('error',str(exc) if isinstance(exc,legacy.SetupError) else 'Операция не завершена. Настройки и исходные данные сохранены.'))
            finally:events.put(('done',None))
        threading.Thread(target=run,daemon=True).start()
    def launch(execute=False):
        if execute and not messagebox.askyesno('Реальное исполнение','Запустить LIVE с разрешением реального исполнения? Сохранённый цикл может продолжиться сразу. Флаги и ограничения не изменяются.'):return
        work(lambda:manager.launch(execute))
    def settings():
        try:manager.prepare_settings();os.startfile(str(manager.config_path),'edit')
        except Exception:messagebox.showerror('Настройки','Сначала установите приложение. Проверьте его service.json.')
    def stop():
        if child and messagebox.askyesno('Остановка '+role.upper(),'Остановить только этот процесс? Это не закрывает позиции и не отменяет ордера.'):
            try:manager.request_stop(session)
            except OSError:messagebox.showerror('Остановка','Не удалось передать запрос.')
    def install():
        if not manager.pointer()['current'] and (legacy_root/'managed-install.json').exists():
            messagebox.showinfo('Сохранить прежние данные','Обнаружена прежняя общая установка. Автоматический перенос отключён. Сохраните прежнюю установку и сначала согласуйте ручной перенос LIVE, истории и RECOVERY.')
            return
        work(manager.install)
    row=ttk.Frame(app);row.pack(pady=4)
    options=[('Установить / обновить',install),('Настройки',settings),('Запуск '+'READ_ONLY',lambda:launch(False)),('Rollback',lambda:work(manager.rollback))]
    if role=='live':
        options.insert(3,('Реальный запуск…',lambda:launch(True)))
        options.append(('Диагностика',lambda:work(manager.diagnose)))
    for label,fn in options:
        b=ttk.Button(row,text=label,command=fn);b.pack(side='left',padx=2);buttons.append(b)
    stop_button=ttk.Button(app,text='Остановить '+role.upper(),command=stop,state='disabled');stop_button.pack(pady=4)
    ttk.Label(app,text='Локальная панель: http://127.0.0.1:8765\nОбновление не меняет настройки и базу. MT5 остаётся внешним источником.').pack(pady=8)
    def poll():
        nonlocal child,session,busy
        try:
            while True:
                kind,value=events.get_nowait()
                if kind=='started':child,session=value;log(role.upper()+': процесс запущен.')
                elif kind=='done':busy=False
                elif kind=='error':log(value);messagebox.showerror(role.upper(),value)
                else:log(value)
        except queue.Empty:pass
        if child and child.poll() is not None:
            log('Процесс завершён (код '+str(child.returncode)+').');child=None
            (root/'data'/('stop-'+role+'-'+session+'.request')).unlink(missing_ok=True)
        for b in buttons:b.configure(state='disabled' if busy or child else 'normal')
        stop_button.configure(state='normal' if child else 'disabled');app.after(200,poll)
    def close():
        if busy or child:messagebox.showinfo(role.upper(),'Дождитесь операции или остановите только этот процесс.');return
        app.destroy()
    app.protocol('WM_DELETE_WINDOW',close);app.after(200,poll);app.mainloop()


def main(argv=None):
    parser=argparse.ArgumentParser(description='Independent LIVE application')
    parser.add_argument('--role',choices=('live',),default='live')
    parser.add_argument('--root',type=Path)
    args=parser.parse_args(argv)
    return gui(args.role,args.root)


if __name__=='__main__':main()
