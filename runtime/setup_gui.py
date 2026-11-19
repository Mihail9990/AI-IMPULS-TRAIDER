from pathlib import Path
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
from setup_core import install,adopt_existing,install_caddy
from connectivity_core import profile,ConnectionSession,EXE_SHA256
from auto_mt5 import detect_mt5,prepare_compile_ea
from firewall_setup import configure_firewall
import hashlib
root=tk.Tk();root.title('AI IMPULS · RECOVERY WEB');root.geometry('980x790')
assets=Path(__file__).resolve().parent;payload=assets/'server';events=queue.Queue();busy=[False];ready=[None];session=[None];key=[None];native_mode=[False];failed_child=[False]
frame=ttk.Frame(root,padding=14);frame.pack(fill='both',expand=True)
ttk.Label(frame,text='Установка и первое подключение веб-панели',font=('Segoe UI',15,'bold')).pack(anchor='w')
ttk.Label(frame,text='Python и MT5 должны быть установлены. Подготовка не запускает торговлю.\nУкажите публичный IPv4 VPS; остальные поля можно оставить по умолчанию.',justify='left').pack(anchor='w',pady=8)
python=tk.StringVar(value=sys.executable);server=tk.StringVar(value=str(Path(os.environ.get('LOCALAPPDATA') or Path.home())/'AI_IMPULS_RECOVERY_WEB'));data=tk.StringVar();archive=tk.StringVar();ip=tk.StringVar();url=tk.StringVar()
def choose(var,kind):
    p=filedialog.askdirectory() if kind=='dir' else filedialog.askopenfilename(filetypes=[('ZIP','*.zip')] if kind=='zip' else [('Python','python.exe')])
    if p:var.set(p.replace('/', '\\'))
def row(label,var,kind=None):
    f=ttk.Frame(frame);f.pack(fill='x',pady=4);ttk.Label(f,text=label,width=26).pack(side='left');ttk.Entry(f,textvariable=var).pack(side='left',fill='x',expand=True)
    if kind:ttk.Button(f,text='Выбрать',command=lambda:choose(var,kind)).pack(side='right')
row('Публичный IPv4 VPS:',ip);row('Python x64:',python,'exe');row('Папка сервера:',server,'dir');row('Каталог данных MT5:',data,'dir');row('Архив Caddy:',archive,'zip')
ttk.Label(frame,text='Поля MT5 и Caddy можно оставить пустыми. Если автоопределение MT5 или загрузка Caddy не пройдут,\nвыберите путь или официальный ZIP вручную.',justify='left').pack(anchor='w',pady=4)
open443=tk.BooleanVar(value=False);auto_connect=tk.BooleanVar(value=True)
ttk.Checkbutton(frame,text='Разрешить входящий TCP 443 только для Caddy (Windows запросит разрешение)',variable=open443).pack(anchor='w')
ttk.Checkbutton(frame,text='Запустить проверку связи после установки (без торговли)',variable=auto_connect).pack(anchor='w')
controls=ttk.Frame(frame);controls.pack(fill='x',pady=5)
more=ttk.Frame(frame);more.pack(fill='x',pady=4)
tls_disclosure='Публичный IP передаётся Let’s Encrypt и публикуется в CT-журналах для сертификата. Условия: https://letsencrypt.org/repository/'
status=tk.StringVar(value='После подготовки появятся адрес и ключ веб-панели.')
ttk.Label(frame,textvariable=status,wraplength=920,justify='left').pack(anchor='w',pady=5)
credentials=ttk.Frame(frame);credentials.pack(fill='x');ttk.Entry(credentials,textvariable=url,state='readonly').pack(side='left',fill='x',expand=True)
def copy_url():
    if url.get():root.clipboard_clear();root.clipboard_append(url.get())
def copy_key():
    if key[0]:root.clipboard_clear();root.clipboard_append(key[0]);status.set('Ключ скопирован. Вставьте только в веб-панель, не отправляйте в чат. Общий буфер RDP может синхронизировать ключ.')
