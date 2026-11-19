"""Old operator ZIP workflow, RECOVERY namespace, launch-owned Caddy only.
Does not execute Caddy, install services, set firewall/ACL, or touch MT5.
"""
import os
from pathlib import Path
import tempfile
import urllib.request
from archive_verifier import VERSION,DOWNLOAD,MAX_ARCHIVE,EXE_SHA256,HttpsError,validate_archive,digest


def prepare_caddy(server_root,archive=None,*,confirmed=False,opener=urllib.request.urlopen):
    if not confirmed:raise HttpsError('Подготовка не подтверждена; изменений нет.')
    server=Path(server_root).absolute()
    if server.name.casefold()!='ai_impuls_recovery_web':
        raise HttpsError('Нужна отдельная папка AI_IMPULS_RECOVERY_WEB, не каталог основного бота.')
    if not server.is_dir():raise HttpsError('Сначала подготовьте отдельную установку RECOVERY_WEB.')
    folder=server/'tools'/'caddy';target=folder/'caddy.exe'
    for item in [target,folder,*folder.parents]:
        if item.is_symlink() or (item.exists() and getattr(item.stat(),'st_file_attributes',0)&1024):
            raise HttpsError('Путь содержит ссылку или junction.')
    if target.exists():
        if not target.is_file() or digest(target.read_bytes())!=EXE_SHA256:
            raise HttpsError('Существующий Caddy отличается; не заменён.')
        return dict(status='existing_verified',version=VERSION,trading_started=False,caddy_started=False)
    source=Path(archive).open('rb') if archive else opener(DOWNLOAD,timeout=60)
    with source as stream:
        data=stream.read(MAX_ARCHIVE+1)
    binary=validate_archive(data)
    if digest(binary)!=EXE_SHA256:raise HttpsError('EXE не совпадает с ожидаемой версией RECOVERY.')
    folder.mkdir(parents=True,exist_ok=True)
    # Publish verified complete bytes exclusively. Never overwrite after a race.
    fd,name=tempfile.mkstemp(prefix='caddy-',suffix='.prepared',dir=folder)
    try:
        with os.fdopen(fd,'wb') as f:f.write(binary);f.flush();os.fsync(f.fileno())
        if os.name=='nt':os.rename(name,target)
        else:os.link(name,target)
    finally:
        Path(name).unlink(missing_ok=True)
    return dict(status='prepared',version=VERSION,trading_started=False,caddy_started=False)


def main():
    import tkinter as tk
    from tkinter import filedialog,messagebox
    app=tk.Tk();app.withdraw()
    try:
        server=Path(os.environ.get('LOCALAPPDATA',''))/'AI_IMPULS_RECOVERY_WEB'
        path=filedialog.askopenfilename(parent=app,title='Официальный Caddy '+VERSION+' Windows amd64 ZIP',filetypes=[('ZIP','*.zip')])
        if not path:return
        if not messagebox.askyesno('Подготовка Caddy для RECOVERY',
            'Будет проверен официальный ZIP и подготовлен Caddy только в отдельной AI_IMPULS_RECOVERY_WEB.\n'
            'Службы, firewall, ключи, сертификаты и основной бот не изменяются.\n'
            'Caddy и торговля не запускаются.\nПродолжить?',parent=app):return
        result=prepare_caddy(server,path,confirmed=True)
        messagebox.showinfo('Caddy подготовлен','Официальная версия проверена. Ничего не запущено.\n'
            'Дальнейший запуск выполняется только существующим RECOVERY-лаунчером.\n'
            'При занятом TCP 443 чужой Caddy не останавливайте.',parent=app)
    except Exception as exc:
        text=str(exc) if isinstance(exc,HttpsError) else 'Подготовка остановлена. Файлы и службы не заменены; сохраните состояние для проверки.'
        messagebox.showerror('Caddy — остановлено',text,parent=app)
    finally:app.destroy()
if __name__=='__main__':main()
