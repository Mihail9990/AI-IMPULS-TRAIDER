"""Offline, per-user installer. It never starts MT5, the strategy or its helpers."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from zipfile import ZipFile

from package_spec import BOOTSTRAP, HELPERS, RUNTIME, SHORTCUT, WHEELS, digest

HERE = Path(__file__).resolve().parent


def ps(code, **fields):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             "$ErrorActionPreference='Stop';" + code], env=dict(os.environ, **fields),
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode


def real_dir(path):
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        raise ValueError("Некорректный каталог установки: " + path.name)
    return path


def safe_tree(base, parts):
    here = real_dir(base)
    for part in parts:
        here = here / part
        here.mkdir(exist_ok=True)
        real_dir(here)
    return here


def secure_private(path):
    path.mkdir(exist_ok=True)
    real_dir(path)
    script = """
$p=$env:REC_PRIVATE; $u=[Security.Principal.WindowsIdentity]::GetCurrent().User
$ids=@($u.Value,'S-1-5-18','S-1-5-32-544')
if($env:REC_NEW -eq '1'){
  $acl=New-Object Security.AccessControl.DirectorySecurity
  $acl.SetAccessRuleProtection($true,$false)
  foreach($id in $ids){
    $sid=[Security.Principal.SecurityIdentifier]$id
    $rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
  }
  Set-Acl -LiteralPath $p -AclObject $acl
}
$a=Get-Acl -LiteralPath $p
foreach($rule in $a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])){
 if($rule.AccessControlType -eq 'Allow' -and $ids -notcontains $rule.IdentityReference.Value){exit 4}
}
"""
    if ps(script, REC_PRIVATE=str(path), REC_NEW="1" if not any(path.iterdir()) else "0"):
        raise ValueError("Требуется приватный ACL каталога python-v1 для владельца, администраторов и SYSTEM.")


def read(path):
    if path.is_symlink() or path.stat().st_size > 65536:
        raise ValueError("Некорректный локальный файл.")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def machine(value):
    fields = {"format", "login", "server", "symbol", "magic", "terminal_exe", "data_dir", "helper_scope"}
    if not isinstance(value, dict) or not fields <= set(value) or type(value["format"]) is not int or value["format"] != 1:
        raise ValueError("Неверный формат machine.json.")
    if any(type(value[k]) is not int or not 0 < value[k] < 2**63 for k in ("login", "magic")):
        raise ValueError("Login и Magic должны быть положительными целыми числами.")
    for k in ("server", "symbol"):
        v = value[k]
        if not isinstance(v, str) or not 1 <= len(v) <= 100 or v != v.strip() or "||" in v or any(ord(c) < 32 or ord(c) == 127 for c in v):
            raise ValueError("Проверьте сервер и символ MT5.")
    if not isinstance(value["helper_scope"], str) or not re.fullmatch("[0-9a-f]{32}", value["helper_scope"]):
        raise ValueError("Некорректная привязка помощников.")
    exe, data = Path(value["terminal_exe"]), Path(value["data_dir"])
    if not exe.is_absolute() or exe.name.lower() != "terminal64.exe" or not exe.is_file() or not data.is_absolute():
        raise ValueError("Укажите существующий terminal64.exe и каталог данных MT5.")
    real_dir(data / "MQL5")
    real_dir(data / "MQL5/Files")
    return {k: value[k] for k in fields}


def choose_machine(base, path, explicit, *, interactive):
    if path.exists():
        value = machine(read(path))
        if explicit:
            raise ValueError("Существующий machine.json сохранён. Параметр --binding доступен только для новой установки.")
        return value
    if explicit:
        raw = read(explicit)
    else:
        old_desk, old_profile, old_roles = base/"control/desk/settings.json", base/"recovery-profile.json", base/"role-session.json"
        if all(p.is_file() for p in (old_desk, old_profile, old_roles)):
            cfg, profile, role = read(old_desk), read(old_profile)["mt5"], read(old_roles)
            if any(profile[k] != role["expected_" + k] for k in ("login", "server", "symbol")):
                raise ValueError("Привязки прежней установки расходятся. Укажите проверенный файл --binding.")
            raw = dict(login=profile["login"], server=profile["server"], symbol=profile["symbol"],
                       magic=role["strategy_magic"], data_dir=cfg["data_dir"], terminal_exe=cfg["terminal_exe"])
        elif interactive:
            raw = {k: input(prompt + ": ").strip().strip('"') for k, prompt in (
                ("terminal_exe", "Полный путь к terminal64.exe"), ("data_dir", "Каталог данных MT5"),
                ("login", "Номер счёта MT5"), ("server", "Сервер MT5"), ("symbol", "Символ как в MT5"),
                ("magic", "Отдельный положительный Magic стратегии"))}
            for key in ("magic", "login"):
                raw[key] = int(raw[key])
        else:
            raise ValueError("Нужен файл --binding с локальными параметрами.")
    return machine(dict(raw, format=1, helper_scope=secrets.token_hex(16)))


def manifest(root):
    meta = read(root/"manifest.json")
    expected = {*("app/"+x for x in RUNTIME), *("helpers/"+s+".ex5" for s in HELPERS), *("wheels/"+n for n in WHEELS)}
    if meta.get("format") != 1 or set(meta["files"]) != expected:
        raise ValueError("Неполный состав установочного пакета.")
    bootstrap = {name: digest(root/name) for name in BOOTSTRAP}
    if meta.get("bootstrap") != bootstrap:
        raise ValueError("Файлы установщика не совпадают с контрольными суммами.")
    content = json.dumps([meta["files"], bootstrap], sort_keys=True, separators=(",", ":")).encode()
    if meta.get("build") != hashlib.sha256(content).hexdigest()[:16]:
        raise ValueError("Не совпадает номер сборки.")
    for name, checksum in meta["files"].items():
        if digest(root/name) != checksum or (name.startswith("wheels/") and WHEELS[Path(name).name] != checksum):
            raise ValueError("Контрольная сумма не совпадает: " + name)
    return meta


def extract_lib(wheel, target):
    with ZipFile(wheel) as z:
        for entry in z.infolist():
            p = Path(entry.filename)
            if p.is_absolute() or p.drive or ".." in p.parts or ":" in entry.filename or "\\" in entry.filename:
                raise ValueError("Некорректный путь внутри wheel.")
            if not entry.is_dir():
                dst = target / p
                dst.parent.mkdir(exist_ok=True, parents=True)
                with z.open(entry) as inp, dst.open("xb") as out:
                    shutil.copyfileobj(inp, out)


def stage_code(root, builds, meta):
    target = builds / meta["build"]
    if not target.exists():
        tmp = Path(tempfile.mkdtemp(prefix=".stage-", dir=builds))
        try:
            for name in RUNTIME:
                shutil.copyfile(root/"app"/name, tmp/name)
            for name in WHEELS:
                extract_lib(root/"wheels"/name, tmp/"lib")
            check_runtime(tmp)
            inventory = {f.relative_to(tmp).as_posix(): digest(f) for f in tmp.rglob("*") if f.is_file()}
            (tmp/"installed.sha256.json").write_text(json.dumps(inventory, sort_keys=True), encoding="ascii")
            tmp.rename(target)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp)
    else:
        real_dir(target)
        index = json.loads((target/"installed.sha256.json").read_text(encoding="ascii"))
        actual = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} - {"installed.sha256.json"}
        if set(index) != actual or any(digest(target/p) != expected for p, expected in index.items()):
            raise ValueError("Файлы существующей сборки изменены. Сохраните её отдельно и проверьте источник пакета.")
        if any(digest(target/n) != meta["files"]["app/"+n] for n in RUNTIME):
            raise ValueError("Не совпадает версия установленного приложения.")
        check_runtime(target)
    return target


def check_runtime(target):
    code = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import numpy,MetaTrader5,tkinter;"
            "assert all(Path(m.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()) for m in (numpy,MetaTrader5));"
            "assert numpy.__version__=='2.5.5' and MetaTrader5.__version__=='5.0.6231';tkinter.Tcl()")
    if subprocess.run([sys.executable,"-I","-B","-c",code,str(target/"lib")],capture_output=True).returncode:
        raise ValueError("Не удалось импортировать локальные MT5, NumPy и Tcl/Tk на Python 3.13 x64.")


def preflight(base, cfg):
    status = base/"control/python-v1/status.json"
    # A running supervisor keeps its mutex even while paused; switching the shortcut
    # must never present the old owner as the freshly installed runtime.
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenMutexW.argtypes = [wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
    k.OpenMutexW.restype = wintypes.HANDLE
    k.OpenEventW.argtypes = k.OpenMutexW.argtypes
    k.OpenEventW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    owner = "Local\\RECOVERY.Python."+hashlib.sha256(str(status.parent).casefold().encode()).hexdigest()[:24]
    desk = base/"control/desk"
    oldweb = "Local\\AI-IMPULS-RECOVERY.Panel.Stop."+uuid.uuid5(uuid.NAMESPACE_OID,str(desk).casefold()).hex
    for f, name, reason in ((k.OpenMutexW,owner,"Контроллер Python запущен."), (k.OpenEventW,oldweb,"Прежняя веб-панель запущена.")):
        ctypes.set_last_error(0)
        h = f(0x100000, False, name)
        if h:
            k.CloseHandle(h)
            raise ValueError(reason+" См. раздел подготовки в README_RU.txt.")
        if ctypes.get_last_error() not in (0,2):
            raise ValueError("Не удалось установить владельца локального контроллера.")
    code = r"""$p=$env:REC_PROXY
