"""Preparation only: never starts the runtime, MT5, Caddy, or trading."""
from pathlib import Path
import os
import shutil
import subprocess


class SetupError(RuntimeError):
    pass


def checked_run(args, *, log, runner=subprocess.run):
    # A list, never shell=True. Do not include secrets in arguments.
    result = runner([str(x) for x in args], capture_output=True, text=True,
                    encoding='utf-8', errors='replace', shell=False)
    if result.stdout:
        log(result.stdout)
    if result.stderr:
        log(result.stderr)
    if result.returncode:
        raise SetupError('Команда завершилась с ошибкой. Подробности в журнале окна.')
    return result.stdout or ''


def copy_server(payload, target):
    payload, target = Path(payload).resolve(), Path(target).absolute()
    if not (payload / 'requirements.txt').is_file() or not (payload / 'run_recovery_native.py').is_file():
        raise SetupError('Неполный пакет server. Распакуйте весь ZIP.')
    # Existing installations/configs/databases are never overwritten or merged.
    if target.exists() or target.is_symlink():
        raise SetupError('Папка установки уже существует. Выберите новое имя папки; старая версия и данные не изменяются.')
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink():
        raise SetupError('Выберите существующую локальную родительскую папку.')
    if target == payload or payload in target.parents:
        raise SetupError('Нельзя устанавливать внутрь исходного пакета.')
    if str(target).startswith(('\\\\', '//')):
        raise SetupError('Сетевая папка не поддерживается. Выберите локальный диск.')
    # mkdir reserves the chosen destination without replacing anything.
    target.mkdir(exist_ok=False)
    try:
        for source in payload.rglob('*'):
            if source.is_symlink():
                raise SetupError('Ссылки в исходном пакете не поддерживаются.')
        shutil.copytree(payload, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    except BaseException:
        # Preserve partial copy for investigation; never delete a user directory.
        raise SetupError('Копирование не завершено. Неполная папка сохранена; выберите новую папку после исправления причины.')
    return target


def install(payload, target, python, *, log, runner=subprocess.run, platform=os.name):
    if platform != 'nt':
        raise SetupError('Установка предназначена только для Windows x64.')
    python = Path(python)
    if not python.is_absolute() or not python.is_file():
        raise SetupError('Выберите существующий python.exe (64 bit).')
    call = lambda *args: checked_run(args, log=log, runner=runner)
    call(python, '-c', "import sys,struct; assert sys.platform == 'win32' and struct.calcsize('P') == 8 and sys.version_info >= (3,10)")
    target = copy_server(payload, target)
    log('Скопирован новый server. Существующие установки не изменены.\n')
    venv = target / '.venv-native'
    call(python, '-m', 'venv', venv)
    executable = venv / 'Scripts' / 'python.exe'
    # Explicit PyPI source; wheel-only avoids executing package build scripts.
    call(executable, '-m', 'pip', '--isolated', 'install', '--index-url', 'https://pypi.org/simple',
         '--only-binary=:all:', '-r', target / 'requirements.txt')
    call(executable, '-m', 'pip', 'check')
    call(executable, '-m', 'compileall', '-q', target / 'recovery', target / 'run_recovery_native.py')
    call(executable, '-c', "import MetaTrader5,fastapi,uvicorn; print('Imports OK; MT5 was NOT initialized')")
    versions = call(executable, '-m', 'pip', 'freeze')
    (venv / 'installed-versions.txt').write_text(versions, encoding='utf-8')
    log('Зависимости готовы. MT5 не подключён, торговля и Caddy НЕ запущены.\n')
    return target


def prepare_ea(server, data_dir):
    """Manual selection of MT5 Data Folder. Source only; no compile/attach."""
    data_dir, server = Path(data_dir), Path(server)
    experts = data_dir / 'MQL5' / 'Experts'
    if not experts.is_dir() or experts.is_symlink():
        raise SetupError('Выберите каталог данных MT5 с папкой MQL5\\Experts.')
    source = server / 'recovery' / 'mql5' / 'RecoveryEvidenceBridge.mq5'
    if not source.is_file() or source.is_symlink():
        raise SetupError('Исходник EA отсутствует.')
    target = experts / source.name
    # Exclusive creation protects existing source and compiled EA versions.
    if target.exists() or target.is_symlink() or target.with_suffix('.ex5').exists():
        raise SetupError('В MT5 уже есть этот EA или его .ex5. Ничего не перезаписано; проверьте версии вручную.')
    content = source.read_bytes()
    with target.open('xb') as f:
        f.write(content)
    return target

CADDY_URL = 'https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_windows_amd64.zip'
CADDY_SHA256 = 'ad93112d335ee687100c4c9f11bced16ca590377976b7fe7883882b9e797e472'


def install_caddy(server, *, log, opener=None, local_archive=None):
    """Fetch official fixed release, verify archive, install only exe; never run it."""
    import hashlib
    import tempfile
    import urllib.request
    import zipfile
    opener = opener or urllib.request.urlopen
    folder = Path(server) / 'tools' / 'caddy'
    for part in [folder, *folder.parents]:
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 1024):
            raise SetupError('Ссылки и junction в папке назначения Caddy не поддерживаются.')
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'caddy.exe'
    if target.exists() or target.is_symlink():
        raise SetupError('caddy.exe уже существует. Сохранён без изменений; автоматическая замена запрещена.')
    log('Проверка локального архива Caddy…\n' if local_archive else 'Загрузка Caddy 2.11.9 Windows amd64 с официального GitHub релиза…\n')
    with tempfile.TemporaryDirectory(prefix='caddy-download-') as temp:
        archive = Path(temp) / 'caddy.zip'
        digest = hashlib.sha256()
        count = 0
        source = Path(local_archive).open('rb') if local_archive else opener(CADDY_URL, timeout=60)
        with source as response, archive.open('wb') as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk: break
                count += len(chunk)
                if count > 100 * 1024 * 1024:
                    raise SetupError('Архив Caddy превышает допустимый размер.')
                digest.update(chunk); output.write(chunk)
        if digest.hexdigest() != CADDY_SHA256:
            raise SetupError('SHA256 Caddy не совпадает. Файл не установлен; повторите загрузку позже.')
        with zipfile.ZipFile(archive) as z:
            entries = [x for x in z.infolist() if x.filename == 'caddy.exe']
            if len(entries) != 1 or entries[0].is_dir() or entries[0].file_size > 100 * 1024 * 1024:
                raise SetupError('Неожиданная структура архива Caddy.')
            # Never extractall: all other entries (including paths) are ignored.
            content = z.read(entries[0])
            if not content.startswith(b'MZ'):
                raise SetupError('Caddy exe имеет неверный формат.')
        # Verified bytes only; no overwrite and no partial executable on failure.
        staged = folder / 'caddy.exe.prepared'
        owned_stage = False
        try:
            with staged.open('xb') as out:
                owned_stage = True
                out.write(content)
            # Windows rename fails if destination already exists, unlike replace.
            if target.exists(): raise SetupError('caddy.exe появился во время установки; не изменён.')
            if os.name == 'nt': staged.rename(target)
            else:
                # Test/platform-neutral exclusive publication.
                os.link(staged, target); staged.unlink()
        finally:
            if owned_stage and staged.exists(): staged.unlink()
    log(f'Caddy установлен: {target}\nCaddy не запущен. HTTPS, порты и службы не изменены.\n')
    return target