ttk.Button(credentials,text='Копировать адрес',command=copy_url).pack(side='left',padx=5)
key_button=ttk.Button(credentials,text='Копировать ключ',command=copy_key,state='disabled');key_button.pack(side='left')
log=tk.Text(frame,height=18,wrap='word');log.pack(fill='both',expand=True,pady=8)
def emit(text):events.put(('log',text))
def prepare():
    if busy[0] or session[0] is not None:return
    values=(python.get().strip(),server.get().strip(),data.get().strip(),archive.get().strip(),ip.get().strip())
    allow_port=open443.get();auto_start=auto_connect.get()
    summary='Будут установлены Python-зависимости из PyPI, проверенный Caddy и скомпилирован советник чтения. Торговля не запускается.'
    if allow_port:summary+='\nБудет добавлено правило Windows: разрешить подключения из интернета только к caddy.exe по TCP 443. Firewall не отключается.'
    if auto_start:summary+='\nБудет запущена проверка HTTPS без торговли. '+tls_disclosure+' Подтвердите, что указанный публичный IP принадлежит вашему VPS.'
    if not messagebox.askyesno('Подготовка VPS',summary+'\nПродолжить?'):return
    ready[0]=None;key[0]=None;url.set('');key_button.config(state='disabled');ea_folder_button.config(state='disabled')
    busy[0]=True;prepare_button.config(state='disabled');connect_button.config(state='disabled');native_button.config(state='disabled')
    status.set('Подготовка…')
    def work():
        try:
            py,target,mt5,zipfile,address=values
            from connectivity_core import public_ip,safe_path
            if Path(target).name.upper()!='AI_IMPULS_RECOVERY_WEB':
                raise ValueError('Для RECOVERY WEB нужна отдельная папка AI_IMPULS_RECOVERY_WEB. Старую установку не выбирайте.')
            address=public_ip(address)
            installation=detect_mt5(data_dir=mt5 or None)
            emit(f'Найден каталог MT5: {installation.data_dir}\n')
            if Path(target).exists():target=adopt_existing(payload,target);emit('Проверена существующая установка, без переустановки.\n')
            else:target=install(payload,target,py,log=emit)
            exe=safe_path(target/'tools/caddy/caddy.exe')
            if exe.exists():
                if hashlib.sha256(exe.read_bytes()).hexdigest()!=EXE_SHA256:raise RuntimeError('Существующий Caddy отличается от проверенного; не перезаписан.')
                emit('Caddy уже установлен и проверен.\n')
            else:
                default=Path.home()/'Downloads'/'caddy_2.11.9_windows_amd64.zip'
                try:install_caddy(target,log=emit,local_archive=zipfile or (default if default.is_file() else None))
                except OSError as exc:raise RuntimeError('Не удалось получить Caddy. Выберите официальный ZIP вручную: '+str(exc)) from exc
            try:
                build=prepare_compile_ea(target,installation,log=emit)
                emit(f'Советник: {build.executable}. Если его ещё нет на графике, в MT5 обновите Expert Advisors и добавьте советник вручную.\n')
            except (ValueError,RuntimeError,OSError) as exc:
                emit('EA не изменён; проверка связи доступна отдельно. Для реального запуска сначала устраните причину: '+str(exc)+'\n')
                build=None
            folder,token=profile(target,address)
            prepared=(target,folder,token,address,installation,build)
            if allow_port:
                try:configure_firewall(exe,approved=True,log=emit)
                except Exception as exc:events.put(('prepared_error',(prepared,'Брандмауэр: '+str(exc))));return
            if auto_start:
                try:
                    obj=ConnectionSession(target,assets);obj.start(address,token,folder);session[0]=obj
                except Exception as exc:events.put(('prepared_error',(prepared,str(exc))));return
            events.put(('ready_running' if auto_start else 'ready',prepared))
        except Exception as exc:events.put(('error',str(exc)))
    threading.Thread(target=work,daemon=False).start()
def connect():
    if busy[0] or ready[0] is None or session[0] is not None:return
    if not messagebox.askyesno('Подключить веб-панель без торговли?', 'Будут запущены только read-only API на 127.0.0.1:8767 и Caddy на TCP 443. Firewall не меняется.\nIP передаётся Let’s Encrypt и публикуется в CT-журналах для сертификата. Условия: https://letsencrypt.org/repository/\n\nПодтверждаете принадлежность IP вашему VPS и продолжение с этими условиями?'):return
    busy[0]=True;connect_button.config(state='disabled');prepare_button.config(state='disabled');native_button.config(state='disabled')
    def work():
        try:
            target,folder,token,address,_,_=ready[0];obj=ConnectionSession(target,assets);obj.start(address,token,folder);session[0]=obj;events.put(('connected',None))
        except Exception as exc:events.put(('error',str(exc)))
    threading.Thread(target=work,daemon=False).start()
