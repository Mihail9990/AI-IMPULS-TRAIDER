"""Local-only Telegram settings. Secrets are never included in service.json or HTTP.

Windows DPAPI binds the encrypted file to the current Windows user. This is not a
backup/transfer format: re-enter credentials after changing Windows user or VPS.
"""
import ctypes
from contextlib import closing
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

NAME = 'telegram-credentials.dpapi'
MAX_FILE = 16384

class TelegramSettingsError(ValueError):
    pass


def _crypt(data, decrypt=False):
    if os.name != 'nt':
        raise TelegramSettingsError('Защищённые настройки Telegram доступны только в Windows.')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN, current-user scope (not machine-wide).
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise TelegramSettingsError('Не удалось защитить или прочитать настройки Telegram.')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def path_for(runtime_path):
    return Path(runtime_path).resolve().parent / NAME


def validate(value):
    if not isinstance(value, dict) or set(value) != {'enabled', 'bot_token', 'chat_id'} or type(value['enabled']) is not bool:
        raise TelegramSettingsError('Некорректные настройки Telegram.')
    token, chat = value['bot_token'], value['chat_id']
    if not isinstance(token, str) or not isinstance(chat, str):
        raise TelegramSettingsError('Некорректные настройки Telegram.')
    if (token or chat or value['enabled']) and (not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token) or not re.fullmatch(r'[1-9]\d*', chat)):
        raise TelegramSettingsError('Нужны токен бота и числовой ID личного чата.')
    return dict(value)


def load(runtime_path, *, decrypt=None):
    path = path_for(runtime_path)
    if path.is_symlink():
        raise TelegramSettingsError('Небезопасный файл настроек Telegram.')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise TelegramSettingsError('Некорректный файл настроек Telegram.')
    try:
        data = (decrypt or (lambda data: _crypt(data, True)))(path.read_bytes())
        return validate(json.loads(data.decode('utf-8')))
    except Exception:
        raise TelegramSettingsError('Не удалось прочитать защищённые настройки Telegram. Введите их заново на этом VPS.') from None


def save(runtime_path, value, *, encrypt=None):
    value = validate(value)
    path = path_for(runtime_path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise TelegramSettingsError('Небезопасный файл настроек Telegram.')
    # Never redirect an existing queue to a new recipient, even if currently empty.
    queue = Path(str(runtime_path) + '.telegram-outbox.sqlite3')
    if queue.exists() and value['enabled']:
        if queue.is_symlink() or not queue.is_file():
            raise TelegramSettingsError('Небезопасная очередь Telegram.')
        try:
            with closing(sqlite3.connect(queue.resolve().as_uri()+'?mode=ro', uri=True)) as db:
                db.execute('PRAGMA query_only=ON')
                row = db.execute('SELECT destination FROM delivery WHERE id=1').fetchone()
                if not row or row[0] != value['chat_id']:
                    raise ValueError()
        except Exception:
            raise TelegramSettingsError('Получатель существующей очереди отличается или очередь недоступна. Настройки не изменены.') from None
    try:
        encrypted = (encrypt or _crypt)(json.dumps(value).encode('utf-8'))
        if not encrypted or len(encrypted) > MAX_FILE:
            raise ValueError()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.telegram-settings-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(encrypted); out.flush(); os.fsync(out.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except Exception:
        raise TelegramSettingsError('Не удалось сохранить защищённые настройки Telegram.') from None


def dialog(parent, runtime_path):
    """Local installer UI; no network access, no startup/trading side effects."""
    import tkinter as tk
    from tkinter import ttk, messagebox
    try:
        previous = load(runtime_path)
    except TelegramSettingsError:
        previous = None
        messagebox.showwarning('Telegram', 'Сохранённые настройки не читаются. Можно ввести их заново.', parent=parent)
    previous = previous or {'enabled': False, 'bot_token': '', 'chat_id': ''}
    win = tk.Toplevel(parent); win.title('Настройки Telegram · логи .txt'); win.transient(parent); win.grab_set()
    enabled = tk.BooleanVar(value=previous['enabled'])
    token = tk.StringVar(); chat = tk.StringVar(value=previous['chat_id'])
    ttk.Label(win, text='Токен BotFather (пустое поле сохраняет прежний токен)').grid(row=0,column=0,padx=12,pady=6,sticky='w')
    entry=ttk.Entry(win,textvariable=token,show='•',width=48);entry.grid(row=1,column=0,padx=12,sticky='ew')
    ttk.Label(win,text='ID вашего личного чата с ботом').grid(row=2,column=0,padx=12,pady=6,sticky='w')
    ttk.Entry(win,textvariable=chat,width=48).grid(row=3,column=0,padx=12,sticky='ew')
    ttk.Checkbutton(win,text='Включить отправку логов в Telegram (авто и вручную)',variable=enabled).grid(row=4,column=0,padx=12,pady=10,sticky='w')
    ttk.Label(win,text='Автоматическая отправка каждые 10 минут.\nСначала напишите боту /start и проверьте ID получателя.\nВ Telegram уйдут сведения о сделках, без паролей и номеров счёта.\nРучная отправка остаётся в веб-панели. Изменения применятся\nпри следующем запуске сервиса. Токен хранится зашифрованным\nдля текущего пользователя Windows, не в service.json.',wraplength=440).grid(row=5,column=0,padx=12,pady=6,sticky='w')
    def close():
        token.set('');win.destroy()
    def apply():
        value={'enabled':enabled.get(),'bot_token':token.get().strip() or previous['bot_token'],'chat_id':chat.get().strip()}
        try:
            save(runtime_path,value)
        except TelegramSettingsError as exc:
            messagebox.showerror('Telegram',str(exc),parent=win);return
        close();messagebox.showinfo('Telegram','Настройки сохранены. Отправка начнётся после запуска сервиса; тестовое сообщение не отправлялось.',parent=parent)
    row=ttk.Frame(win);row.grid(row=6,column=0,pady=12)
    ttk.Button(row,text='Сохранить',command=apply).pack(side='left',padx=6)
    ttk.Button(row,text='Отмена',command=close).pack(side='left',padx=6)
    win.protocol('WM_DELETE_WINDOW',close);entry.focus_set()
