"""Independent PAPER HTTPS installer. Importing performs no external actions.
Windows mutations are behind an injected adapter and fresh confirmation callback.
"""
from functools import wraps
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import threading
import urllib.request
import urllib.error
import uuid

import https_setup as shared

SERVICE = 'AIImpulsPaperHTTPS'
RULE = 'AIImpulsPaperHTTPS-Inbound'
BACKEND = 8766


def public_origin(address):
    address = address.strip()
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        domain = address.encode('idna').decode('ascii').lower()
        labels = domain.split('.')
        if len(labels) < 2 or len(domain) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x) for x in labels) or labels[-1].isdigit():
            raise shared.HttpsError('Введите публичный IP или домен без протокола, порта и пути.') from None
        return 'https://' + domain
    if not ip.is_global:
        raise shared.HttpsError('Нужен публичный адрес.')
    return 'https://' + ('[' + str(ip) + ']' if ip.version == 6 else str(ip))


def caddyfile(origin, storage, http_challenge=False, instance_id=None):
    if instance_id is not None and not re.fullmatch(r"[0-9a-f]{32}", instance_id):
        raise shared.HttpsError("Неверный идентификатор PAPER.")
    if public_origin(origin.removeprefix('https://').strip('[]')) != origin:
        raise shared.HttpsError('Неверный адрес.')
    return ('{\n admin off\n persist_config off\n auto_https disable_redirects\n servers {\n  protocols h1 h2\n }\n'
            ' storage file_system {\n  root ' + json.dumps(str(storage).replace('\\', '/')) + '\n }\n}\n' + origin + ' {\n'
            ' tls {\n  issuer acme https://acme-v02.api.letsencrypt.org/directory {\n   profile shortlived\n'
            + ('' if http_challenge else '   disable_http_challenge\n') + '  }\n }\n'
            + (' header X-AIImpuls-Paper-Instance ' + instance_id + '\n' if instance_id else '') +
            ' reverse_proxy 127.0.0.1:8766 {\n  header_up Host 127.0.0.1:8766\n }\n}\n')


