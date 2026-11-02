"""Windows HTTPS setup. No trading imports; mutations require a reviewed plan.
The inventory and command adapter are injectable so tests never control real services.
"""
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import re
import subprocess
import time
import urllib.request
import urllib.error
import uuid
import zipfile

SERVICE = 'AIImpulsHTTPS'
VERSION = '2.11.7'
ARCHIVE_SHA256 = '0a1edc0b799512051c57071ce0e798d3f2cf67dc98171366f1d2b326072e3b06'
DOWNLOAD = f'https://github.com/caddyserver/caddy/releases/download/v{VERSION}/caddy_{VERSION}_windows_amd64.zip'

class HttpsError(Exception): pass

def digest(data): return hashlib.sha256(data).hexdigest()
def canonical(value): return json.dumps(value, sort_keys=True, ensure_ascii=True).encode()
def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def save(path, value):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with tmp.open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,indent=2); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)

def origin(address):
    try: ip=ipaddress.ip_address(address.strip())
    except ValueError: raise HttpsError('Введите публичный IP VPS без https:// и порта.') from None
    if not ip.is_global: raise HttpsError('Нужен публичный IP VPS.')
    return 'https://'+('['+str(ip)+']' if ip.version==6 else str(ip))

def arguments(command):
    # Accept only simple, fully quoted absolute executable/config paths. No wrappers.
    tokens=re.findall(r'"[^"\r\n]*"|[^ \t\r\n]+',command)
    return [t[1:-1] if t.startswith('"') and t.endswith('"') else t for t in tokens]

def old_identity(service):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',service.get('Name','')): raise HttpsError('Неподдерживаемое имя службы.')
    args=arguments(service.get('PathName',''))
    if len(args) not in (4,6) or args[1:3]!=['run','--config']:
        raise HttpsError('Неизвестный запуск старой службы. Поддерживается прямой caddy.exe run --config; обёртки требуют отдельной проверки.')
    if len(args)==6 and args[4:]!=['--adapter','caddyfile']: raise HttpsError('Неизвестные параметры службы.')
    exe,config=map(PureWindowsPath,(args[0],args[3]))
    parts=[x.lower() for x in config.parts]
    if not re.fullmatch(r'[A-Za-z]:',exe.drive) or not re.fullmatch(r'[A-Za-z]:',config.drive) or '..' in exe.parts or '..' in config.parts: raise HttpsError('Нужны локальные абсолютные пути без переходов к родительским папкам.')
    if not exe.is_absolute() or not config.is_absolute() or exe.name.lower()!='caddy.exe' or exe.parent!=config.parent:
        raise HttpsError('Не подтверждена связь исполняемого файла и конфигурации анализатора.')
    if len(parts)<4 or parts[-3:-1]!=['xauusds_strategy_analyzer','https_ip']:
        raise HttpsError('Служба не принадлежит ожидаемой папке старого анализатора.')
    if service.get('Name','').casefold()==SERVICE.casefold() or service.get('StartName')!='LocalSystem' or service.get('StartMode') not in ('Auto','Manual','Disabled'):
        raise HttpsError('Неизвестный владелец или режим службы; автоматическое удаление запрещено.')
    if service.get('Dependencies') or service.get('Dependents'):
        raise HttpsError('У службы есть зависимости; требуется ручная проверка.')
    if service.get('State') not in ('Running','Stopped'): raise HttpsError('Дождитесь устойчивого состояния службы.')
    return str(exe),str(config)

def relevant(snapshot):
    return {'services':sorted(snapshot['services'],key=lambda s:s['Name']),
            'listeners':sorted(snapshot['listeners'],key=lambda x:(x['port'],x['pid'],x['address']))}

def select_old(snapshot):
    candidates=[]
    for svc in snapshot['services']:
        try: old_identity(svc); candidates.append(svc)
        except HttpsError: pass
    if len(candidates)>1: raise HttpsError('Несколько служб анализатора: автоматический выбор запрещён.')
    return candidates[0] if candidates else None

