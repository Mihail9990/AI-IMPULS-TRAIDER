"""Local file discovery and compilation only. Never initializes or starts MT5.

MetaQuotes docs: /en/metaeditor/help/beginning/integration_ide and
/en/metaeditor/help/beginning/open on www.metatrader5.com.
Compilation is not attachment, account verification, or permission to trade.
"""
from dataclasses import dataclass
from pathlib import Path
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from setup_core import SetupError

EA_SHA256 = '990a68c50900b9c38969eea558e18d0da98c702f71b918eae137decbb9af2b7e'
EA_NAME = 'RecoveryEvidenceBridge.mq5'

@dataclass(frozen=True)
class Mt5Installation:
    terminal: Path
    metaeditor: Path
    data_dir: Path

@dataclass(frozen=True)
class BuildResult:
    source: Path
    executable: Path
    log_path: Path | None
    reused: bool = False


def _safe(path):
    path = Path(path).absolute()
    if str(path).startswith(('\\\\', '//')):
        raise SetupError('Выберите локальную папку, не сетевой путь.')
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 1024):
            raise SetupError('Ссылки/junction в путях MT5 не поддерживаются.')
    return path


def _text(path):
    raw = _safe(path).read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise SetupError('Неожиданно большой файл MT5.')
    encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-16-le' if b'\x00' in raw else 'utf-8-sig')
    return raw.decode(encoding).strip().rstrip('\x00')


def _exe(path):
    path = _safe(path)
    if not path.is_file():
        raise SetupError('Не найден исполняемый файл MT5/MetaEditor: ' + str(path))
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise SetupError('Неверный формат exe MT5/MetaEditor.')
    return path


