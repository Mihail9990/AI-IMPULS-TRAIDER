"""Standalone installer/updater for running the bot from Pydroid 3.

Download only this file, open it in Pydroid 3, and press Run. It uses only Python's standard
library until it installs requirements for the downloaded project.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from zipfile import ZipFile


ARCHIVE_URL = "https://github.com/Mihail9990/AI-IMPULS-TRAIDER/archive/refs/heads/work.zip"
PROJECT_NAME = "AI-IMPULS-TRAIDER"
PRESERVE = {"bot_config.json", "bot_state.json", "demo_captures"}


def default_install_dir() -> Path:
    android_download = Path("/storage/emulated/0/Download")
    root = android_download if android_download.is_dir() and os.access(android_download, os.W_OK) else Path.cwd()
    return root / PROJECT_NAME


def download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "AI-IMPULS-TRAIDER-Pydroid-Installer"})
    with urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def safe_extract(archive: Path, destination: Path) -> Path:
    destination = destination.resolve()
    with ZipFile(archive) as bundle:
        for item in bundle.infolist():
            target = (destination / item.filename).resolve()
            if destination != target and destination not in target.parents:
                raise RuntimeError(f"Unsafe path in archive: {item.filename}")
        bundle.extractall(destination)
    roots = [item for item in destination.iterdir() if item.is_dir()]
    if len(roots) != 1 or not (roots[0] / "main.py").exists():
        raise RuntimeError("Downloaded archive does not contain the expected project")
    return roots[0]


def copy_project(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if item.name in PRESERVE and target.exists():
            continue
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)


def create_config(project: Path) -> bool:
    config = project / "bot_config.json"
    if config.exists():
        return False
    shutil.copy2(project / "bot_config.example.json", config)
    return True


def install_requirements(project: Path) -> None:
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "-r", str(project / "requirements.txt")
    ])


def install(archive_url: str = ARCHIVE_URL, install_dir: Path | None = None) -> Path:
    target = (install_dir or default_install_dir()).resolve()
    print(f"Installing into: {target}")
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / "project.zip"
        print("Downloading the work branch...")
        download(archive_url, archive)
        source = safe_extract(archive, temporary_path / "unpacked")
        copy_project(source, target)
    created = create_config(target)
    print("Installing Python requirements...")
    install_requirements(target)
    print("\nInstallation completed successfully.")
    print(f"Project: {target}")
    print("Created bot_config.json." if created else "Preserved existing bot_config.json and bot_state.json.")
    print("Next: open bot_config.json, enter DEMO credentials, then run main.py in Pydroid 3.")
    return target


if __name__ == "__main__":
    try:
        install()
    except Exception as error:
        print(f"\nINSTALLATION FAILED: {error}")
        print("Check internet/storage permission and run this file again.")
        raise
