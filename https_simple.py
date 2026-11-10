"""Linear local-ZIP installer. No broker imports or automatic bot startup."""
import contextlib
import os
from pathlib import Path
import sys
import https_setup as h

FIREWALL = r"""
$ErrorActionPreference='Stop'
$group='AI Impuls HTTPS managed v1'
$specs=@(@('AIImpulsHTTPS-HTTP','80','Allow'),@('AIImpulsHTTPS-TLS','443','Allow'),@('AIImpulsHTTPS-Backend','8765','Block'))
# Validate ALL existing names before any change. Never replace foreign rules.
foreach($s in $specs){
 $r=@(Get-NetFirewallRule -Name $s[0] -ErrorAction SilentlyContinue)
 if($r.Count -gt 1){throw 'Ambiguous rule'}
 if($r.Count -eq 1){
  $p=@($r[0] | Get-NetFirewallPortFilter)
  $a=@($r[0] | Get-NetFirewallAddressFilter)
  $app=@($r[0] | Get-NetFirewallApplicationFilter)
  $svc=@($r[0] | Get-NetFirewallServiceFilter)
  if($r[0].Group -ne $group -or $r[0].Direction -ne 'Inbound' -or $r[0].Action -ne $s[2] -or $r[0].Enabled -ne 'True' -or $r[0].Profile -ne 'Any' -or $r[0].PolicyStoreSourceType -ne 'Local' -or $p.Count -ne 1 -or $p[0].Protocol -ne 'TCP' -or [string]$p[0].LocalPort -ne $s[1] -or [string]$p[0].RemotePort -ne 'Any' -or $a.Count -ne 1 -or [string]$a[0].LocalAddress -ne 'Any' -or [string]$a[0].RemoteAddress -ne 'Any' -or $app.Count -ne 1 -or $app[0].Program -ne 'Any' -or $svc.Count -ne 1 -or $svc[0].Service -ne 'Any'){throw 'Foreign or modified firewall rule'}
 }
}
foreach($s in $specs){
 if(-not (Get-NetFirewallRule -Name $s[0] -ErrorAction SilentlyContinue)){
  New-NetFirewallRule -Name $s[0] -DisplayName $s[0] -Group $group -Direction Inbound -Action $s[2] -Enabled True -Profile Any -Protocol TCP -LocalPort $s[1] | Out-Null
 }
}
"""

class SimpleWindows(h.Windows):
    def validate_firewall(self):
        code=FIREWALL[:FIREWALL.index("foreach($s in $specs){\n if(-not")]
        self.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',code])
    def firewall(self):
        self.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',FIREWALL])

@contextlib.contextmanager
def exclusive(root):
    """OS lock released even on crash; never delete another process's lock file."""
    import msvcrt
    with (Path(root)/'https-simple.lock').open('a+b') as f:
        if f.tell()==0:f.write(b'0');f.flush()
        f.seek(0)
        try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:raise h.HttpsError('Другой простой HTTPS установщик уже работает.') from None
        try:yield
        finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

class Installer:
    def __init__(self,root,adapter=None):
        self.s=h.Setup(root,adapter or SimpleWindows())
        self.stage='preflight'
    def emit(self,stage):
        self.stage=stage
        print('Этап: '+stage,flush=True)
    def install(self,address,confirmed=False):
        if not confirmed:raise h.HttpsError('Установка отменена; изменений нет.')
        s=self.s;s.adapter.admin();h.origin(address)
        if s.local_archive is None:raise h.HttpsError('Сначала выберите официальный ZIP.')
        self.emit('preflight')
        s.validate_installation()
        for path in (s.root,s.root/'data',s.config):
            if path.is_symlink() or getattr(path,'is_junction',lambda:False)():raise h.HttpsError('Путь установки содержит ссылку; установка остановлена.')
        if s.home.exists() or s.attempt_path.exists():
            raise h.HttpsError('Обнаружена предыдущая попытка HTTPS. Этот установщик предназначен для чистой установки: ничего не удалено и не восстановлено. Нужна диагностика существующей установки.')
        snapshot=s.adapter.inventory()
        if snapshot['services']:raise h.HttpsError('Найдена существующая HTTPS служба. Чистая установка остановлена; чужие службы не изменены.')
        h.ports(snapshot,include_backend=True)
        plan=s.prepare(address)
        if plan['old'] is not None:raise h.HttpsError('Найдена старая служба. Автоматический переход запрещён.')
        self.emit('firewall_check');s.adapter.validate_firewall()
        self.emit('install');s.apply(plan,confirmed=True)
        self.emit('firewall');s.adapter.firewall()
        self.emit('complete')
        return 'Служба и правила Windows настроены. Бот НЕ запущен. Запустите READ_ONLY и проверьте HTTPS с телефона. Выдача сертификата ещё может ожидать проверки сети.'

def main():
    import tkinter as tk
    from tkinter import filedialog,messagebox,simpledialog
    app=tk.Tk();app.withdraw()
    root=Path(os.environ.get('LOCALAPPDATA',''))/'AI_IMPULS_LIVE'
    runner=Installer(root)
    try:
        runner.s.adapter.admin()
        if not runner.s.config.is_file():raise h.HttpsError('Сначала установите бот и сохраните настройки через установщик под этим пользователем Windows.')
        address=simpledialog.askstring('HTTPS для телефона','Публичный IP этого VPS (без https://):',parent=app)
        if not address:return 0
        public=h.origin(address)
        path=filedialog.askopenfilename(parent=app,title='Официальный caddy_2.11.7_windows_amd64.zip',filetypes=[('Caddy ZIP','*.zip')])
        if not path:return 0
        runner.s.select_local_zip(path)
        if not messagebox.askyesno('Подтверждение HTTPS',
            'Остановите READ_ONLY и закройте старый HTTPS мастер.\n\n'+public+'\n'
            'Будут настроены public_origin, служба AIImpulsHTTPS и правила Windows: входящие TCP 80/443 разрешить, 8765 заблокировать.\n'
            'При существующей службе или незавершённой установке программа остановится. Чужие службы, сертификаты и данные не удаляются. Бот и торговля не запускаются.\n\nПродолжить?',parent=app):return 0
        with exclusive(root):result=runner.install(address,True)
        print(result)
        report=runner.s.diagnose(address)
        h.save(root/'https-simple-diagnostic.json',report)
        messagebox.showinfo('HTTPS',result+'\n'+report['https']+'\nОтчёт: https-simple-diagnostic.json в папке AI_IMPULS_LIVE.',parent=app)
        return 0
    except Exception as exc:
        # Known safety messages contain no credentials; arbitrary native errors never printed.
        text=str(exc) if isinstance(exc,h.HttpsError) else h.safe_failure(exc,runner.stage)['reason']
        print('Этап: '+runner.stage+'\n'+text)
        try:h.save(root/'https-simple-diagnostic.json',dict(runner.s.local_report(),simple_stage=runner.stage,error=text))
        except Exception:print('Отчёт не удалось сохранить.')
        messagebox.showerror('HTTPS — остановлено','Этап: '+runner.stage+'\n'+text+'\nНе удаляйте файлы и службу. Сохранённые данные нужны для сверки.',parent=app)
        return 1
    finally:app.destroy()

if __name__=='__main__':sys.exit(main())