def ports(snapshot, allowed_pid=0, include_backend=False):
    for row in snapshot['listeners']:
        if row['port'] in ((80,443,2020,8765) if include_backend else (80,443,2020)):
            if not allowed_pid or row['pid']!=allowed_pid or row['port']==2020:
                raise HttpsError('Порт '+str(row['port'])+' занят неподтверждённым процессом. Ничего не остановлено.')

def caddyfile(public_origin, storage):
    # No user text is inserted until parsed as an IP; JSON quoting protects Windows paths.
    origin(public_origin.removeprefix('https://').strip('[]'))
    return ('{\n admin 127.0.0.1:2020\n persist_config off\n storage file_system {\n  root '+json.dumps(str(storage).replace('\\','/'))+'\n }\n}\n'
            +public_origin+' {\n tls {\n  issuer acme https://acme-v02.api.letsencrypt.org/directory {\n   profile shortlived\n  }\n }\n'
            +' reverse_proxy 127.0.0.1:8765 {\n  header_up Host 127.0.0.1:8765\n }\n}\n')

INVENTORY = r"""
$ErrorActionPreference='Stop'
$services=@(Get-CimInstance Win32_Service | Where-Object { $_.PathName -match '(?i)caddy|https_ip' -or $_.Name -eq 'AIImpulsHTTPS' } | ForEach-Object {
 $s=$_; $g=Get-Service -Name $s.Name -ErrorAction Stop
 $p=$null; if($s.ProcessId -gt 0){$p=Get-CimInstance Win32_Process -Filter ("ProcessId="+$s.ProcessId) -ErrorAction Stop}
 [ordered]@{Name=$s.Name;PathName=$s.PathName;StartName=$s.StartName;StartMode=$s.StartMode;State=$s.State;ProcessId=[int]$s.ProcessId;
 Dependencies=@($g.ServicesDependedOn|ForEach-Object {$_.Name});Dependents=@($g.DependentServices|ForEach-Object {$_.Name});
 ProcessPath=if($p){$p.ExecutablePath}else{''};ProcessCommand=if($p){$p.CommandLine}else{''}}
})
$listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object {$_.LocalPort -in @(80,443,2020,8765)} | ForEach-Object {
 [ordered]@{port=[int]$_.LocalPort;pid=[int]$_.OwningProcess;address=$_.LocalAddress}
})
[ordered]@{services=$services;listeners=$listeners}|ConvertTo-Json -Depth 6 -Compress
"""

class Windows:
    def run(self,args, timeout=40):
        result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if result.returncode: raise HttpsError('Команда Windows не завершена: '+Path(args[0]).name+'. Состояние необходимо проверить; секреты не выводятся.')
        return result.stdout
    def inventory(self):
        raw=self.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                      '[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();'+INVENTORY])
        try: return json.loads(raw.decode('utf-8-sig'))
        except Exception: raise HttpsError('Не удалось проверить службы и порты.') from None
    def admin(self):
        if os.name!='nt': raise HttpsError('Настройка служб доступна только в Windows.')
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin(): raise HttpsError('Откройте установщик от имени администратора. Изменения не применены.')
    def sc(self,*args): return self.run(['sc.exe',*args])
    def wait(self,name,state):
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            svc=next((s for s in self.inventory()['services'] if s['Name']==name),None)
            if (state=='Absent' and svc is None) or (svc and svc['State']==state): return
            time.sleep(.25)
        raise HttpsError('Служба не подтвердила состояние '+state+'. Повторное действие запрещено до диагностики.')
    def backup_service(self,service,folder):
        # Registry export also preserves failure/recovery/custom service settings. Local only.
        self.run(['reg.exe','export','HKLM\\SYSTEM\\CurrentControlSet\\Services\\'+service['Name'],str(folder/'service.reg'),'/y'])
        (folder/'service-sddl.txt').write_bytes(self.sc('sdshow',service['Name']))
    def protect(self,folder):
        raw=self.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',
                      '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'])
        sid=raw.decode('ascii').strip()
        if not re.fullmatch(r'S-1-[0-9-]+',sid): raise HttpsError('Не удалось определить владельца резервной копии.')
        self.run(['icacls.exe',str(folder),'/inheritance:r','/grant:r',
                  '*'+sid+':(OI)(CI)F','*S-1-5-18:(OI)(CI)F','*S-1-5-32-544:(OI)(CI)F'])
    def fetch(self):
        request=urllib.request.Request(DOWNLOAD,headers={'User-Agent':'AI-Impuls-HTTPS-Setup'})
        with urllib.request.urlopen(request,timeout=30) as response:
            from urllib.parse import urlsplit
            target=urlsplit(response.url)
            if target.scheme!='https' or target.hostname not in ('github.com','release-assets.githubusercontent.com'):
                raise HttpsError('Неожиданный источник Caddy.')
            chunks=[]; size=0; deadline=time.monotonic()+90
            while True:
                chunk=response.read(65536)
                if not chunk: break
                chunks.append(chunk); size+=len(chunk)
                if size>64*1024*1024 or time.monotonic()>deadline: raise HttpsError('Загрузка Caddy превысила лимит.')
            data=b''.join(chunks)
        if len(data)>64*1024*1024 or digest(data)!=ARCHIVE_SHA256: raise HttpsError('Контрольная сумма Caddy не совпала.')
        return data