def native_settings(installation):
    dialog=tk.Toplevel(root)
    dialog.title('Реальный счёт MT5 — пробный запуск')
    dialog.transient(root);dialog.grab_set()
    panel=ttk.Frame(dialog,padding=15);panel.pack(fill='both',expand=True)
    ttk.Label(panel,text='Запуск может отправлять реальные ордера и продолжить сохранённые диапазоны.\n'
              'На брокере эту сборку ещё не проверяли. Остановка не отменяет ордера и не закрывает позиции.',
              wraplength=620,justify='left').pack(anchor='w',pady=(0,9))
    ttk.Label(panel,text=str(installation.terminal),wraplength=620).pack(anchor='w')
    fields={}
    for field,label,default in [('account','Номер счёта MT5',''),('broker_server','Сервер брокера',''),
                                ('symbol','Символ','XAUUSD.s'),('magic','Уникальный Magic ID',''),
                                ('keyword','Напишите REAL для запуска','')]:
        line=ttk.Frame(panel);line.pack(fill='x',pady=3)
        ttk.Label(line,text=label,width=30).pack(side='left')
        fields[field]=tk.StringVar(value=default)
        ttk.Entry(line,textvariable=fields[field]).pack(side='left',fill='x',expand=True)
    attached=tk.BooleanVar();resume=tk.BooleanVar()
    ttk.Checkbutton(panel,text='Проверил HTTPS с телефона и подключил RecoveryEvidenceBridge к нужному графику',variable=attached).pack(anchor='w',pady=3)
    ttk.Checkbutton(panel,text='Разрешаю продолжение ранее сохранённых диапазонов данного счёта',variable=resume).pack(anchor='w',pady=3)
    result=[]
    def accept():
        value={k:v.get().strip() for k,v in fields.items()}
        if not attached.get() or value.pop('keyword')!='REAL':
            messagebox.showwarning('Подтверждение','Отметьте выполненные шаги и введите REAL.',parent=dialog);return
        try:
            for k in ('account','magic'):
                if not value[k].isascii() or not value[k].isdecimal() or not 0<int(value[k])<2**63:raise ValueError(k)
                value[k]=int(value[k])
            if not value['broker_server'] or not value['symbol']:raise ValueError('empty')
        except ValueError:
            messagebox.showwarning('Параметры MT5','Укажите номер счёта, сервер, символ и отдельный положительный Magic ID.',parent=dialog);return
        value['resume_confirmed']=resume.get()
        result.append(value);dialog.destroy()
    buttons=ttk.Frame(panel);buttons.pack(fill='x',pady=(10,0))
    ttk.Button(buttons,text='Отмена',command=dialog.destroy).pack(side='left')
    ttk.Button(buttons,text='Запустить реальную торговлю',command=accept).pack(side='right')
    root.wait_window(dialog)
    return result[0] if result else None

def start_native():
    if busy[0] or ready[0] is None or session[0] is not None:return
    target,folder,token,address,installation,build=ready[0]
    if build is None:
        messagebox.showwarning('Советник не проверен','Откройте журнал и устраните причину подготовки EA. Торговля не запущена.');return
    settings=native_settings(installation)
    if not settings:return
    busy[0]=True;prepare_button.config(state='disabled');connect_button.config(state='disabled');native_button.config(state='disabled')
    status.set('Проверяю фактический режим и счёт MT5 перед запуском…')
    def work():
        obj=None
        try:
            from native_launch import NativeLaunchSession
            obj=NativeLaunchSession(target,assets)
            obj.start(address,token,folder,terminal=installation.terminal,expected_data_path=installation.data_dir,owner_confirmed=True,**settings)
            session[0]=obj;native_mode[0]=True
            events.put(('native_started',None))
        except Exception as exc:
            if obj is not None and obj.children:
                session[0]=obj;native_mode[0]=True;events.put(('stop_error',str(exc)))
            else:events.put(('error',str(exc)))
    threading.Thread(target=work,daemon=False).start()

def stop():
    if busy[0] or session[0] is None:return
    busy[0]=True;stop_button.config(state='disabled')
    status.set('Остановка… Уже выставленные ордера и позиции остаются в MT5.' if native_mode[0] else 'Остановка подключения…')
    def work():
        try:
            try:emit(session[0].logs())
            except OSError:pass
            session[0].stop();session[0]=None;native_mode[0]=False;events.put(('stopped',None))
        except Exception as exc:events.put(('stop_error',str(exc)))
    threading.Thread(target=work,daemon=False).start()