def verify_metaquotes(path, *, runner=subprocess.run):
    """Default Windows Authenticode trust, exact publisher; no trust-store changes.

    Fails closed for offline/revocation/unknown publisher errors. This is not
    protection against a hostile administrator replacing files after validation.
    """
    path = _exe(path)
    official_root = _safe(Path(os.environ.get('ProgramFiles', r'C:\Program Files')))
    if official_root not in path.parents:
        raise SetupError('Автосборка разрешена только для официальной установки MT5 в Program Files. Пользовательскую/portable установку проверьте вручную.')
    powershell = _safe(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                       'System32/WindowsPowerShell/v1.0/powershell.exe')
    if not powershell.is_file():
        raise SetupError('Не найден системный PowerShell для проверки подписи MetaQuotes.')
    literal = str(path).replace("'", "''")
    script = "$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath '" + literal + "'; [pscustomobject]@{status=[string]$s.Status;publisher=$s.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,$false)} | ConvertTo-Json -Compress"
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        result = runner([str(powershell), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                        shell=False, capture_output=True, timeout=45)
        raw = result.stdout.decode('utf-8-sig') if isinstance(result.stdout, bytes) else result.stdout
        signature = json.loads(raw)
        if result.returncode or signature.get('status') != 'Valid' or signature.get('publisher') not in (
                'MetaQuotes Ltd.', 'MetaQuotes Software Corp.'):
            raise ValueError('untrusted publisher/signature')
    except (ValueError, TypeError, AttributeError, subprocess.SubprocessError, OSError) as exc:
        raise SetupError('Подпись MetaQuotes не подтверждена Windows. MetaEditor не запущен; проверьте официальную установку MT5.') from exc


def _existing_ea(experts, content):
    # Scan every subdirectory; do not introduce mixed root/versioned duplicates.
    found = set()
    for directory, dirs, files in os.walk(experts, followlinks=False):
        _safe(directory)
        for name in dirs:
            _safe(Path(directory) / name)
        if any(name.lower() in (EA_NAME.lower(), 'recoveryevidencebridge.ex5') for name in files):
            found.add(Path(directory))
    if len(found) > 1:
        raise SetupError('Найдены несколько копий RecoveryEvidenceBridge. Автоматическая установка остановлена; старые копии не изменены.')
    if not found:
        return None
    directory = found.pop()
    source, binary = directory / EA_NAME, directory / 'RecoveryEvidenceBridge.ex5'
    if not source.is_file() or not binary.is_file() or _safe(source).read_bytes() != content or _safe(binary).stat().st_size < 16:
        raise SetupError('Существующий EA нельзя подтвердить: исходник/EX5 отсутствует или отличается. Дубликат не создан; проверьте вручную.')
    return directory


def detect_mt5(terminal_path=None, data_dir=None, *, appdata=None, program_files=None):
    """Select exactly one origin-matched installation; never guess newest directory.

    An explicit portable data directory is allowed only beside its terminal.
    File discovery does NOT establish which terminal is currently running.
    """
    terminal = _safe(terminal_path) if terminal_path else None
    if terminal and terminal.name.lower() != 'terminal64.exe':
        raise SetupError('Выберите terminal64.exe.')
    if data_dir:
        candidates = [_safe(data_dir)]
    else:
        root = _safe(Path(appdata or os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'MetaQuotes/Terminal')
        candidates = sorted(root.iterdir()) if root.is_dir() else []
    matches = []
    for folder in candidates:
        folder = _safe(folder)
        if not (folder / 'MQL5/Experts').is_dir():
            continue
        _safe(folder / 'MQL5/Experts')
        origin = folder / 'origin.txt'
        if origin.is_file():
            origin_value = _text(origin)
            if not Path(origin_value).is_absolute():
                raise SetupError('origin.txt содержит не абсолютный путь.')
            install_dir = _safe(origin_value)
        elif data_dir and terminal and folder == terminal.parent:
            install_dir = folder  # explicitly selected portable installation
        else:
            continue
        found = install_dir / 'terminal64.exe'
        if terminal and os.path.normcase(str(found)) != os.path.normcase(str(terminal)):
            continue
        if not found.is_file():
            continue
        matches.append(Mt5Installation(_exe(found), _exe(install_dir / 'metaeditor64.exe'), folder))
    if len(matches) != 1:
        raise SetupError('Не удалось однозначно найти папку данных MT5. В MT5: Файл → Открыть каталог данных; выберите её в установщике.')
    return matches[0]


RECEIPT_NAME = 'installer-build-receipt.json'


def _sha(path):
    return hashlib.sha256(_safe(path).read_bytes()).hexdigest()


def _valid_build_log(path):
    summaries = re.findall(r'(?im)^\s*Result:\s*([^\r\n]+)', _text(path))
    return bool(summaries and re.match(r'^0 errors?,\s*\d+ warnings?\b', summaries[-1], re.IGNORECASE))


def _receipt(directory, installation):
    """Local continuity marker, NOT independent authenticity against local writers.

    Never adopt a hand-built EA by creating a receipt for it. Tool hashes enforce
    explicit review after an editor update rather than quietly changing provenance.
    """
    return {'version': 1, 'kind': 'installer-fresh-readonly-build',
            'directory': str(directory), 'pinned_source_sha256': EA_SHA256,
            'files': {name: _sha(directory / name) for name in
                      (EA_NAME, 'RecoveryEvidenceBridge.ex5', 'RecoveryEvidenceBridge.log')},
            'tools': {name: {'path': str(path), 'sha256': _sha(path)} for name, path in
                      (('terminal', installation.terminal), ('metaeditor', installation.metaeditor))}}


def _reuse_receipt(directory, installation):
    marker = _safe(directory / RECEIPT_NAME)
    if not marker.exists():
        return None
    try:
        if not re.fullmatch(r'RecoveryReadOnly-[0-9a-f]{12}', directory.name):
            raise ValueError('not installer directory')
        if marker.stat().st_size > 16384:
            raise ValueError('oversized receipt')
        recorded = json.loads(marker.read_text(encoding='utf-8'))
        if recorded != _receipt(directory, installation) or not _valid_build_log(directory / 'RecoveryEvidenceBridge.log'):
            raise ValueError('changed build/tool/log')
    except (ValueError, OSError, TypeError) as exc:
        raise SetupError('Квитанция сборки EA не подтверждена: файлы или MetaEditor изменились. Ничего не перезаписано; нужна ручная проверка.') from exc
    return BuildResult(directory / EA_NAME, directory / 'RecoveryEvidenceBridge.ex5',
                       directory / 'RecoveryEvidenceBridge.log', True)


def prepare_compile_ea(server, installation, *, log, runner=subprocess.run, platform=os.name):
    """Compile pinned read-only source, verify new binary + zero-error log.

    Reuses an installer-created EA with matching continuity receipt, or a sole
    manual EA only if fresh build bytes match. Otherwise fails
    closed; publishes a new subfolder only if no existing EA. Never overwrites
    source/binary or edits profiles. User must attach manually with account inputs.
    """
    if platform != 'nt':
        raise SetupError('Компиляция EA доступна только в Windows.')
    server = _safe(server)
    source = _safe(server / 'recovery/mql5' / EA_NAME)
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != EA_SHA256:
        raise SetupError('Исходник read-only EA отличается от проверенной версии. Компиляция отменена.')
    editor = _exe(installation.metaeditor)
    # Revalidate correspondence, not just dataclass supplied by caller.
    detected = detect_mt5(installation.terminal, installation.data_dir)
    if detected.metaeditor != editor:
        raise SetupError('MetaEditor не соответствует выбранному MT5.')
    experts = _safe(detected.data_dir / 'MQL5/Experts')
    existing = _existing_ea(experts, content)
    verify_metaquotes(detected.terminal)
    verify_metaquotes(editor)
    if existing is not None:
        reused = _reuse_receipt(existing, detected)
        if reused is not None:
            log('Сборка установщика проверена по квитанции и подписям инструментов; перекомпиляция не требуется.\n')
            return reused
    with tempfile.TemporaryDirectory(prefix='readonly-ea-build-', dir=server) as work:
        staged = Path(work) / EA_NAME
        staged.write_bytes(content)
        args = [str(editor), '/compile:' + str(staged), '/include:' + str(detected.data_dir / 'MQL5'), '/log']
        log('Компиляция read-only EA. Терминал и торговля не запускаются.\n')
        try:
            result = runner(args, shell=False, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired as exc:
            raise SetupError('MetaEditor не завершил компиляцию за 120 секунд. EA не установлен.') from exc
        binary, build_log = staged.with_suffix('.ex5'), staged.with_suffix('.log')
        log(f'Компилятор: exit={result.returncode}; EX5={binary.is_file()}; log={build_log.is_file()}.\n')
        if result.returncode not in (0, 1) or not binary.is_file() or not build_log.is_file():
            raise SetupError('MetaEditor не создал подтверждённую сборку EA. Выполните компиляцию вручную в MetaEditor.')
        if not _valid_build_log(build_log):
            raise SetupError('В журнале нет результата «0 errors». EA не установлен; проверьте компиляцию вручную.')
        _safe(binary)
        if (binary.stat().st_mtime_ns < staged.stat().st_mtime_ns
                or build_log.stat().st_mtime_ns < staged.stat().st_mtime_ns
                or staged.read_bytes() != content):
            raise SetupError('EX5/журнал старее исходника или исходник изменился. Сборка не подтверждена.')
        if binary.stat().st_size < 16:
            raise SetupError('Файл EX5 пуст или повреждён.')
        if existing is not None:
            old_binary = _safe(existing / 'RecoveryEvidenceBridge.ex5')
            if old_binary.read_bytes() != binary.read_bytes():
                raise SetupError('Существующий EX5 не совпадает со свежей проверенной сборкой. Он сохранён, дубликат не создан. Возможно, другая версия компилятора; проверьте вручную.')
            log('Существующий EA проверен свежей сборкой и оставлен на прежнем месте. Повторное прикрепление не требуется.\n')
            return BuildResult(existing / EA_NAME, old_binary, None, True)
        # Recheck before publication; avoid introducing a duplicate on concurrent changes.
        if _existing_ea(experts, content) is not None:
            raise SetupError('EA появился во время сборки. Ничего не установлено; повторите проверку.')
        # No old binary can satisfy checks: staging was created empty for this run.
        destination = experts / ('RecoveryReadOnly-' + uuid.uuid4().hex[:12])
        destination.mkdir(exist_ok=False)
        try:
            for file in (staged, binary, build_log):
                with (destination / file.name).open('xb') as target:
                    target.write(file.read_bytes())
            # Publish last, exclusively: interrupted copies never gain a valid receipt.
            with (destination / RECEIPT_NAME).open('x', encoding='utf-8') as marker:
                json.dump(_receipt(destination, detected), marker, sort_keys=True, indent=2)
        except Exception as exc:
            raise SetupError('Копирование EA не завершено. Частичная новая папка сохранена, старые EA не изменены.') from exc
    log('EA собран. Прикрепите его к графику вручную, задайте ExpectedLogin/ExpectedServer. Algo Trading не включён.\n')
    return BuildResult(destination / EA_NAME, destination / 'RecoveryEvidenceBridge.ex5', destination / 'RecoveryEvidenceBridge.log')