def verify_service_process(service):
    if service['State']=='Running':
        args=arguments(service['PathName']); actual=arguments(service.get('ProcessCommand',''))
        if not service.get('ProcessId') or not actual or [x.lower() for x in actual]!=[x.lower() for x in args] or str(PureWindowsPath(service.get('ProcessPath',''))).lower()!=str(PureWindowsPath(args[0])).lower():
            raise HttpsError('Процесс службы не соответствует её конфигурации.')

def backup_old(adapter,service,folder):
    exe,config=old_identity(service); verify_service_process(service)
    if not Path(exe).is_file() or not Path(config).is_file(): raise HttpsError('Не найдены оригинальные файлы анализатора. Удаление запрещено.')
    # Do not copy keys/certificate storage or emit file contents.
    data=Path(config).read_bytes()
    if len(data)>1024*1024: raise HttpsError('Слишком большой Caddyfile.')
    folder.mkdir(parents=True,exist_ok=False)
    (folder/'Caddyfile.original').write_bytes(data)
    save(folder/'service.json',service)
    adapter.backup_service(service,folder)
    return digest(data)

def same_plan(adapter,plan):
    now=adapter.inventory()
    if digest(canonical(relevant(now)))!=plan['inventory_hash']:
        raise HttpsError('Службы или порты изменились. Повторите диагностику; старый план отменён.')
    return now