prepare_button=ttk.Button(controls,text='1. Подготовить всё',command=prepare);prepare_button.pack(side='left')
connect_button=ttk.Button(controls,text='2. Подключить веб-панель (без торговли)',command=connect,state='disabled');connect_button.pack(side='left',padx=8)
stop_button=ttk.Button(controls,text='Остановить подключение',command=stop,state='disabled');stop_button.pack(side='left')
native_button=ttk.Button(more,text='Запуск реальной торговли…',command=start_native,state='disabled');native_button.pack(side='left',padx=(0,8))
def read_logs():
    if session[0] is not None and not busy[0]:
        try:emit(session[0].logs() or 'Журнал пока пуст.\n')
        except OSError:emit('Не удалось прочитать журнал запущенного подключения.\n')
    else:emit('Подключение не запущено; предыдущие сообщения остаются выше.\n')
ttk.Button(more,text='Журнал подключения',command=read_logs).pack(side='left',padx=(0,8))
def open_ea_folder():
    if ready[0] is None or ready[0][5] is None:return
    try:os.startfile(str(ready[0][5].executable.parent))
    except OSError:messagebox.showwarning('Папка советника','Не удалось открыть папку. Точный путь есть в журнале.')
ea_folder_button=ttk.Button(more,text='Папка советника',command=open_ea_folder,state='disabled');ea_folder_button.pack(side='left')

def poll():
    while True:
        try:kind,value=events.get_nowait()
        except queue.Empty:break
        if kind=='log':log.insert('end',value);log.see('end');continue
        busy[0]=False
        if kind in ('ready','ready_running','prepared_error'):
            problem=None
            if kind=='prepared_error':value,problem=value
            ready[0]=value;key[0]=value[2];url.set('https://'+value[3]+'/panel/');data.set(str(value[4].data_dir));key_button.config(state='normal')
            ea_folder_button.config(state='normal' if value[5] is not None else 'disabled')
            if kind=='ready_running':kind='connected'
            else:
                connect_button.config(state='normal');prepare_button.config(state='normal');native_button.config(state='normal')
                status.set('Подготовлено. Нажмите «Подключить веб-панель».' if value[5] else 'Готово для проверки связи. EA не подтверждён: см. журнал.')
                if problem:status.set('Подготовлено, но подключение не запущено: '+problem);emit(problem+'\n')
                continue
        if kind=='native_started':
            failed_child[0]=False;stop_button.config(state='normal',text='Остановить торговый процесс');status.set('Реальный режим запущен. Проверьте режим MT5 в веб-панели. Остановка не отменяет ордера.')
        elif kind=='connected':
            failed_child[0]=False;native_button.config(state='disabled');stop_button.config(state='normal',text='Остановить подключение');status.set('Откройте адрес веб-панели в браузере. Это проверка HTTPS без торговли.')
        elif kind=='stopped':
            stop_button.config(state='disabled',text='Остановить подключение');connect_button.config(state='normal');prepare_button.config(state='normal');native_button.config(state='normal' if ready[0] else 'disabled');status.set('Процесс остановлен. Остановка не отменяет существующие ордера.')
        elif kind=='stop_error':failed_child[0]=True;stop_button.config(state='normal',text='Повторить остановку');status.set('Остановка не подтверждена: '+value);emit(value+'\n')
        else:
            status.set('Ошибка: '+value);log.insert('end','Ошибка: '+value+'\n');log.see('end')
            prepare_button.config(state='normal');connect_button.config(state='normal' if ready[0] else 'disabled')
            native_button.config(state='normal' if ready[0] else 'disabled')
    if not busy[0] and not failed_child[0] and session[0] is not None and any(c.poll() is not None for c in session[0].children):
        failed_child[0]=True;emit('Один процесс завершился. Останавливаю его пару; проверьте журнал.\n');stop()
    root.after(500,poll)
def close():
    if busy[0] or session[0] is not None:messagebox.showinfo('Сначала остановите','Дождитесь завершения или нажмите «Остановить подключение».')
    else:root.destroy()
root.protocol('WM_DELETE_WINDOW',close);root.after(500,poll);root.mainloop()