def adopt_existing(payload, target):
    """Read-only verification for preparation buttons, NOT permission to run code."""
    payload, target = Path(payload), Path(target)
    if not target.is_absolute() or not target.is_dir():
        raise SetupError('Выберите существующую папку Recovery-Server.')
    def plain(path):
        for part in [path, *path.parents]:
            if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 1024):
                raise SetupError('Ссылки и junction в выбранной папке не поддерживаются.')
    plain(target)
    # Compare packaged source files, excluding interpreter/test caches. Extra user
    # config/data are preserved and are never imported or executed here.
    for source in payload.rglob('*'):
        if not source.is_file() or '__pycache__' in source.parts or source.suffix == '.pyc':
            continue
        dest = target / source.relative_to(payload)
        plain(dest)
        if not dest.is_file() or dest.read_bytes() != source.read_bytes():
            raise SetupError('Файлы сервера отличаются от проверенного пакета или отсутствуют. Папка не принята; ничего не изменено.')
    venv = target / '.venv-native'
    for name in ('pyvenv.cfg', 'Scripts/python.exe', 'installed-versions.txt'):
        item = venv / name; plain(item)
        if not item.is_file() or item.stat().st_size == 0:
            raise SetupError('Не найдены маркеры завершённой установки Python-зависимостей.')
    with (venv / 'Scripts/python.exe').open('rb') as f:
        if f.read(2) != b'MZ': raise SetupError('Неверный формат python.exe.')
    if 'home' not in (venv / 'pyvenv.cfg').read_text(encoding='utf-8', errors='replace'):
        raise SetupError('Неверный pyvenv.cfg.')
    return target