class Setup:
    def __init__(self,root,adapter=None):
        self.root=Path(root); self.home=self.root/'https'; self.adapter=adapter or Windows()
        self.config=self.root/'data'/'service.json'; self.journal=self.home/'transaction.json'
    def prepare(self,address):
        public=origin(address); raw=self.config.read_bytes(); cfg=json.loads(raw.decode('utf-8-sig'))
        web=cfg.get('web',{})
        if not isinstance(web,dict) or 'web' not in cfg: raise HttpsError('Отсутствует объект web в настройках.')
        if web.get('host','127.0.0.1')!='127.0.0.1' or web.get('port',8765)!=8765 or not web.get('enabled',True):
            raise HttpsError('Мастер поддерживает включённую панель 127.0.0.1:8765. Настройки не изменены.')
        if 'password' in web and not isinstance(web['password'],str): raise HttpsError('web.password должен быть строкой.')
        password=os.environ.get('TRADFI_WEB_PASSWORD',web.get('password',''))
        if not isinstance(password,str) or len(password)<32 or not password.strip(): raise HttpsError('Для телефона задайте web.password минимум 32 символа в Блокноте; переменная окружения имеет приоритет.')
        try: password.encode('utf-8')
        except UnicodeError: raise HttpsError('Пароль содержит недопустимые символы.') from None
        if cfg.get('enable_trading') is not False: raise HttpsError('Перед настройкой HTTPS требуется enable_trading=false и остановленный процесс бота.')
        if self.journal.exists(): raise HttpsError('Уже есть установка/незавершённый переход HTTPS. Используйте диагностику или откат; повторная установка запрещена.')
        inventory=self.adapter.inventory()
        if any(s['Name'].casefold()==SERVICE.casefold() for s in inventory['services']): raise HttpsError('Имя новой службы уже занято. Автоматическое присвоение запрещено.')
        old=select_old(inventory)
        if old: verify_service_process(old)
        ports(inventory,old['ProcessId'] if old and old['State']=='Running' else 0)
        return {'format':1,'origin':public,'inventory_hash':digest(canonical(relevant(inventory))),
                'config_hash':digest(raw),'old':old,
                'caddyfile':caddyfile(public,self.home/'certificates')}
    def apply(self,plan,confirmed=False):
        if not confirmed: raise HttpsError('Применение не подтверждено.')
        self.adapter.admin()
        if self.journal.exists(): raise HttpsError('Переход уже начат; повтор запрещён.')
        if digest(self.config.read_bytes())!=plan['config_hash']: raise HttpsError('Настройки изменились. Подготовьте план заново.')
        snapshot=same_plan(self.adapter,plan)
        if any(x['port']==8765 for x in snapshot['listeners']): raise HttpsError('Сначала остановите процесс кнопкой установщика, затем повторите диагностику. Чужие процессы не останавливаются.')
        archive=self.adapter.fetch()
        if digest(archive)!=ARCHIVE_SHA256: raise HttpsError('Контрольная сумма Caddy не совпала.')
        with zipfile.ZipFile(io.BytesIO(archive)) as z:
            entry=z.getinfo('caddy.exe')
            if entry.file_size>128*1024*1024: raise HttpsError('Слишком большой исполняемый файл.')
            binary=z.read(entry)
        # Download can take time: revalidate immediately before the first local mutation.
        same_plan(self.adapter,plan)
        if digest(self.config.read_bytes())!=plan['config_hash']: raise HttpsError('Настройки изменились во время загрузки.')
        self.home.mkdir(parents=True,exist_ok=True)
        if any(self.home.iterdir()): raise HttpsError('Папка HTTPS уже содержит файлы; автоматическая перезапись запрещена.')
        self.adapter.protect(self.home)
        old=plan['old']; backup=self.home/'backup'
        if old: backup_old(self.adapter,old,backup)
        else: backup.mkdir()
        raw=self.config.read_bytes(); (backup/'service.original.json').write_bytes(raw)
        exe=self.home/'caddy.exe'; exe.write_bytes(binary)
        config=self.home/'Caddyfile'; config.write_text(plan['caddyfile'],encoding='utf-8')
        command=subprocess.list2cmdline([str(exe),'run','--config',str(config),'--adapter','caddyfile'])
        tx={'format':1,'stage':'prepared','old':old,'command':command,'origin':plan['origin'],
            'config_before':digest(raw),'config_after':None,'binary_hash':digest(binary)}
        save(self.journal,tx)
        # Parsing only: no provisioning, sockets or certificate issuance.
        self.adapter.run([str(exe),'adapt','--config',str(config),'--adapter','caddyfile'])
        if old:
            same_plan(self.adapter,plan)
            if Path(old_identity(old)[1]).read_bytes()!=(backup/'Caddyfile.original').read_bytes(): raise HttpsError('Старый Caddyfile изменился; переход остановлен.')
            tx['stage']='stopping_old'; save(self.journal,tx)
            if old['State']=='Running': self.adapter.sc('stop',old['Name']); self.adapter.wait(old['Name'],'Stopped')
            if Path(old_identity(old)[1]).read_bytes()!=(self.home/'backup'/'Caddyfile.original').read_bytes(): raise HttpsError('Старый Caddyfile изменён; запуск запрещён.')
            self.adapter.sc('config',old['Name'],'start=','disabled')
        ports(self.adapter.inventory(),include_backend=True)
        if digest(self.config.read_bytes())!=plan['config_hash']: raise HttpsError('Настройки изменились. Не перезаписываем их; требуется диагностика/откат.')
        cfg=json.loads(raw.decode('utf-8-sig')); cfg['web']['public_origin']=plan['origin']
        # Journal intended bytes BEFORE replacement for crash-safe CAS rollback.
        after=(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode()
        tx['config_after']=digest(after); tx['stage']='configuring'; save(self.journal,tx)
        temp=self.config.with_suffix('.https.tmp'); temp.write_bytes(after); os.replace(temp,self.config)
        tx['stage']='creating_service'; save(self.journal,tx)
        self.adapter.sc('create',SERVICE,'start=','auto','binPath=',command)
        tx['stage']='starting_service'; save(self.journal,tx)
        self.adapter.sc('start',SERVICE); self.adapter.wait(SERVICE,'Running')
        tx['stage']='installed'; save(self.journal,tx)
        return 'HTTPS служба запущена. Выпуск сертификата и доступ с телефона ещё нужно проверить. Запустите READ_ONLY; реальные сделки не включены.'
    def rollback(self,confirmed=False):
        if not confirmed: raise HttpsError('Откат не подтверждён.')
        self.adapter.admin(); tx=read(self.journal)
        if tx['stage']=='rolled_back': raise HttpsError('Откат уже выполнен.')
        current=digest(self.config.read_bytes())
        if current not in (tx['config_before'],tx['config_after']): raise HttpsError('service.json изменён после настройки. Автоматический откат запрещён.')
        snapshot=self.adapter.inventory()
        if any(x['port']==8765 for x in snapshot['listeners']): raise HttpsError('Сначала остановите процесс бота кнопкой установщика.')
        own=next((s for s in snapshot['services'] if s['Name'].casefold()==SERVICE.casefold()),None)
        if own:
            if own['PathName']!=tx['command'] or own['StartName']!='LocalSystem': raise HttpsError('Новая служба изменилась; откат запрещён.')
            verify_service_process(own)
            if own['State']=='Running': self.adapter.sc('stop',SERVICE); self.adapter.wait(SERVICE,'Stopped')
            elif own['State']!='Stopped': raise HttpsError('Служба меняет состояние; повторите диагностику.')
            self.adapter.sc('config',SERVICE,'start=','disabled')
        ports(self.adapter.inventory(),include_backend=True)
        backup=self.home/'backup'/'service.original.json'
        if digest(backup.read_bytes())!=tx['config_before']: raise HttpsError('Резервная конфигурация повреждена.')
        self.config.write_bytes(backup.read_bytes())
        old=tx['old']
        if old:
            actual=next((s for s in self.adapter.inventory()['services'] if s['Name']==old['Name']),None)
            if not actual or actual['PathName']!=old['PathName'] or actual['StartName']!=old['StartName']: raise HttpsError('Старая служба изменилась. Её запуск запрещён.')
            self.adapter.sc('config',old['Name'],'start=',{'Auto':'auto','Manual':'demand','Disabled':'disabled'}[old['StartMode']])
            if old['State']=='Running': self.adapter.sc('start',old['Name']); self.adapter.wait(old['Name'],'Running')
        tx['stage']='rolled_back'; save(self.journal,tx)
        return 'Настройки восстановлены. Новая служба остановлена и отключена, файлы сохранены. READ_ONLY запускается отдельно.'
    def diagnose(self,address):
        public=origin(address); snapshot=self.adapter.inventory()
        # Native TLS verification only. No credentials/cookies, no redirect to another site.
        status='TLS/маршрут ещё не проверен'
        try:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*args,**kwargs): return None
            with urllib.request.build_opener(NoRedirect).open(public+'/',timeout=8) as response:
                body=response.read(65536)
                status='TLS доверен Windows; HTTP '+str(response.status)+('. Получена панель бота.' if b'panel.js' in body else '. Принадлежность страницы боту не подтверждена.')
        except urllib.error.HTTPError as exc:
            status='TLS доверен Windows; HTTP '+str(exc.code)+'. Маршрут к панели не подтверждён. Проверьте public_origin/Host и прежнюю службу.'
        except Exception: status='HTTPS не подтверждён: проверьте сертификат, службу, 80/443 и public_origin. Не обходите предупреждения TLS.'
        return {'services':[{'name':s['Name'],'state':s['State']} for s in snapshot['services']], 'listeners':snapshot['listeners'],'https':status}