if(Get-Process -Name caddy -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $p }){exit 3}
$base=$env:REC_APP
foreach($p in Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'"){
 if($p.CommandLine -and $p.CommandLine.IndexOf($base,[StringComparison]::OrdinalIgnoreCase) -ge 0 -and
    $p.CommandLine -match '(?i)(control_desk|recovery_desktop)\.pyw\b'){exit 4}
}
"""
    if ps(code, REC_PROXY=str(base/"control/proxy/caddy.exe"), REC_APP=str(base/"control")):
        raise ValueError("Проверьте прежний пульт и его отдельный Caddy. См. README_RU.txt.")


@contextmanager
def check_owner_files(cfg):
    files = Path(cfg["data_dir"])/"MQL5/Files"
    k = ctypes.WinDLL("kernel32",use_last_error=True)
    k.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,wintypes.LPVOID,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    k.CreateFileW.restype=wintypes.HANDLE
    k.CloseHandle.argtypes=[wintypes.HANDLE]
    with ExitStack() as stack:
        for sub, name in (("RECOVERY_CONTROL","owner.lock"),("RECOVERY_PANEL","writer.lock")):
            folder = safe_tree(files,(sub,))
            h=k.CreateFileW(str(folder/name),0x80000000,0,None,4,128,None)
            if h in (None,ctypes.c_void_p(-1).value):
                raise ValueError("Прежний основной советник или Python-владелец удерживает управление в MT5.")
            stack.callback(k.CloseHandle,h)
        yield


def create_once(path, data):
    fd, temp = tempfile.mkstemp(prefix=".install-",dir=path.parent)
    try:
        with os.fdopen(fd,"wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.link(temp,path)
    finally:
        os.unlink(temp)


def preset_values(cfg):
    return dict(ExpectedLogin=str(cfg["login"]), ExpectedServer=cfg["server"], ExpectedSymbol=cfg["symbol"], PythonScope=cfg["helper_scope"])


def read_preset(path):
    raw=path.read_bytes()
    if len(raw)>65536: raise ValueError("Слишком большой пресет.")
    text=raw.decode("utf-16" if raw.startswith((b"\xff\xfe",b"\xfe\xff")) else "utf-8-sig")
    values={}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(";"):continue
        k, delim, v=line.partition("=")
        if not delim or k in values:raise ValueError("Неоднозначный пресет.")
        values[k]=v
    return values


def prepare_data(root, private, cfg):
    experts = safe_tree(Path(cfg["data_dir"])/"MQL5",("Experts","RECOVERY-Python"))
    presets = safe_tree(private,("presets",))
    values=preset_values(cfg)
    text="".join(k+"="+v+"\r\n" for k,v in values.items()).encode("utf-16")
    writes=[]
    for helper in HELPERS:
        src=root/"helpers"/(helper+".ex5")
        dest=experts/src.name
        if dest.exists() and (dest.is_symlink() or digest(dest)!=digest(src)):
            raise ValueError("Уже установлен другой "+src.name+". Перенесите его в резервную папку по инструкции в README_RU.txt и повторите установку.")
        if not dest.exists(): writes.append((dest,src.read_bytes()))
        preset=presets/(helper+".set")
        if preset.exists() and (preset.is_symlink() or read_preset(preset)!=values):
            raise ValueError("Существующие параметры "+preset.name+" отличаются.")
        if not preset.exists():writes.append((preset,text))
    current=private/"machine.json"
    if current.exists() and machine(read(current))!=cfg:
        raise ValueError("Локальная привязка была изменена.")
    if not current.exists():
        # Save scope before dependants so retry after a partial copy reuses it.
        create_once(current,(json.dumps(cfg,ensure_ascii=False,indent=2)+"\n").encode("utf-8"))
    for p,data in writes:
        create_once(p,data)


def shortcut(target, desktop=None):
    pythonw=Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():raise ValueError("Не найден pythonw.exe.")
    script = r"""$desk=if($env:REC_DESKTOP){$env:REC_DESKTOP}else{[Environment]::GetFolderPath('DesktopDirectory')}
