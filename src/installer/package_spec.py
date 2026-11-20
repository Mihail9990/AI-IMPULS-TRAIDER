from pathlib import Path
import hashlib

RUNTIME = ("control_host.py", "ipc_files.py", "local_settings.py", "python_supervisor.py", "recovery_book.py",
           "recovery_desktop.pyw", "recovery_helpers.py", "recovery_strategy.py")
HELPERS = ("RecoveryPythonSL", "RecoveryPythonVerifier")
WHEELS = {
    "metatrader5-5.0.6231-cp313-cp313-win_amd64.whl": "3a8c8349ce691a4ef05e314afc034ad82f4dbb19415efc7442af5ea075d5e797",
    "numpy-2.5.5-cp313-cp313-win_amd64.whl": "e0608d1585557b68dcb6a70a41654f7536567960afad79d91752d2863b242b68",
}
SHORTCUT = "RECOVERY — Пульт управления.lnk"
BOOTSTRAP = ("Install.cmd", "install.py", "package_spec.py", "README_RU.txt")


def digest(path):
    with Path(path).open("rb") as fd:
        return hashlib.file_digest(fd, "sha256").hexdigest()