class OldRemoval:
    """Standalone old analyzer service removal. Does not know any bot config or broker."""
    def __init__(self,backup_root,adapter=None):
        self.root=Path(backup_root); self.adapter=adapter or Windows()
    def prepare(self):
        snapshot=self.adapter.inventory(); old=select_old(snapshot)
        if not old:
            ports(snapshot)
            raise HttpsError('Подтверждённая старая служба не найдена. Ничего не удалено.')
        verify_service_process(old)
        ports(snapshot,old['ProcessId'] if old['State']=='Running' else 0)
        return {'old':old,'inventory_hash':digest(canonical(relevant(snapshot)))}
    def remove(self,plan,confirmed=False):
        if not confirmed: raise HttpsError('Удаление не подтверждено.')
        self.adapter.admin(); same_plan(self.adapter,plan)
        old=plan['old']; old_identity(old)
        folder=self.root/('backup-'+uuid.uuid4().hex)
        self.root.mkdir(parents=True,exist_ok=True); self.adapter.protect(self.root)
        backup_old(self.adapter,old,folder)
        save(folder/'operation.json',{'state':'prepared','old':old})
        same_plan(self.adapter,plan)
        if Path(old_identity(old)[1]).read_bytes()!=(folder/'Caddyfile.original').read_bytes(): raise HttpsError('Caddyfile изменился после резервного копирования. Удаление остановлено.')
        save(folder/'operation.json',{'state':'stopping','old':old})
        if old['State']=='Running': self.adapter.sc('stop',old['Name']); self.adapter.wait(old['Name'],'Stopped')
        actual=next((s for s in self.adapter.inventory()['services'] if s['Name']==old['Name']),None)
        if not actual or actual['PathName']!=old['PathName'] or actual['State']!='Stopped': raise HttpsError('Служба изменилась перед удалением. Проверьте резервную копию; повтор запрещён.')
        save(folder/'operation.json',{'state':'deleting','old':old})
        self.adapter.sc('delete',old['Name']); self.adapter.wait(old['Name'],'Absent')
        save(folder/'operation.json',{'state':'removed','old':old})
        try: ports(self.adapter.inventory())
        except HttpsError: raise HttpsError('Старая служба удалена, но порты заняты другим процессом. Он не остановлен. Резервная копия: '+str(folder)) from None
        return folder
    def restore(self,folder,confirmed=False):
        if not confirmed: raise HttpsError('Восстановление не подтверждено.')
        self.adapter.admin(); folder=Path(folder)
        old=read(folder/'service.json'); exe,config=old_identity(old)
        if not Path(exe).is_file() or not Path(config).is_file(): raise HttpsError('Оригинальные файлы анализатора отсутствуют. Восстановление запрещено.')
        if Path(config).read_bytes()!=(folder/'Caddyfile.original').read_bytes(): raise HttpsError('Конфигурация анализатора изменена. Автоматическая перезапись запрещена.')
        operation=read(folder/'operation.json')
        if operation.get('old')!=old or operation.get('state')=='restored': raise HttpsError('Копия уже восстановлена или не соответствует операции.')
        snapshot=self.adapter.inventory(); ports(snapshot)
        actual=next((s for s in snapshot['services'] if s['Name']==old['Name']),None)
        if actual and (actual['PathName']!=old['PathName'] or actual['StartName']!='LocalSystem' or actual['State']!='Stopped'):
            raise HttpsError('Имя службы уже занято или служба работает. Восстановление запрещено.')
        registry=(folder/'service.reg').read_text(encoding='utf-16')
        prefix='HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Services\\'+old['Name']
        sections=re.findall(r'^\[(.*)\]$',registry,re.M)
        if not sections or any(x!=prefix and not x.startswith(prefix+'\\') for x in sections): raise HttpsError('Небезопасная резервная копия реестра. Импорт запрещён.')
        security=(folder/'service-sddl.txt').read_text(encoding='utf-8-sig').strip()
        lines=[x.strip() for x in security.splitlines() if x.strip().startswith(('D:','O:'))]
        if len(lines)!=1: raise HttpsError('ACL резервной копии не распознан. Восстановление запрещено.')
        # The backup is scoped to one verified service; never import arbitrary registry keys.
        if not actual: self.adapter.sc('create',old['Name'],'start=','disabled','binPath=',old['PathName'])
        self.adapter.run(['reg.exe','import',str(folder/'service.reg')])
        self.adapter.sc('sdset',old['Name'],lines[0])
        self.adapter.sc('config',old['Name'],'start=',{'Auto':'auto','Manual':'demand','Disabled':'disabled'}[old['StartMode']])
        if old['State']=='Running': self.adapter.sc('start',old['Name']); self.adapter.wait(old['Name'],'Running')
        save(folder/'operation.json',{'state':'restored','old':old})


