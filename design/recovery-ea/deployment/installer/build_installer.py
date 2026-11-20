"""Build the Windows x64 installer from a verified MetaEditor candidate."""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import sys
import tempfile
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent
REPO = DEPLOY.parents[2]
sys.path.insert(0, str(DEPLOY))
from stage_recovery_install import prepare_stage

EA_COMMIT = "254e6932e9898b20315f003d498fe598d8e70baa"
PYTHON_ZIP = "python-3.13.17-embed-amd64.zip"
PYTHON_URL = "https://www.python.org/ftp/python/3.13.17/" + PYTHON_ZIP
PYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
LOCK = HERE / "requirements-panel.txt"

# A positive list keeps personal state, native compiler logs, tester fixtures
# and the legacy Python trading backend out of the redistributable.
PANEL = {
    "design/recovery-ea/deployment": ("run_panel.py", "fresh_profile.py", "role_launch.py"),
    "design/recovery-ea/channel": (
        "channel.py", "web_handoff.py", "runtime_panel.py", "runtime_panel.html",
        "runtime_panel.css", "runtime_panel.mjs", "recovery_manual.html", "recovery_manual.mjs",
    ),
    "design/recovery-ea/settings": (
        "support_controls.html", "support_controls.mjs", "support_panel.mjs",
        "support_routes.py", "support_store.py", "support_settings.py",
    ),
    "runtime": ("web_session.py",),
    "web": ("session.js",),
}
LAUNCH_FILES = ("README_RU.txt", "RECOVERY.cmd", "recovery_setup.py", "private_acl.ps1")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def python_archive(cache: Path) -> bytes:
    file = cache / PYTHON_ZIP
    if file.is_file():
        data = file.read_bytes()
    else:
        with urlopen(PYTHON_URL, timeout=90) as response:
            data = response.read()
    if digest(data) != PYTHON_SHA256:
        raise ValueError("Unexpected digest for the official embedded Python archive")
    if not file.exists():
        file.write_bytes(data)
    return data


def wheels(cache: Path) -> list[Path]:
    wanted = {line.rsplit(" --hash=sha256:", 1)[1] for line in LOCK.read_text().splitlines() if line}
    directory = cache / "wheels"
    directory.mkdir(exist_ok=True)

    def verified():
        return {checksum: file for file in directory.glob("*.whl")
                if (checksum := digest(file.read_bytes())) in wanted}

    available = verified()
    if available.keys() != wanted:
        subprocess.run([
            sys.executable, "-m", "pip", "--disable-pip-version-check", "download",
            "--index-url", "https://pypi.org/simple", "--only-binary=:all:", "--no-deps",
            "--require-hashes", "--dest", str(directory), "-r", str(LOCK),
        ], check=True)
        available = verified()
    if available.keys() != wanted:
        raise ValueError("Missing or unverified Python wheels")
    return sorted(available.values())


def build_payload(stage: Path, results: Path, cache: Path, target: Path) -> None:
    hashes: dict[str, str] = {}
    paths: set[str] = set()
    with ZipFile(target, "w") as payload:
        def add(path: str, data: bytes):
            parts = PurePosixPath(path).parts
            if (not parts or parts[0] not in ("App", "Experts") or ".." in parts
                    or path != "/".join(parts) or re.search(r'[\\:\x00-\x1f]', path)
                    or path.casefold() in paths):
                raise ValueError(f"Invalid or duplicate payload path: {path}")
            info = ZipInfo(path)
            info.compress_type = ZIP_DEFLATED
            payload.writestr(info, data, compresslevel=9)
            hashes[path] = digest(data)
            paths.add(path.casefold())

        for name in ("RecoveryVerifier", "RecoveryPriceWatcher", "RecoveryTrader"):
            add(f"Experts/{name}.ex5", (stage / (name + ".ex5")).read_bytes())
        add("App/stage-manifest.json", (stage / "stage-manifest.json").read_bytes())
        add("App/compile-report.json", (results / "compile-report.json").read_bytes())
        add("App/requirements-panel.txt", LOCK.read_bytes())
        for folder, files in PANEL.items():
            for name in files:
                add(f"App/{folder}/{name}", (REPO / folder / name).read_bytes())
        for name in LAUNCH_FILES:
            add("App/" + name, (HERE / "app" / name).read_bytes())
        with ZipFile(BytesIO(python_archive(cache))) as python:
            for name in python.namelist():
                if name == "python313._pth":
                    add("App/python/" + name, b"python313.zip\r\n.\r\n..\r\nsite-packages\r\n")
                elif not name.endswith("/"):
                    add("App/python/" + name, python.read(name))
        for wheel in wheels(cache):
            with ZipFile(wheel) as package:
                for name in package.namelist():
                    if name.endswith("/"):
                        continue
                    if ".data/" in name:
                        raise ValueError(f"Wheel requires explicit installation layout: {wheel.name}")
                    add("App/python/site-packages/" + name, package.read(name))
        manifest = "".join(f"{hashes[path]}  {path}\n" for path in sorted(hashes))
        payload.writestr(ZipInfo("SHA256SUMS"), manifest.encode("utf-8"), compress_type=ZIP_DEFLATED)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--archive-sha256", required=True)
    p.add_argument("--results", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13) or struct.calcsize("P") != 8:
        p.error("Use x64 Python 3.13 on Windows")
    if args.output.exists():
        p.error("Choose a new EXE output path")
    import os
    compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        p.error(".NET Framework x64 compiler was not found")
    args.cache.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stage, _ = prepare_stage(args.archive, EA_COMMIT, args.archive_sha256, args.results)
    try:
        with tempfile.TemporaryDirectory(prefix="recovery-setup-", dir=args.output.parent) as temp:
            root = Path(temp).resolve()
            zip_path = root / "payload.zip"
            exe = root / "RECOVERY-Setup-x64.exe"
            build_payload(stage, args.results, args.cache, zip_path)
            subprocess.run([
                str(compiler), "/nologo", "/utf8output", "/codepage:65001", "/optimize+",
                "/target:winexe", "/platform:x64", "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
                "/r:System.IO.Compression.dll", "/r:System.IO.Compression.FileSystem.dll",
                "/win32manifest:" + str(HERE / "setup.manifest"),
                "/resource:" + str(zip_path) + ",RecoveryPayload.zip", "/out:" + str(exe),
                str(HERE / "RecoverySetup.cs"),
            ], check=True)
            exe.rename(args.output)
    finally:
        # prepare_stage created this unique temporary folder with these four files.
        for f in stage.iterdir():
            f.unlink()
        stage.rmdir()
    print(digest(args.output.read_bytes()), args.output)


if __name__ == "__main__":
    main()
