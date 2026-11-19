"""Optional, explicitly approved Windows firewall action. Never run on import.

GUI must disclose inbound TCP 443 for the exact Caddy executable on all profiles,
then pass approved=True ONLY for that click. Call from a worker (UAC may wait).
Does not start Caddy, alter provider rules, enable trading, or modify other rules.
"""
import base64
import hashlib
import os
from pathlib import Path
import subprocess


class FirewallSetupError(RuntimeError):
    pass


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def build_script(program):
    """Pure script builder; does not execute. Program was validated by caller."""
    name = 'MT5Widget-Caddy-HTTPS-' + hashlib.sha256(str(program).casefold().encode('utf-8')).hexdigest()[:20]
    script = f"""$ErrorActionPreference = 'Stop'
function Match-Fields($item, $expected) {{
  if (@($item).Count -ne 1) {{ return $false }}
  foreach ($key in $expected.Keys) {{
    $prop = $item.PSObject.Properties[$key]
    if ($null -eq $prop -or @($prop.Value).Count -ne 1 -or [string]$prop.Value -cne [string]$expected[$key]) {{ return $false }}
  }}
  return $true
}}
function Match-OwnRule($rule, $name, $program) {{
  if (-not (Match-Fields $rule @{{Name=$name;DisplayName=$name;Description='MT5 Widget read-only HTTPS connectivity: Caddy TCP 443 only';Direction='Inbound';Action='Allow';Enabled='True';Profile='Any';EdgeTraversalPolicy='Block';LooseSourceMapping='False';LocalOnlyMapping='False'}})) {{ return $false }}
  $port = @($rule | Get-NetFirewallPortFilter -ErrorAction Stop)
  $app = @($rule | Get-NetFirewallApplicationFilter -ErrorAction Stop)
  $address = @($rule | Get-NetFirewallAddressFilter -ErrorAction Stop)
  $service = @($rule | Get-NetFirewallServiceFilter -ErrorAction Stop)
  $interface = @($rule | Get-NetFirewallInterfaceFilter -ErrorAction Stop)
  $interfaceType = @($rule | Get-NetFirewallInterfaceTypeFilter -ErrorAction Stop)
  $security = @($rule | Get-NetFirewallSecurityFilter -ErrorAction Stop)
  if ($port.Count -ne 1 -or $app.Count -ne 1 -or $address.Count -ne 1 -or $service.Count -ne 1 -or $interface.Count -ne 1 -or $interfaceType.Count -ne 1 -or $security.Count -ne 1) {{ return $false }}
  if (-not (Match-Fields $port[0] @{{Protocol='TCP';LocalPort='443';RemotePort='Any';IcmpType='Any';DynamicTarget='Any'}})) {{ return $false }}
  if (-not (Match-Fields $app[0] @{{Program=$program;Package='Any'}})) {{ return $false }}
  if (-not (Match-Fields $address[0] @{{LocalAddress='Any';RemoteAddress='Any'}})) {{ return $false }}
  if (-not (Match-Fields $service[0] @{{Service='Any'}})) {{ return $false }}
  if (-not (Match-Fields $interface[0] @{{InterfaceAlias='Any'}})) {{ return $false }}
  if (-not (Match-Fields $interfaceType[0] @{{InterfaceType='Any'}})) {{ return $false }}
  if (-not (Match-Fields $security[0] @{{Authentication='NotRequired';Encryption='NotRequired';OverrideBlockRules='False';LocalUser='Any';RemoteUser='Any';RemoteMachine='Any'}})) {{ return $false }}
  return $true
}}
try {{
  $program = {_literal(str(program))}
  $name = {_literal(name)}
  if (-not (Test-Path -LiteralPath $program -PathType Leaf)) {{ exit 12 }}
  $existing = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop | Where-Object {{ $_.Name -eq $name }})
  if ($existing.Count -ne 0) {{
    if ($existing.Count -eq 1 -and (Match-OwnRule $existing[0] $name $program)) {{ exit 20 }}
    exit 10
  }}
  New-NetFirewallRule -PolicyStore PersistentStore -Name $name -DisplayName $name -Description 'MT5 Widget read-only HTTPS connectivity: Caddy TCP 443 only' -Direction Inbound -Action Allow -Enabled True -Profile Any -Protocol TCP -LocalPort 443 -Program $program -EdgeTraversalPolicy Block -ErrorAction Stop | Out-Null
  $rule = Get-NetFirewallRule -PolicyStore PersistentStore -Name $name -ErrorAction Stop
  $port = $rule | Get-NetFirewallPortFilter -ErrorAction Stop
  $app = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
  if ($rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow' -or $rule.Enabled -ne 'True' -or $port.Protocol -ne 'TCP' -or @($port.LocalPort).Count -ne 1 -or $port.LocalPort -ne '443' -or $app.Program -ne $program) {{ exit 13 }}
  exit 0
}} catch {{ exit 11 }}
"""
    return name, script