class Windows(shared.Windows):
    def inventory(self):
        script = shared.INVENTORY.replace("@(80,443,2020,8765)", "@(80,443,8766)")
        script = script.replace("$_.Name -eq 'AIImpulsHTTPS'", "$_.Name -in @('AIImpulsHTTPS','AIImpulsPaperHTTPS')")
        result = json.loads(self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script]).decode('utf-8-sig'))
        rules = self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "$ErrorActionPreference='Stop'; @((Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object {$_.Name -eq '" + RULE + "'}) | ForEach-Object {$_.Name}) | ConvertTo-Json -Compress"])
        value = json.loads(rules.decode('utf-8-sig') or '[]')
        result['firewall'] = [value] if isinstance(value, str) else value
        return result

    def firewall(self, ports, exe):
        # Numeric ports and exact executable scope; never a backend rule.
        if ports not in ([443], [80, 443]):
            raise shared.HttpsError('Недопустимые порты firewall.')
        quoted = str(exe).replace("'", "''")
        self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "$ErrorActionPreference='Stop'; New-NetFirewallRule -Name '" + RULE + "' -DisplayName 'PAPER HTTPS' -Group 'AIImpulsPaperHTTPS managed v1' -Direction Inbound -Action Allow -Protocol TCP -LocalPort " + ','.join(map(str, ports)) + " -Program '" + quoted + "' -Profile Any -EdgeTraversalPolicy Block | Out-Null"])

    def validate_firewall(self, ports, exe):
        quoted = str(exe).replace("'", "''")
        script = """$ErrorActionPreference='Stop';
$r=@(Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object {$_.Name -eq '%s'});
if($r.Count -ne 1){throw 'Rule identity mismatch'};
$p=@($r[0] | Get-NetFirewallPortFilter); $a=@($r[0] | Get-NetFirewallApplicationFilter);
$addr=@($r[0] | Get-NetFirewallAddressFilter); $svc=@($r[0] | Get-NetFirewallServiceFilter);
if($r[0].Group -ne 'AIImpulsPaperHTTPS managed v1' -or $r[0].Direction -ne 'Inbound' -or $r[0].Action -ne 'Allow' -or $r[0].Enabled -ne 'True' -or $r[0].Profile -ne 'Any' -or $r[0].PolicyStoreSourceType -ne 'Local' -or $r[0].EdgeTraversalPolicy -ne 'Block' -or $p.Count -ne 1 -or $p[0].Protocol -ne 'TCP' -or (($p[0].LocalPort | Sort-Object) -join ',') -ne '%s' -or [string]$p[0].RemotePort -ne 'Any' -or $a.Count -ne 1 -or $a[0].Program -ne '%s' -or $addr.Count -ne 1 -or [string]$addr[0].LocalAddress -ne 'Any' -or [string]$addr[0].RemoteAddress -ne 'Any' -or $svc.Count -ne 1 -or $svc[0].Service -ne 'Any'){throw 'Rule changed'}
""" % (RULE, ','.join(sorted(map(str, ports))), quoted)
        self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script])

    def remove_firewall(self):
        self.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
            "$ErrorActionPreference='Stop'; Remove-NetFirewallRule -Name '" + RULE + "'"])

    def verify_certificate(self, origin, instance_id):
        if not re.fullmatch(r"[0-9a-f]{32}", instance_id):
            raise shared.HttpsError("Неверный идентификатор PAPER.")
        if public_origin(origin.removeprefix('https://').strip('[]')) != origin:
            raise shared.HttpsError('Неверный HTTPS адрес.')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise shared.HttpsError('Перенаправление HTTPS запрещено.')
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        with opener.open(urllib.request.Request(origin), timeout=20) as response:
            if response.headers.get('X-AIImpuls-Paper-Instance') != instance_id:
                raise shared.HttpsError('HTTPS принадлежит другой установке.')
            body_bytes = response.read(2 * 1024 * 1024 + 1)
            if len(body_bytes) > 2 * 1024 * 1024:
                raise shared.HttpsError('Ответ HTTPS превысил лимит.')
            body = body_bytes.decode('utf-8')
            if response.url != origin and response.url != origin + '/':
                raise shared.HttpsError('Неожиданный адрес HTTPS.')
            if 'data-controller="native" data-role="paper"' not in body:
                raise shared.HttpsError('HTTPS не подтвердил страницу PAPER.')


def maintenance(fn):
    @wraps(fn)
    def guarded(self, *args, **kwargs):
        from independent_install import IndependentManager
        self.validate_root()
        with IndependentManager(self.root, 'paper').maintenance_guard():
            return fn(self, *args, **kwargs)
    return guarded