def wizard(manager):
    """GUI actions are explicit and use the same operation/account/state locks as installer."""
    import tkinter as tk
    from tkinter import messagebox,ttk
    from install_tradfi import lock,account_lock,state_lock
    window=tk.Toplevel(); window.title('HTTPS для телефона'); window.geometry('830x620')
    address=tk.StringVar(); plan=None
    ttk.Label(window,text='Публичный IP VPS (без https://). Сначала остановите процесс бота кнопкой установщика.').pack(padx=12,pady=8)
    ttk.Entry(window,textvariable=address,width=45).pack()
    output=tk.Text(window,wrap='word',height=22); output.pack(fill='both',expand=True,padx=12,pady=8)
    setup=Setup(manager.root)
    def show(value): output.delete('1.0','end'); output.insert('end',value)
    def guarded(action):
        try:
            with lock(manager.root/'operation.lock'),account_lock(manager.root,manager.config()),state_lock(manager.root,manager.config()):
                return action()
        except HttpsError as exc: messagebox.showerror('HTTPS',str(exc),parent=window)
        except Exception: messagebox.showerror('HTTPS','Операция не завершена. Выполните диагностику. Журнал перехода сохранён; повторное применение запрещено. Секреты не выводятся.',parent=window)
    def prepare():
        nonlocal plan
        plan=None
        def task():
            nonlocal plan
            plan=setup.prepare(address.get())
            old=plan['old']
            show('ПЛАН, ещё не применён:\n'+plan['origin']+' → 127.0.0.1:8765\n'
                 +'Пароль 32+, Secure cookie. Торговля не включается.\n'
                 +('Остановить и отключить автозапуск старой службы '+old['Name']+'\nФайлы: '+old['PathName']+'\n' if old else 'Старая служба не обнаружена.\n')
                 +'Резервная копия: '+str(setup.home/'backup')+'\n'
                 +'Скачать проверенный Caddy '+VERSION+', создать службу '+SERVICE+' с автозапуском.\n'
                 +'Изменить только web.public_origin. После применения нажмите READ_ONLY в установщике.\n'
                 +'Порты 80/443 должны быть доступны извне; firewall мастер не меняет.\n'
                 +'Сертификат выпускается и продлевается Caddy автоматически, пока служба работает.\n'
                 +'Старые папки и сертификаты не удаляются.\n\n'+plan['caddyfile'])
        guarded(task)
    def apply():
        if not plan: messagebox.showinfo('HTTPS','Сначала подготовьте план.',parent=window); return
        if not messagebox.askyesno('Применить план?', 'Подтверждаете показанный переход служб, резервное копирование и изменение public_origin?\nПроцесс бота должен быть остановлен. Никакие сделки не запускаются.',parent=window): return
        def task(): show(setup.apply(plan,confirmed=True)+'\n\nЗакройте мастер и нажмите READ_ONLY в установщике, затем откройте указанный HTTPS адрес на телефоне.')
        guarded(task)
    def diagnose():
        try: show(json.dumps(setup.diagnose(address.get()),ensure_ascii=False,indent=2))
        except Exception: messagebox.showerror('Диагностика','Не удалось проверить службы/порты. Нужны Windows и права администратора.',parent=window)
    def rollback():
        if messagebox.askyesno('Откат HTTPS','Остановить новую службу и восстановить конфигурацию/автозапуск прежней? Бот должен быть остановлен. Файлы и сертификаты сохраняются.',parent=window):
            guarded(lambda:show(setup.rollback(confirmed=True)))
    for label,action in [('1. Подготовить план',prepare),('2. Применить',apply),('Диагностика HTTPS',diagnose),('Откат HTTPS',rollback)]:
        ttk.Button(window,text=label,command=action).pack(side='left',padx=6,pady=10)