$dst=Join-Path $desk $env:REC_NAME
$tmp=Join-Path $desk ('.recovery-'+[Guid]::NewGuid().ToString('N')+'.lnk')
$prior=Join-Path $desk ('.recovery-prior-'+[Guid]::NewGuid().ToString('N')+'.lnk')
$s=New-Object -ComObject WScript.Shell
try{
 if(Test-Path -LiteralPath $dst){
  [IO.File]::Copy($dst,$prior)
  $old=$s.CreateShortcut($prior)
  if(($old.Arguments+' '+$old.TargetPath) -notmatch '(?i)(control_desk\.pyw|recovery_desktop\.pyw|RECOVERY[-— ]+Пульт|AI-IMPULS-RECOVERY)'){exit 4}
 }
 $l=$s.CreateShortcut($tmp);$l.TargetPath=$env:REC_PY;$l.Arguments='-I -B -X utf8 "'+$env:REC_GUI+'"'
 $l.WorkingDirectory=$env:REC_CWD;$l.Description='RECOVERY';$l.Save()
 $verify=$s.CreateShortcut($tmp)
 if($verify.TargetPath -ne $env:REC_PY -or -not $verify.Arguments.Contains($env:REC_GUI)){exit 5}
 if(Test-Path -LiteralPath $dst){[IO.File]::Replace($tmp,$dst,[System.Management.Automation.Language.NullString]::Value)}
 else{[IO.File]::Move($tmp,$dst)}
}finally{foreach($f in @($tmp,$prior)){if(Test-Path -LiteralPath $f){Remove-Item -LiteralPath $f}}}
"""
    if ps(script,REC_PY=str(pythonw),REC_GUI=str(target/"recovery_desktop.pyw"),REC_CWD=str(target),REC_NAME=SHORTCUT,
          REC_DESKTOP=str(desktop) if desktop is not None else ""):
        raise ValueError("Не удалось обновить ярлык владельца.")


def install(root, base, binding=None, interactive=True, verify_only=False):
    if os.name!="nt" or sys.version_info[:2]!=(3,13) or sys.maxsize<2**32:
        raise ValueError("Нужен полный Windows CPython 3.13 x64 с Tcl/Tk.")
    import tkinter
    tkinter.Tcl()
    meta=manifest(root)
    if verify_only:return meta["build"]
    base.mkdir(exist_ok=True,parents=True)
    control=safe_tree(base,("control",))
    private=control/"python-v1"
    secure_private(private)
    cfg=choose_machine(base,private/"machine.json",binding,interactive=interactive)
    preflight(base,cfg)
    with check_owner_files(cfg):
        app=stage_code(root,safe_tree(control,("python-builds",)),meta)
        prepare_data(root,private,cfg)
        shortcut(app)
    return meta["build"]


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--binding",type=Path)
    parser.add_argument("--verify-only",action="store_true")
    args=parser.parse_args()
    try:
        version=install(HERE,Path(os.environ["LOCALAPPDATA"])/"AI-IMPULS-RECOVERY",args.binding,
                        interactive=sys.stdin.isatty(),verify_only=args.verify_only)
        print("Пакет проверен." if args.verify_only else "Готово. Порядок подключения двух помощников указан в README_RU.txt.")
        print("Сборка "+version)
    except (KeyError,EOFError,ValueError,OSError,ImportError,TypeError) as error:
        print(str(error) if isinstance(error,ValueError) else "Установка не завершена. Проверьте локальные файлы, права и Python.",file=sys.stderr)
        sys.exit(2)
