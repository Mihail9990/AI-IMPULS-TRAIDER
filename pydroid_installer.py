"""Standalone installer/updater for running the bot from Pydroid 3.

Download only this file, open it in Pydroid 3, and press Run. It uses only Python's standard
library until it installs requirements for the downloaded project.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from zipfile import ZipFile


ARCHIVE_URL = "https://github.com/Mihail9990/AI-IMPULS-TRAIDER/archive/refs/heads/work.zip"
VERSION_URL = "https://api.github.com/repos/Mihail9990/AI-IMPULS-TRAIDER/commits/work"
PROJECT_NAME = "AI-IMPULS-TRAIDER"
PRESERVE = {
    "bot_config.json", "bot_state.json", "bot_state.sqlite3", "bot_state.sqlite3-wal",
    "bot_state.sqlite3-shm", "bot_state.sqlite3.lock", "demo_captures",
    "bot_diagnostics.log.history",
}


def is_runtime_data(name: str) -> bool:
    return name in PRESERVE or name.startswith("bot_state.sqlite3.backup") \
        or name == "bot_diagnostics.log" or (
        name.startswith("bot_diagnostics.log.") and name.removeprefix("bot_diagnostics.log.").isdigit()
    )


def default_install_dir() -> Path:
    current = Path.cwd().resolve()
    # Running a downloaded installer from an existing project must offer that project itself,
    # not a surprising AI-IMPULS-TRAIDER/AI-IMPULS-TRAIDER nested copy.
    if current.name == PROJECT_NAME and (current / "main.py").is_file():
        return current
    android_download = Path("/storage/emulated/0/Download")
    root = android_download if android_download.is_dir() and os.access(android_download, os.W_OK) else Path.cwd()
    return root / PROJECT_NAME


def choose_install_dir(*, input_fn=input, output_fn=print) -> Path | None:
    """Interactively select and confirm the project directory before changing any files."""
    suggested = default_install_dir().resolve()
    while True:
        output_fn("\nChoose the AI-IMPULS-TRAIDER project folder.")
        output_fn(f"Press Enter for the suggested folder: {suggested}")
        output_fn("Or enter the full path of an existing installation; Q cancels.")
        answer = input_fn("Project folder: ").strip()
        if answer.lower() in {"q", "quit", "cancel"}:
            output_fn("Installation cancelled before any files were changed.")
            return None
        target = (Path(answer).expanduser() if answer else suggested).resolve()
        output_fn(f"Final absolute project path: {target}")
        confirmation = input_fn("Use this folder? [y]es / [n]o / [q]uit: ").strip().lower()
        if confirmation in {"y", "yes"}:
            return target
        if confirmation in {"q", "quit", "cancel"}:
            output_fn("Installation cancelled before any files were changed.")
            return None
        output_fn("Path was not confirmed; choose it again.")


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
        if is_runtime_data(item.name) and target.exists():
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


def installed_version() -> str:
    try:
        request = Request(VERSION_URL, headers={"User-Agent": "AI-IMPULS-TRAIDER-Pydroid-Installer"})
        with urlopen(request, timeout=30) as response:
            return str(json.load(response).get("sha", "unknown"))
    except Exception as error:
        print(f"Could not query work commit (installation continues): {error}")
        return "work (commit lookup unavailable)"


def install(archive_url: str = ARCHIVE_URL, install_dir: Path | None = None) -> Path:
    target = (install_dir or default_install_dir()).resolve()
    print(f"Installing into: {target}")
    version = installed_version()
    download_url = archive_url
    if archive_url == ARCHIVE_URL and len(version) == 40:
        # Pin the archive to the SHA we report, avoiding a branch update between two requests.
        download_url = f"https://github.com/Mihail9990/AI-IMPULS-TRAIDER/archive/{version}.zip"
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        archive = temporary_path / "project.zip"
        print("Downloading the work branch...")
        download(download_url, archive)
        source = safe_extract(archive, temporary_path / "unpacked")
        copy_project(source, target)
    created = create_config(target)
    (target / "installed_version.txt").write_text(version + "\n", encoding="utf-8")
    print("Installing Python requirements...")
    install_requirements(target)
    print("\nInstallation completed successfully.")
    print(f"Project: {target}")
    print(f"Installed work version: {version}")
    print("Created bot_config.json." if created else (
        "Preserved existing settings and runtime state: bot_config.json, legacy JSON, "
        "SQLite/WAL/SHM/backups and diagnostic history."
    ))
    print("Next: open bot_config.json, enter DEMO credentials, then run main.py in Pydroid 3.")
    return target


if __name__ == "__main__":
    try:
        selected = choose_install_dir()
        if selected is not None:
            install(install_dir=selected)
    except Exception as error:
        print(f"\nINSTALLATION FAILED: {error}")
        print("Check internet/storage permission and run this file again.")
        raise