class Setup:
    def __init__(self, root, adapter=None):
        self.root = Path(root)
        self.home = self.root / 'paper-https'
        self.config = self.root / 'data' / 'service.json'
        self.journal = self.home / 'transaction.json'
        self.adapter = adapter or Windows()
        self.guard = threading.Lock()
        self.local_archive = None

    def validate_root(self):
        for path in (self.root, *self.root.parents, self.root / 'data', self.config, self.home):
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise shared.HttpsError('Ссылки и junction не поддерживаются.')
        if shared.read(self.root / 'application.json') != {'format': 1, 'role': 'paper'} or shared.read(self.root / 'managed-install.json') != {'format': 2, 'role': 'paper', 'layout': 'independent_roots_v1'} or (self.root / 'migration-pending.json').exists():
            raise shared.HttpsError('Нужна завершённая независимая установка PAPER.')
        cfg = shared.read(self.config)
        web = cfg.get('web', {})
        if cfg.get('enable_trading') is not False or cfg.get('backend') != 'paper_mt5_quotes_v1' or web.get('host') != '127.0.0.1' or web.get('port') != BACKEND or web.get('enabled') is not True:
            raise shared.HttpsError('Нужен PAPER без реальных сделок с панелью 127.0.0.1:8766.')
        return cfg

    def select_local_zip(self, path):
        self.local_archive = None
        if path:
            if str(path).startswith(('\\\\', '//')):
                raise shared.HttpsError('Нужен локальный официальный ZIP.')
            with Path(path).open('rb') as f:
                data = f.read(shared.MAX_ARCHIVE + 1)
            shared.validate_archive(data)
            self.local_archive = data

    def inventory(self):
        snapshot = self.adapter.inventory()
        if not isinstance(snapshot.get('firewall'), list):
            raise shared.HttpsError('Не удалось проверить firewall.')
        return snapshot

    def conflict_check(self, snapshot):
        if snapshot['services'] or snapshot['firewall']:
            raise shared.HttpsError('Обнаружена LIVE/PAPER или неизвестная HTTPS-служба/firewall. Ничего не отключаем.')
        if any(x['port'] in (80, 443, BACKEND) for x in snapshot['listeners']):
            raise shared.HttpsError('Порт занят. Остановите только PAPER через его окно; LIVE не изменяется.')

    def prepare(self, address, http_challenge=False, password=None):
        cfg = self.validate_root()
        if self.home.exists():
            raise shared.HttpsError('Есть предыдущая попытка. Сначала диагностика/откат.')
        password = cfg['web'].get('password', '') if password is None else password
        if not isinstance(password, str) or len(password) < 32 or not password.strip():
            raise shared.HttpsError('Задайте пароль PAPER не короче 32 символов.')
        snapshot = self.inventory()
        self.conflict_check(snapshot)
        origin = public_origin(address)
        instance_id = uuid.uuid4().hex
        return {'origin': origin, 'instance_id': instance_id, 'ports': [80, 443] if http_challenge else [443],
                'config_hash': shared.digest(self.config.read_bytes()), 'inventory_hash': shared.digest(shared.canonical(snapshot)),
                'password': password, 'caddyfile': caddyfile(origin, self.home / 'certificates', http_challenge, instance_id)}

    def recheck(self, plan):
        self.validate_root()
        if shared.digest(self.config.read_bytes()) != plan['config_hash'] or shared.digest(shared.canonical(self.inventory())) != plan['inventory_hash']:
            raise shared.HttpsError('Настройки/службы/порты изменились. Подготовьте новый план.')

    def owned_service(self, tx):
        self.validate_root()
        if shared.digest(self.config.read_bytes()) != tx['config_after'] or shared.digest((self.home / 'caddy.exe').read_bytes()) != tx['binary_hash'] or shared.digest((self.home / 'Caddyfile').read_bytes()) != tx['caddy_hash']:
            raise shared.HttpsError('Настройки HTTPS изменились.')
        snap = self.inventory()
        own = next((s for s in snap['services'] if s['Name'] == SERVICE), None)
        if not own or own['PathName'] != tx['command'] or own['StartName'] != 'LocalSystem' or own['State'] != 'Stopped':
            raise shared.HttpsError('Созданная служба не подтверждена.')
        if any(x['port'] in (80, 443, BACKEND) for x in snap['listeners']):
            raise shared.HttpsError('Порты изменились.')
        return snap

    def mutate(self, tx, action, confirm, fn):
        # Confirm at action time, not a blanket boolean recorded before download.
        if confirm(action + '\nPAPER: ' + str(self.root) + '\nHTTPS: ' + tx.get('origin', 'ещё не настроен')) is not True:
            raise shared.HttpsError('Действие не подтверждено.')
        tx['pending'] = action
        shared.save(self.journal, tx)  # Unknown outcomes survive crashes/timeouts.
        fn()
        tx['done'].append(action)
        tx['pending'] = None
        shared.save(self.journal, tx)

    @maintenance
    def apply(self, plan, confirm):
        if not self.guard.acquire(blocking=False):
            raise shared.HttpsError('Операция уже выполняется.')
        tx = None
        try:
            self.adapter.admin()
            self.recheck(plan)
            if self.home.exists():
                raise shared.HttpsError('Повторное применение запрещено.')
            binary = shared.validate_archive(self.local_archive if self.local_archive is not None else self.adapter.fetch())
            self.recheck(plan)
            self.home.mkdir(exist_ok=False)
            identity = {'format': 1, 'role': 'paper', 'service': SERVICE, 'root': str(self.root.resolve()), 'id': plan['instance_id']}
            shared.save(self.home / 'identity.json', identity)
            tx = dict(identity=identity, config_before=plan['config_hash'], done=[], pending=None, status='PREPARED')
            shared.save(self.journal, tx)
            self.adapter.protect(self.home)
            raw = self.config.read_bytes()
            (self.home / 'service.original.json').write_bytes(raw)
            exe = self.home / 'caddy.exe'
            exe.write_bytes(binary)
            conf = self.home / 'Caddyfile'
            conf.write_text(plan['caddyfile'], encoding='utf-8')
            cfg = json.loads(raw.decode('utf-8-sig'))
            cfg['web'].update(password=plan['password'], public_origin=plan['origin'])
            after = json.dumps(cfg, ensure_ascii=False, indent=2).encode()
            command = subprocess.list2cmdline([str(exe), 'run', '--config', str(conf), '--adapter', 'caddyfile'])
            tx = dict(identity=identity, command=command, config_before=shared.digest(raw), config_after=shared.digest(after),
                      binary_hash=shared.digest(binary), caddy_hash=shared.digest(conf.read_bytes()), ports=plan['ports'], origin=plan['origin'], done=[], pending=None, status='PREPARED')
            shared.save(self.journal, tx)
            self.adapter.run([str(exe), 'adapt', '--config', str(conf), '--adapter', 'caddyfile'])
            self.recheck(plan)
            def write_config():
                self.recheck(plan)
                tmp = self.home / 'service.new.json'
                tmp.write_bytes(after)
                os.replace(tmp, self.config)
            self.mutate(tx, 'Сохранить настройки PAPER (резервная копия создана)', confirm, write_config)
            def create():
                self.conflict_check(self.inventory())
                self.adapter.sc('create', SERVICE, 'start=', 'demand', 'binPath=', command)
            self.mutate(tx, 'Создать только службу ' + SERVICE, confirm, create)
            def firewall():
                if self.owned_service(tx)['firewall']:
                    raise shared.HttpsError('Правило firewall уже появилось.')
                self.adapter.firewall(plan['ports'], exe)
                self.adapter.validate_firewall(plan['ports'], exe)
            self.mutate(tx, 'Открыть входящий TCP ' + ','.join(map(str, plan['ports'])) + ' только для PAPER Caddy', confirm, firewall)
            def start_service():
                self.owned_service(tx)
                self.adapter.validate_firewall(plan['ports'], exe)
                self.adapter.sc('start', SERVICE)
                self.adapter.wait(SERVICE, 'Running')
            def enable_autostart():
                self.owned_service(tx)
                self.adapter.validate_firewall(plan['ports'], exe)
                self.adapter.sc('config', SERVICE, 'start=', 'auto')
            self.mutate(tx, 'Включить автозапуск PAPER HTTPS при загрузке Windows. IP/домен будет передан Let’s Encrypt для выпуска/автопродления публичного сертификата (Certificate Transparency). Условия: https://letsencrypt.org/repository/', confirm, enable_autostart)
            self.mutate(tx, 'Запустить только службу ' + SERVICE + '. IP/домен отправляется Let’s Encrypt для выпуска и автоматического продления публичного сертификата (Certificate Transparency). Условия: https://letsencrypt.org/repository/', confirm, start_service)
            tx['status'] = 'INSTALLED_UNVERIFIED'
            shared.save(self.journal, tx)
            return 'Служба PAPER установлена. Запустите PAPER отдельно, затем проверьте HTTPS. Доступ с телефона ещё не подтверждён.'
        except Exception as exc:
            if tx is not None:
                tx['failure'] = shared.safe_failure(exc, 'config')
                tx['status'] = 'REVIEW_REQUIRED'
                shared.save(self.journal, tx)
            raise shared.HttpsError(shared.safe_failure(exc, 'config')['reason'] + ' Проверьте диагностику перед повторением.') from None
        finally:
            self.guard.release()

    @maintenance
    def recover(self):
        if not self.guard.acquire(blocking=False):
            raise shared.HttpsError('Операция уже выполняется.')
        try:
            self.validate_root()
            tx = shared.read(self.journal)
            if tx.get('pending') or tx.get('status') not in ('PREPARED', 'ROLLED_BACK', 'REVIEW_REQUIRED'):
                raise shared.HttpsError('Нужна сверка неизвестного исхода или откат.')
            if shared.read(self.home / 'identity.json') != tx['identity'] or tx['identity'].get('root') != str(self.root.resolve()) or tx['identity'].get('service') != SERVICE:
                raise shared.HttpsError('Владелец не подтверждён.')
            self.conflict_check(self.inventory())
            if shared.digest(self.config.read_bytes()) != tx['config_before']:
                raise shared.HttpsError('Сначала восстановите настройки откатом.')
            for path in self.home.rglob('*'):
                if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                    raise shared.HttpsError('Ссылки не поддерживаются.')
            destination = self.root / ('paper-https-recovery-' + uuid.uuid4().hex)
            self.home.rename(destination)
            return 'Подготовка архивирована без удаления. Можно создать новый план.'
        finally:
            self.guard.release()

    def diagnose(self):
        self.validate_root()
        snapshot = self.inventory()
        tx = shared.read(self.journal) if self.journal.exists() else None
        return {'status': tx.get('status') if tx else 'NO_TRANSACTION', 'pending': tx.get('pending') if tx else None,
                'services': [s['Name'] for s in snapshot['services']], 'ports': sorted(set(x['port'] for x in snapshot['listeners'])), 'firewall': snapshot['firewall']}

    def verify(self):
        self.validate_root()
        tx = shared.read(self.journal)
        if tx.get('pending') or tx.get('status') != 'INSTALLED_UNVERIFIED' or shared.read(self.home / 'identity.json') != tx['identity'] or tx['identity'].get('role') != 'paper' or tx['identity'].get('service') != SERVICE or tx['identity'].get('root') != str(self.root.resolve()):
            raise shared.HttpsError('Завершённая собственная установка не подтверждена.')
        if shared.digest(self.config.read_bytes()) != tx['config_after'] or shared.digest((self.home / 'caddy.exe').read_bytes()) != tx['binary_hash'] or shared.digest((self.home / 'Caddyfile').read_bytes()) != tx['caddy_hash']:
            raise shared.HttpsError('Файлы установки изменились.')
        self.adapter.validate_firewall(tx['ports'], self.home / 'caddy.exe')
        snapshot = self.inventory()
        own = next((s for s in snapshot['services'] if s['Name'] == SERVICE), None)
        if not own or own['PathName'] != tx['command'] or own['State'] != 'Running' or own['StartName'] != 'LocalSystem':
            raise shared.HttpsError('Собственная служба PAPER не подтверждена.')
        shared.verify_service_process(own)
        backend = [x for x in snapshot['listeners'] if x['port'] == BACKEND]
        if not backend or any(x['address'] != '127.0.0.1' for x in backend):
            raise shared.HttpsError('Нужна работающая панель PAPER только на loopback8766.')
        if not any(x['port'] == 443 and x['pid'] == own['ProcessId'] for x in snapshot['listeners']):
            raise shared.HttpsError('Служба PAPER не слушает443.')
        self.adapter.verify_certificate(tx['origin'], tx['identity']['id'])
        return 'Сертификат и страница PAPER проверены с этого компьютера. Дополнительно откройте адрес на телефоне через мобильную сеть.'

    @maintenance
    def rollback(self, confirm):
        if not self.guard.acquire(blocking=False):
            raise shared.HttpsError('Операция уже выполняется.')
        try:
            self.validate_root()
            self.adapter.admin()
            tx = shared.read(self.journal)
            identity = shared.read(self.home / 'identity.json')
            if identity != tx['identity'] or identity.get('service') != SERVICE or identity.get('role') != 'paper' or identity.get('root') != str(self.root.resolve()):
                raise shared.HttpsError('Владелец установки не подтверждён.')
            if tx.get('pending'):
                raise shared.HttpsError('Исход предыдущей команды неизвестен. Автоматический откат запрещён; требуется сверка состояния.')
            if shared.digest((self.home / 'caddy.exe').read_bytes()) != tx['binary_hash'] or shared.digest((self.home / 'Caddyfile').read_bytes()) != tx['caddy_hash']:
                raise shared.HttpsError('Файлы HTTPS изменились.')
            raw = (self.home / 'service.original.json').read_bytes()
            if shared.digest(raw) != tx['config_before'] or shared.digest(self.config.read_bytes()) not in (tx['config_before'], tx['config_after']):
                raise shared.HttpsError('Настройки или резервная копия изменились.')
            snap = self.inventory()
            if any(x['port'] == BACKEND for x in snap['listeners']):
                raise shared.HttpsError('Сначала остановите PAPER.')
            def checked_service():
                current = next((s for s in self.inventory()['services'] if s['Name'] == SERVICE), None)
                if not current or current['PathName'] != tx['command'] or current['StartName'] != 'LocalSystem' or current.get('Dependencies') or current.get('Dependents'):
                    raise shared.HttpsError('Служба изменилась после подтверждения.')
                shared.verify_service_process(current)
                return current
            def disable_service():
                if checked_service()['State'] not in ('Running', 'Stopped'):
                    raise shared.HttpsError('Служба в переходном состоянии.')
                self.adapter.sc('config', SERVICE, 'start=', 'disabled')
                if checked_service().get('StartMode') != 'Disabled':
                    raise shared.HttpsError('Отключение автозапуска не подтверждено.')
            def stop_service():
                current = checked_service()
                if current['State'] != 'Running' or current.get('StartMode') != 'Disabled':
                    raise shared.HttpsError('Состояние службы изменилось.')
                self.adapter.sc('stop', SERVICE)
                self.adapter.wait(SERVICE, 'Stopped')
            def delete_service():
                current = checked_service()
                if current['State'] != 'Stopped' or current.get('StartMode') != 'Disabled':
                    raise shared.HttpsError('Служба не остановлена.')
                self.adapter.sc('delete', SERVICE)
                self.adapter.wait(SERVICE, 'Absent')
            own = next((s for s in snap['services'] if s['Name'] == SERVICE), None)
            if own:
                if own['PathName'] != tx['command'] or own['StartName'] != 'LocalSystem' or own.get('Dependencies') or own.get('Dependents'):
                    raise shared.HttpsError('Служба не соответствует собственной установке.')
                shared.verify_service_process(own)
                self.mutate(tx, 'Отключить автозапуск только ' + SERVICE + ' перед откатом', confirm, disable_service)
                if own['State'] == 'Running':
                    self.mutate(tx, 'Остановить только ' + SERVICE, confirm, stop_service)
                elif own['State'] != 'Stopped':
                    raise shared.HttpsError('Служба в переходном состоянии.')
                self.mutate(tx, 'Удалить только ' + SERVICE, confirm, delete_service)
            if RULE in snap['firewall']:
                if not any(x.startswith('Открыть входящий TCP ') for x in tx['done']):
                    raise shared.HttpsError('Владелец правила firewall не подтверждён.')
                def remove_rule():
                    self.adapter.validate_firewall(tx['ports'], self.home / 'caddy.exe')
                    self.adapter.remove_firewall()
                self.mutate(tx, 'Удалить только правило ' + RULE, confirm, remove_rule)
            def restore():
                if shared.digest(self.config.read_bytes()) not in (tx['config_before'], tx['config_after']):
                    raise shared.HttpsError('Настройки изменились.')
                temp = self.home / 'service.restore.json'
                temp.write_bytes(raw)
                os.replace(temp, self.config)
            self.mutate(tx, 'Восстановить настройки PAPER из резервной копии', confirm, restore)
            tx['status'] = 'ROLLED_BACK'
            shared.save(self.journal, tx)
            return 'PAPER HTTPS отключён. Резервная копия и журнал сохранены; LIVE не изменён.'
        finally:
            self.guard.release()