def removal_gui():
    import tkinter as tk
    from tkinter import messagebox,ttk,filedialog
    app=tk.Tk(); app.title('Старая служба анализатора — удаление'); app.geometry('800x520')
    root=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'AIImpuls_OldHTTPS_Backups'
    removal=OldRemoval(root); plan=None
    output=tk.Text(app,wrap='word'); output.pack(fill='both',expand=True,padx=12,pady=12)
    def show(text): output.delete('1.0','end'); output.insert('end',text)
    def diagnose():
        nonlocal plan
        plan=None
        try:
            plan=removal.prepare(); old=plan['old']
            show('Будет удалена ТОЛЬКО служба: '+old['Name']+'\n'+old['PathName']+'\nСостояние: '+old['State']+
                 '\nРезервная копия: '+str(root)+'\nФайлы анализатора, сертификаты, MT5, firewall и новая служба бота не удаляются.\n\nДля удаления требуется отдельное подтверждение. Пока ничего не изменено.')
        except HttpsError as exc: show(str(exc))
        except Exception: show('Диагностика недоступна. Запустите файл от имени администратора. Ничего не удалено.')
    def remove():
        if not plan: messagebox.showinfo('Удаление','Сначала диагностика.'); return
        if not messagebox.askyesno('Удалить старую службу?', 'Остановить и удалить службу '+plan['old']['Name']+' из показанного каталога анализатора? Сначала будет создана локальная резервная копия.'): return
        try:
            folder=removal.remove(plan,confirmed=True)
            show('Старая служба удалена. Порты 80/443/2020 свободны на момент проверки.\nРезервная копия: '+str(folder)+'\nПри необходимости: Восстановить → выбрать эту папку. Старые папки пока не удаляйте.')
        except HttpsError as exc: show(str(exc)+'\nПри частичном отказе не повторяйте удаление: проверьте диагностику и резервную копию в '+str(root))
        except Exception: show('Операция не завершена. Возможен частичный результат. Проверьте диагностику и резервную копию: '+str(root))
    def restore():
        folder=filedialog.askdirectory(initialdir=str(root),title='Выберите backup-...')
        if folder and messagebox.askyesno('Восстановить','Восстановить старую службу из выбранной локальной резервной копии? Порты должны быть свободны.'):
            try: removal.restore(folder,confirmed=True); show('Служба восстановлена. Проверьте HTTPS отдельно.')
            except HttpsError as exc: show(str(exc))
            except Exception: show('Восстановление не завершено. Не запускайте службы повторно до диагностики.')
    for label,fn in [('1. Диагностика',diagnose),('2. Удалить старую службу',remove),('Восстановить',restore)]: ttk.Button(app,text=label,command=fn).pack(side='left',padx=8,pady=12)
    show('Начните с диагностики. Никакие действия не выполняются при открытии окна. Нужны Windows, Python 3.13 с Tk и права администратора для изменения службы.')
    app.mainloop()

if __name__=='__main__':
    import sys
    if sys.argv[1:]==['--remove-old']: removal_gui()
    else: raise SystemExit('Откройте HTTPS мастер через install_tradfi.py.')