def _elevated_run(executable, parameters):
    """Use documented ShellExecuteEx runas; wait and inspect real child exit code."""
    import ctypes
    from ctypes import wintypes as w
    class ShellInfo(ctypes.Structure):
        _fields_ = [('cbSize', w.DWORD), ('fMask', w.ULONG), ('hwnd', w.HWND),
                    ('lpVerb', w.LPCWSTR), ('lpFile', w.LPCWSTR), ('lpParameters', w.LPCWSTR),
                    ('lpDirectory', w.LPCWSTR), ('nShow', ctypes.c_int), ('hInstApp', w.HINSTANCE),
                    ('lpIDList', ctypes.c_void_p), ('lpClass', w.LPCWSTR), ('hkeyClass', w.HKEY),
                    ('dwHotKey', w.DWORD), ('hIcon', w.HANDLE), ('hProcess', w.HANDLE)]
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellInfo)]
    shell.ShellExecuteExW.restype = w.BOOL
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    kernel.GetExitCodeProcess.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL
    info = ShellInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile, info.lpParameters = 'runas', executable, parameters
    info.nShow = 1
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == 1223:
            raise FirewallSetupError('Подтверждение UAC отменено. Правило не добавлено.')
        raise FirewallSetupError(f'Не удалось запустить настройку брандмауэра (Windows {code}).')
    if not info.hProcess:
        raise FirewallSetupError('Нет подтверждения запуска. Проверьте брандмауэр вручную; не повторяйте вслепую.')
    try:
        if kernel.WaitForSingleObject(info.hProcess, 0xFFFFFFFF) != 0:
            raise FirewallSetupError('Результат настройки неизвестен. Проверьте правило вручную.')
        code = w.DWORD()
        if not kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            raise FirewallSetupError('Не удалось получить результат настройки. Проверьте правило вручную.')
        return code.value
    finally:
        kernel.CloseHandle(info.hProcess)


def configure_firewall(caddy_exe, *, approved=False, log=lambda message: None,
                       platform=os.name, runner=None):
    if approved is not True:
        raise FirewallSetupError('Нужно отдельное явное подтверждение пользователя для настройки брандмауэра.')
    if platform != 'nt':
        raise FirewallSetupError('Настройка брандмауэра предназначена только для Windows.')
    path = Path(caddy_exe)
    if (not path.is_absolute() or not path.is_file() or path.is_symlink()
            or path.name.lower() != 'caddy.exe' or str(path).startswith(('\\\\', '//'))
            or any(ord(c) < 32 for c in str(path))):
        raise FirewallSetupError('Выберите существующий локальный caddy.exe без ссылок.')
    if any(p.is_symlink() for p in path.parents):
        raise FirewallSetupError('Путь Caddy не должен проходить через ссылки.')
    path = path.resolve()
    name, script = build_script(path)
    # System directory from Win32, not PATH/environment command resolution.
    if runner is None:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, len(buf))
        if not n or n >= len(buf):
            raise FirewallSetupError('Не удалось определить системную папку Windows.')
        executable = str(Path(buf.value) / 'WindowsPowerShell/v1.0/powershell.exe')
        runner = _elevated_run
    else:
        executable = 'powershell.exe'  # dependency-injected tests only
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    parameters = subprocess.list2cmdline(['-NoProfile', '-NonInteractive', '-EncodedCommand', encoded])
    log('Ожидание UAC: входящий TCP 443 только для выбранного caddy.exe.\n')
    code = runner(executable, parameters)
    if code == 20:
        log(f'Правило {name} уже существует и точно соответствует проверенным параметрам. Изменений нет.\n')
        return name
    if code == 10:
        raise FirewallSetupError(f'Правило {name} уже существует, но точное соответствие не подтверждено. Оно не изменено; проверьте его вручную.')
    if code:
        raise FirewallSetupError(f'Настройка не подтверждена (код {code}). Проверьте правило {name} вручную; оно могло быть создано.')
    log(f'Создано локальное правило {name}. Доступность из интернета и правила провайдера не проверены.\n')
    return name
