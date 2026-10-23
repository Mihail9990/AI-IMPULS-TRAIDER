"""Windows pre-trade UI. Local pinned release; no service/live launch controls."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import tempfile
import webbrowser
sys.path.insert(0,str(Path(__file__).resolve().parent))
from install_tradfi import Manager,SetupError,read_json,lock,account_lock,state_path,state_lock,SHA,MAX_DOWNLOAD


class PretradeManager(Manager):
    def __init__(self,root,bundle,**kwargs):
        self.bundle=Path(bundle).resolve()
        super().__init__(root,fetch=self.local_release,**kwargs)
    def local_release(self,url,limit=MAX_DOWNLOAD):
        manifest=read_json(self.bundle/'release.json')
        sha=manifest.get('commit','');digest=manifest.get('sha256','')
        if not isinstance(sha,str) or not SHA.fullmatch(sha) or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
            raise SetupError('Некорректный manifest локального пакета.')
        path=self.bundle/'release.zip'
        if not path.is_file() or path.stat().st_size>MAX_DOWNLOAD:raise SetupError('Локальный архив отсутствует или слишком велик.')
        blob=path.read_bytes()
        if hashlib.sha256(blob).hexdigest()!=digest:raise SetupError('SHA256 локального архива не совпадает; установка отменена.')
        if '/git/ref/heads/work' in url:return json.dumps({'ref':'refs/heads/work','object':{'type':'commit','sha':sha}}).encode()
        if not url.endswith('/'+sha+'.zip'):raise SetupError('Неожиданная версия локального пакета.')
        return blob
    def launch(self,*args,**kwargs):
        raise SetupError('Этот launcher не запускает торговый сервис. Только проверки до торговли.')
    def run_check(self,kind,release=None):
        if kind not in {'environment','configuration','mt5','offline'}:raise SetupError('Неизвестная проверка.')
        release=release or self.current_release();code=release/'code'
        python=release/'venv'/'Scripts'/'python.exe'
        args=([str(python),'-I',str(code/'tools'/'run_offline_tests.py')] if kind=='offline' else
              [str(python),'-I',str(code/'pretrade_checks.py'),kind,'--config',str(self.config_path)])
        env=dict(os.environ)
        for name in list(env):
            if name.startswith(('TRADFI_','PYTHON')):env.pop(name,None)
        isolated=tempfile.TemporaryDirectory(prefix='tradfi-offline-') if kind=='offline' else None
        if isolated:env.update(LOCALAPPDATA=isolated.name,TEMP=isolated.name,TMP=isolated.name)
        try:
            result=self.runner(args,cwd=str(code),env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',
                timeout=180 if kind=='offline' else 45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if kind=='offline':
                output=(result.stdout or '')+'\n'+(result.stderr or '')
                match=re.search(r'Ran (\d+) tests?',output)
                good=result.returncode==0 and match and int(match[1])>=495 and 'Blocked real network attempts: 0' in output
                report=dict(check=kind,status='PASS' if good else 'FAIL',tests=int(match[1]) if match else None,network='blocked')
            else:
                parsed=json.loads(result.stdout)
                # Only the fixed report structure from this bundled checker is displayed.
                if not isinstance(parsed,dict) or parsed.get('check') not in {kind,'configuration'} or parsed.get('status') not in {'PASS','WARN','FAIL'}:raise ValueError()
                allowed={'check','status','code','mode','execution_ready','history_complete','timestamp_semantics','symbol','positions','pending',
                    'candles','bid','ask','raw_broker_time_msc','local_observed_time','quote_age_seconds','quote_freshness','independent_utc',
                    'values_redacted','diagnostic_execution_enabled','saved_execution_enabled','details_redacted','windows','python','bits','packages'}
                report={k:v for k,v in parsed.items() if k in allowed}
        except subprocess.TimeoutExpired:report=dict(check=kind,status='FAIL',code='CHECK_TIMEOUT_NO_AUTORETRY')
        except (OSError,ValueError,TypeError):report=dict(check=kind,status='FAIL',code='CHECK_FAILED_DETAILS_REDACTED')
        finally:
            if isolated:isolated.cleanup()
        self.report(json.dumps(report,ensure_ascii=False));return report
    def verify_prepared(self,release):
        if not (release/'code'/'tools'/'run_offline_tests.py').is_file() or not (release/'code'/'tests').is_dir():
            raise SetupError('В локальном пакете отсутствует полный offline runner/tests.')
        if self.run_check('offline',release)['status']!='PASS':raise SetupError('Offline self-tests не прошли. Прежняя версия сохранена, сервис не запускался.')
    def check(self,kind):
        with lock(self.root/'operation.lock'),account_lock(self.root,self.config()),state_lock(self.root,self.config()):
            return self.run_check(kind)
    def preview(self):
        code=self.current_release()/'code'
        page=code/'web'/'local-panel'/'index.html'
        if not page.is_file():raise SetupError('Файлы панели отсутствуют.')
        # file:// standalone MOCK has no backend or trading commands.
        webbrowser.open(page.as_uri())
        self.report('Открыт локальный MOCK: график, zoom/pan и preview. MT5 и торгового backend здесь нет.')


def gui():
    import tkinter as tk
    from tkinter import ttk,messagebox
    app=tk.Tk();app.title('TradFi — проверки ДО торговли');app.geometry('780x680')
    if os.name!='nt' or sys.version_info[:2]!=(3,13) or sys.maxsize<=2**32:
        messagebox.showerror('Среда','Нужен Windows, Python 3.13 x64 с Tcl/Tk. Ничего не запущено.');app.destroy();return
    root=Path(os.environ['LOCALAPPDATA'])/'AI_IMPULS_TRADFI_Manager'
    events=queue.Queue();manager=PretradeManager(root,Path(__file__).resolve().parent,report=lambda s:events.put(s))
    ttk.Label(app,text='Только проверки. Реальный запуск и Telegram-сервис недоступны.',wraplength=730).pack(pady=8)
    ttk.Label(app,text='Существующие данные: '+str(root/'data'),wraplength=730).pack(pady=4)
    output=tk.Text(app,height=16,wrap='word',state='disabled');output.pack(fill='both',expand=True,padx=10,pady=8)
    busy=False;buttons=[]
    def run(action):
        nonlocal busy
        if busy:return
        busy=True
        for b in buttons:b.configure(state='disabled')
        def worker():
            try:action()
            except SetupError as exc:events.put(str(exc))
            except Exception:events.put('Проверка не завершена. Данные сохранены; подробности исключены из отчёта.')
            finally:events.put(None)
        threading.Thread(target=worker,daemon=True).start()
    def readonly():
        if messagebox.askyesno('READ_ONLY MT5','Проверка подключит Python к указанному терминалу (он может открыться).\nНикаких order_send, login или смены счёта. Существующие EA терминала не управляются этой проверкой.\nПродолжить только диагностические чтения?'):
            run(lambda:manager.check('mt5'))
    def edit():
        if not manager.config_path.is_file():raise SetupError('Сначала установите локальный пакет.')
        os.startfile(str(manager.config_path),'edit')
    actions=[('1. Установить / обновить из этого ZIP',lambda:run(manager.install)),
        ('2. Настройки — открыть локально',lambda:run(edit)),
        ('3. Проверить среду и зависимости',lambda:run(lambda:manager.check('environment'))),
        ('4. Проверить конфигурацию (без секретов)',lambda:run(lambda:manager.check('configuration'))),
        ('5. Offline self-tests логики',lambda:run(lambda:manager.check('offline'))),
        ('6. READ_ONLY: MT5 / котировки / время',readonly),
        ('7. Проверить панель MOCK в браузере',lambda:run(manager.preview))]
    for label,fn in actions:
        b=ttk.Button(app,text=label,command=fn);b.pack(fill='x',padx=12,pady=3);buttons.append(b)
    ttk.Label(app,text='Не удаляйте data / lock / state-binding. При занятой блокировке обновление остановится.\nСекреты не импортируются. Проверки не запускают торговлю.',wraplength=730).pack(pady=8)
    def poll():
        nonlocal busy
        try:
            while True:
                msg=events.get_nowait()
                if msg is None:
                    busy=False
                    for b in buttons:b.configure(state='normal')
                else:
                    output.configure(state='normal');output.insert('end',msg+'\n');output.see('end');output.configure(state='disabled')
        except queue.Empty:pass
        app.after(200,poll)
    def close():
        if busy:messagebox.showinfo('Проверка','Дождитесь окончания операции.');return
        app.destroy()
    app.protocol('WM_DELETE_WINDOW',close);app.after(200,poll);app.mainloop()

if __name__=='__main__':gui()
