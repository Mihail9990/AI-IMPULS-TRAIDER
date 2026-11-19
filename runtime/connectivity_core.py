"""Isolated read-only widget setup, no live runtime startup."""
import base64
import ctypes
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import http.client

EXE_SHA256='2622e239c79df700b0eb92cbd950d1edee365e155a7859f358f0e55f002df591'
class ConnectionError(RuntimeError):pass

def safe_path(path):
    path=Path(path)
    for p in [path,*path.parents]:
        if p.is_symlink() or (p.exists() and getattr(p.stat(),'st_file_attributes',0)&1024):raise ConnectionError('Ссылки/junction не поддерживаются.')
    return path

def public_ip(s):
    ip=ipaddress.IPv4Address(s.strip())
    if not ip.is_global or ip.is_multicast or ip.is_reserved:raise ConnectionError('Нужен публичный IPv4 вашего VPS.')
    return str(ip)

def dpapi(data,decrypt=False):
    if os.name!='nt':raise ConnectionError('DPAPI доступен только на Windows.')
    from ctypes import wintypes
    class Blob(ctypes.Structure):_fields_=[('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buf=ctypes.create_string_buffer(data);src=Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)));dst=Blob()
    crypt=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p
    if decrypt:
        fn=crypt.CryptUnprotectData;fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        args=(ctypes.byref(src),None,None,None,None,1,ctypes.byref(dst))
    else:
        fn=crypt.CryptProtectData;fn.argtypes=[ctypes.POINTER(Blob),wintypes.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
        args=(ctypes.byref(src),'RECOVERY connectivity only',None,None,None,1,ctypes.byref(dst))
    fn.restype=wintypes.BOOL
    if not fn(*args):raise ConnectionError('Не удалось обработать ключ DPAPI.')
    try:return ctypes.string_at(dst.data,dst.size)
    finally:kernel.LocalFree(ctypes.cast(dst.data,ctypes.c_void_p))

def profile(server,ip,*,protect=dpapi):
    ip=public_ip(ip);folder=safe_path(Path(server)/'connectivity-only');folder.mkdir(exist_ok=True)
    path=safe_path(folder/'profile.json')
    if path.exists():
        saved=json.loads(path.read_text(encoding='utf-8'))
        if saved.get('version')!=1 or saved.get('ip')!=ip:raise ConnectionError('В этой установке уже сохранён другой IP. Конфиг не изменён.')
        token=protect(base64.b64decode(saved['key_dpapi'],validate=True),decrypt=True).decode('ascii')
        if len(token)<32:raise ConnectionError('Недействительный ключ.')
    else:
        token=secrets.token_urlsafe(32)
        saved={'version':1,'ip':ip,'key_dpapi':base64.b64encode(protect(token.encode('ascii'))).decode('ascii')}
        with path.open('x',encoding='utf-8') as f:json.dump(saved,f)
    return folder,token

def caddy_config(ip,storage):
    ip=public_ip(ip);storage=json.dumps(str(Path(storage).absolute()).replace('\\','/'))
    return '''{
 admin off
 persist_config off
 skip_install_trust
 auto_https disable_redirects
 storage file_system {
  root %s
 }
 servers {
  protocols h1 h2
 }
}
https://%s {
 tls {
  issuer acme {
   dir https://acme-v02.api.letsencrypt.org/directory
   profile shortlived
   disable_http_challenge
  }
 }
 @state {
  path /api/recovery/state
  method GET
 }
 handle @state {
  reverse_proxy 127.0.0.1:8767
 }
 handle {
  respond "READ_ONLY_NO_TRADING" 404
 }
}
'''%(storage,ip)

class ConnectionSession:
    def __init__(self,server,assets,*,run=subprocess.run,popen=subprocess.Popen,readiness=None):
        self.server=Path(server);self.assets=Path(assets);self.run=run;self.popen=popen;self.children=[];self.files=[];self.temp=None;self.readiness=readiness or self._ready
    def _ready(self,child,token,nonce):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if child.poll() is not None:raise ConnectionError('Read-only API завершился при запуске.')
            c=http.client.HTTPConnection('127.0.0.1',8767,timeout=1)
            try:
                c.request('GET','/_local_ready',headers={'Authorization':'Bearer '+token})
                r=c.getresponse();body=r.read(4096)
                if r.status==200 and json.loads(body).get('instance')==nonce:return
            except (OSError,ValueError,http.client.HTTPException):pass
            finally:c.close()
            time.sleep(.1)
        raise ConnectionError('Не подтверждён собственный read-only API; HTTPS не запускается.')
    def start(self,ip,token,storage):
        if self.children:raise ConnectionError('Соединение уже запущено.')
        ip=public_ip(ip)
        if not isinstance(token,str) or len(token)<32 or not token.isascii() or '\n' in token or '\r' in token:raise ConnectionError('Недействительный ключ.')
        safe_path(storage)
        exe=safe_path(self.server/'tools/caddy/caddy.exe');python=safe_path(self.server/'.venv-native/Scripts/python.exe')
        if not python.is_file() or not exe.is_file() or hashlib.sha256(exe.read_bytes()).hexdigest()!=EXE_SHA256:
            raise ConnectionError('Проверьте установленный Python и официальный Caddy 2.11.9.')
        # Refuse occupied API port; do not attach to or replace another process.
        with socket.socket() as sock:
            try:sock.bind(('127.0.0.1',8767))
            except OSError:raise ConnectionError('Порт 8767 уже занят. Другой процесс не остановлен.')
        self.temp=tempfile.TemporaryDirectory(prefix='recovery-readonly-');folder=Path(self.temp.name);cfg=folder/'Caddyfile'
        from native_web_panel import connectivity_panel_config
        cfg.write_text(connectivity_panel_config(caddy_config(ip,Path(storage)/'certificates')),encoding='utf-8')
        flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
        try:
            for action in ('adapt','validate'):
                r=self.run([str(exe),action,'--config',str(cfg),'--adapter','caddyfile'],capture_output=True,text=True,timeout=30,creationflags=flags,shell=False)
                if r.returncode:raise ConnectionError('Caddy не прошёл проверку конфига.')
                if action=='adapt' and json.loads(r.stdout).get('admin',{}).get('disabled') is not True:raise ConnectionError('Admin API не отключён.')
            for name,args in [('api',[str(python),'-I',str(self.assets/'connectivity_api.py')]),('caddy',[str(exe),'run','--config',str(cfg),'--adapter','caddyfile'])]:
                log=(folder/(name+'.log')).open('w',encoding='utf-8');self.files.append(log)
                child=self.popen(args,stdin=subprocess.PIPE if name=='api' else subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=flags,shell=False,cwd=folder)
                self.children.append(child)
                if name=='api':
                    nonce=secrets.token_hex(24);child.stdin.write((token+'\n'+nonce+'\n'+'https://'+ip+'\n').encode('ascii'));child.stdin.close();self.readiness(child,token,nonce)
        except BaseException:self.stop();raise
    def logs(self):
        if not self.temp:return ''
        text=''
        for p in Path(self.temp.name).glob('*.log'):
            with p.open('rb') as f:f.seek(max(0,p.stat().st_size-15000));text+=p.name+'\n'+f.read().decode('utf-8','replace')
        return text
    def stop(self):
        for child in reversed(self.children):
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
        self.children=[]
        for f in self.files:f.close()
        self.files=[]
        if self.temp:self.temp.cleanup();self.temp=None