def gui(root=None):
    import tkinter as tk
    from tkinter import filedialog, messagebox
    import queue
    import secrets
    from independent_install import default_root
    window = tk.Tk()
    window.title('PAPER: HTTPS для телефона')
    root_var = tk.StringVar(value=str(root or default_root('paper')))
    address = tk.StringVar()
    password = tk.StringVar(value=secrets.token_urlsafe(32))
    http = tk.BooleanVar(value=False)
    controls = []
    for label, variable in [('Независимая папка PAPER', root_var), ('Публичный IP или домен', address), ('Новый пароль PAPER (сохраните его)', password)]:
        tk.Label(window, text=label).pack(anchor='w')
        entry = tk.Entry(window, textvariable=variable, width=78)
        entry.pack(fill='x')
        controls.append(entry)
    checkbox = tk.Checkbutton(window, text='Дополнительно TCP80 для ACME HTTP challenge (обычно не нужен)', variable=http)
    checkbox.pack()
    controls.append(checkbox)
    tk.Label(window, text='LIVE не изменяется. Остановите PAPER. Нужен публичный IP/домен и доступ TCP443.\nПроверка сертификата обязательна; порт 8766 никогда не открывается.\nFirewall облачного провайдера не меняется автоматически; доступ с телефона проверяется отдельно.').pack()
    output = tk.StringVar(value='Готово к проверке.')
    tk.Label(window, textvariable=output, wraplength=650).pack()
    manager = [None]
    job = shared.BackgroundJob()
    prompts = queue.Queue()
    buttons = []
    def confirm(action):
        event = threading.Event()
        answer = []
        prompts.put((action, event, answer))
        event.wait()
        return bool(answer and answer[0])
    def start(action):
        if job.busy:
            return
        selected_root = root_var.get()
        addr, pw, use_http = address.get(), password.get(), http.get()
        if manager[0] is None or str(manager[0].root) != selected_root:
            manager[0] = Setup(selected_root)
        setup = manager[0]
        def work():
            if action == 'install':
                return setup.apply(setup.prepare(addr, use_http, pw), confirm)
            if action == 'rollback':
                return setup.rollback(confirm)
            if action == 'recover':
                return setup.recover()
            if action == 'verify':
                return setup.verify()
            return json.dumps(setup.diagnose(), ensure_ascii=False, indent=2)
        if job.start(work):
            for b in buttons + controls:
                b.configure(state='disabled')
            output.set('Выполняется проверка; ожидайте подтверждений отдельных изменений.')
    def local_zip():
        try:
            if manager[0] is None or str(manager[0].root) != root_var.get():
                manager[0] = Setup(root_var.get())
            name = filedialog.askopenfilename(filetypes=[('Caddy ZIP', '*.zip')])
            if name:
                manager[0].select_local_zip(name)
                output.set('Официальный ZIP проверен по SHA256.')
        except Exception as exc:
            output.set(shared.safe_failure(exc, 'local_archive')['reason'])
    for title, action in [('Установить PAPER HTTPS', 'install'), ('Диагностика', 'diagnose'), ('Проверить сертификат', 'verify'), ('Откат PAPER HTTPS', 'rollback'), ('Архивировать проверенную подготовку', 'recover')]:
        b = tk.Button(window, text=title, command=lambda a=action: start(a))
        b.pack(fill='x'); buttons.append(b)
    b = tk.Button(window, text='Выбрать официальный ZIP Caddy', command=local_zip)
    b.pack(fill='x'); buttons.append(b)
    def poll():
        try:
            action, event, answer = prompts.get_nowait()
            answer.append(messagebox.askyesno('Подтверждение изменения PAPER', action + '?', parent=window))
            event.set()
        except queue.Empty:
            pass
        result = job.poll()
        if result is not None:
            ok, value = result
            output.set(str(value) if ok else shared.safe_failure(value, 'config')['reason'] + ' Используйте диагностику.')
            for b in buttons + controls:
                b.configure(state='normal')
        window.after(100, poll)
    window.protocol('WM_DELETE_WINDOW', lambda: messagebox.showinfo('PAPER', 'Дождитесь завершения операции.') if job.busy else window.destroy())
    poll()
    window.mainloop()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    gui(parser.parse_args().root)
